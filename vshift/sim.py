"""Hardware-free simulator: a fake trainer plus a rider who holds a set power.

In SIM mode the rider's power is fixed, so changing gear changes cadence,
which is exactly what you want to see when tuning a gear table:
cadence = speed(power, grade) / (ratio * wheel circumference).
In ERG mode the rider spins at `sim_cadence` and power follows the target.
"""
from __future__ import annotations

import asyncio
import random

from .core import Session


class SimTrainer:
    def __init__(self, session: Session, native: bool, rider_watts: float, erg_cadence: float):
        self.session, self.native = session, native
        self.rider_watts, self.erg_cadence = rider_watts, erg_cadence
        self._ratio = session.native_ratio()
        self._grade = session.state.grade_pct
        self._erg: int | None = None
        self._emulated_target: int | None = None

    async def set_gear(self, ratio: float) -> None:
        self._ratio = ratio

    async def set_grade(self, grade_pct: float) -> None:
        self._grade = grade_pct

    async def set_erg(self, watts: int | None) -> None:
        self._erg = watts

    async def set_power_target(self, watts: int) -> None:
        self._emulated_target = watts

    def _speed_for_power(self, watts: float, grade: float) -> float:
        lo, hi = 0.0, 30.0  # bisection: Physics.power is monotonic in v
        ph = self.session.physics
        for _ in range(50):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if ph.power(mid, grade) < watts else (lo, mid)
        return lo

    async def run(self) -> None:
        s = self.session
        s.state.trainer_status = "simulator"
        await s.on_trainer_ready(self)
        while True:
            noise = random.uniform(-4, 4)
            st = s.state
            if st.erg and self._erg is not None:
                s.on_trainer_data(power=round(self._erg + noise), cadence=self.erg_cadence + noise / 4)
            else:
                ratio = st.ratio  # the gear the rider is actually in
                v = self._speed_for_power(self.rider_watts, st.grade_pct)
                cad = v / (ratio * s.physics.wheel_circ_m) * 60
                s.on_trainer_data(power=round(self.rider_watts + noise), cadence=round(cad, 1),
                                  speed_kmh=v * 3.6)
            await asyncio.sleep(0.25)
