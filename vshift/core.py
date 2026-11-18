"""Ride state and shifting logic. Hardware-independent; runs on the asyncio loop.

The trainer backend (BLE or simulator) implements the Trainer protocol below
and reports data back through Session.on_* callbacks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from .protocol import Physics, suggest_gear

log = logging.getLogger("vshift")


class Trainer(Protocol):
    native: bool  # True once the Zwift-protocol handshake succeeded

    async def set_gear(self, ratio: float) -> None: ...
    async def set_grade(self, grade_pct: float) -> None: ...
    async def set_erg(self, watts: int | None) -> None: ...
    async def set_power_target(self, watts: int) -> None: ...  # for ftms_erg emulation


@dataclass
class State:
    power: int | None = None
    power_avg: float | None = None
    cadence: float | None = None
    speed_kmh: float | None = None
    hr: int | None = None
    gear_index: int = 0
    gear_count: int = 0
    ratio: float = 0.0
    grade_pct: float = 0.0
    erg: bool = False
    erg_watts: int = 200
    emulated_target: int | None = None  # ftms_erg mode: watts we asked for
    suggested_index: int | None = None
    trainer_status: str = "searching"
    trainer_mode: str = "-"
    controllers: dict[str, str] = field(default_factory=dict)
    sensor_status: str = "off"


class Session:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ratios: list[float] = cfg["gears"]["ratios"]
        phys = {k: v for k, v in cfg["physics"].items() if k != "trainer_cwa"}
        self.physics = Physics(rider_kg=cfg["rider"]["rider_kg"], bike_kg=cfg["rider"]["bike_kg"], **phys)
        s = self.state = State()
        s.gear_index = cfg["gears"]["start_index"]
        s.gear_count = len(self.ratios)
        s.ratio = self.ratios[s.gear_index]
        s.grade_pct = cfg["grade"]["start_pct"]
        s.erg_watts = cfg["erg"]["start_watts"]
        self.trainer: Trainer | None = None
        self._power_hist: deque[tuple[float, int]] = deque()
        self._sensor_cadence: float | None = None
        self._last_sensor_at = 0.0

    # ------------------------------------------------------------ mode helpers

    @property
    def emulating(self) -> bool:
        """True if *we* compute resistance (ftms_erg) instead of the trainer."""
        mode = self.cfg["trainer"]["mode"]
        if mode == "ftms_erg":
            return True
        if mode == "native":
            return False
        return not (self.trainer and self.trainer.native)

    def native_ratio(self) -> float:
        return self.state.ratio * self.cfg["trainer"]["native_ratio_scale"]

    # ------------------------------------------------------------ user commands

    async def shift(self, delta: int) -> None:
        s = self.state
        new = max(0, min(len(self.ratios) - 1, s.gear_index + delta))
        if new == s.gear_index:
            return
        s.gear_index, s.ratio = new, self.ratios[new]
        if s.erg:  # shifting implies you want to ride on gears again
            s.erg = False
            if self.trainer:
                await self.trainer.set_erg(None)
        log.info("gear %d/%d ratio %.3f", new + 1, len(self.ratios), s.ratio)
        await self._apply_resistance()

    async def toggle_erg(self) -> None:
        s = self.state
        s.erg = not s.erg
        log.info("ERG %s (%d W)", "on" if s.erg else "off", s.erg_watts)
        if self.trainer:
            await self.trainer.set_erg(s.erg_watts if s.erg else None)
        if not s.erg:
            await self._apply_resistance()

    async def adjust_erg(self, steps: int) -> None:
        s = self.state
        s.erg_watts = max(0, s.erg_watts + steps * self.cfg["erg"]["step_watts"])
        if s.erg and self.trainer:
            await self.trainer.set_erg(s.erg_watts)

    async def adjust_grade(self, steps: int) -> None:
        s = self.state
        s.grade_pct = round(max(-10.0, min(20.0, s.grade_pct + steps * self.cfg["grade"]["step_pct"])), 2)
        log.info("grade %.1f%%", s.grade_pct)
        if not s.erg:
            await self._apply_resistance()

    async def _apply_resistance(self) -> None:
        if not self.trainer or self.state.erg:
            return
        if self.emulating:
            await self._emulate_once()
        else:
            await self.trainer.set_grade(self.state.grade_pct)
            await self.trainer.set_gear(self.native_ratio())

    async def on_trainer_ready(self, trainer: Trainer) -> None:
        """Called by the backend after connecting (and after reconnects)."""
        self.trainer = trainer
        self.state.trainer_mode = "ERG-emulated" if self.emulating else "native gears"
        log.info("trainer ready, mode: %s", self.state.trainer_mode)
        if self.state.erg:
            await trainer.set_erg(self.state.erg_watts)
        else:
            await self._apply_resistance()

    # ------------------------------------------------------------ data in

    def on_trainer_data(self, power=None, cadence=None, speed_kmh=None, hr=None, wheel_kmh=None) -> None:
        """speed_kmh: virtual speed for display (Zwift protocol only).
        wheel_kmh: trainer's real flywheel-based speed (FTMS), for derived cadence."""
        s, now = self.state, time.monotonic()
        if power is not None:
            s.power = power
            self._power_hist.append((now, power))
        if hr:
            s.hr = hr
        src = self.cfg["cadence"]["source"]
        if src == "trainer" and cadence is not None:
            s.cadence = cadence
        elif src == "derived" and wheel_kmh is not None:
            # With the chain fixed on the Cog, flywheel speed is proportional to cadence.
            c = self.cfg["cadence"]
            phys = c["physical_chainring"] / c["physical_cog"]
            s.cadence = wheel_kmh / 3.6 / (phys * self.physics.wheel_circ_m) * 60
        if speed_kmh is not None and not self.emulating:
            s.speed_kmh = speed_kmh  # Zwift protocol reports the trainer's virtual speed

    def on_sensor_cadence(self, rpm: float) -> None:
        self._sensor_cadence, self._last_sensor_at = rpm, time.monotonic()
        if self.cfg["cadence"]["source"] == "sensor":
            self.state.cadence = rpm

    # ------------------------------------------------------------ periodic

    async def _emulate_once(self) -> None:
        """ftms_erg: resistance = our physics model at the virtual speed."""
        s = self.state
        cad = s.cadence or 0.0
        v = self.physics.speed_mps(cad, s.ratio)
        target = round(self.physics.power(v, s.grade_pct)) if cad > 5 else 0
        s.speed_kmh = v * 3.6
        if s.emulated_target is None or abs(target - s.emulated_target) >= 2:
            s.emulated_target = target
            if self.trainer:
                await self.trainer.set_power_target(target)

    async def run(self) -> None:
        win = self.cfg["display"]["power_avg_s"]
        target_cad = self.cfg["cadence"]["target"]
        while True:
            now = time.monotonic()
            while self._power_hist and now - self._power_hist[0][0] > win:
                self._power_hist.popleft()
            s = self.state
            s.power_avg = (sum(p for _, p in self._power_hist) / len(self._power_hist)
                           if self._power_hist else None)
            if self.cfg["cadence"]["source"] == "sensor" and now - self._last_sensor_at > 4:
                s.cadence = None
            s.suggested_index = (suggest_gear(self.ratios, s.gear_index, s.cadence, target_cad)
                                 if s.cadence and not s.erg else None)
            if self.trainer and self.emulating and not s.erg:
                await self._emulate_once()
            await asyncio.sleep(0.5)
