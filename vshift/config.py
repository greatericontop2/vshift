"""Config loading (YAML or JSON) with defaults and gear-table generation."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import yaml

DEFAULTS = {
    "trainer": {
        "name": "",          # substring of BLE name, e.g. "D500"; empty = first FTMS/Zwift trainer
        "address": "",       # optional MAC; overrides name
        # native   : send gear ratios over the Zwift protocol (Zwift Ready / Cog trainers)
        # ftms_erg : compute power from your own physics model and set ERG targets (any FTMS trainer)
        # auto     : native if the trainer accepts the Zwift handshake, else ftms_erg
        "mode": "auto",
        "native_ratio_scale": 1.0,  # multiply ratios sent natively, if feel doesn't match
    },
    "controllers": {
        "enabled": True,
        "addresses": [],     # optional MACs; empty = any Zwift controller seen
        # Click v2 left pod only streams while Zwift's server-signed unlock is valid.
        #   right_only    : ignore the left pod; map everything to right-side buttons
        #   restart_left  : reboot the left pod every restart_left_s to get a fresh
        #                   unlock window (left buttons drop out ~1-2 s per restart)
        #   zwift_unlocked: you unlocked it in the Zwift app today; use both pods as-is
        "click_v2_mode": "right_only",
        "restart_left_s": 50,
        # Right pod: + is shift_up_right; Y/Z/A/B are y/z/a/b. Left pod: d-pad up/down/
        # left/right, - is shift_up_left. Click v1 sends click_plus/click_minus.
        "shift_up": ["click_plus", "shift_up_right"],
        "shift_down": ["click_minus", "b"],
        "toggle_erg": ["a"],
    },
    "cadence_sensor": {"enabled": False, "name": "", "address": ""},
    "gears": {
        # Either list explicit ratios ...
        "ratios": [],
        # ... or generate a geometric ladder with a constant % step.
        "generate": {"start": 1.2, "end": 4.8, "step_pct": 3.5},
        "start_gear": 0,     # 1-based; 0 = gear closest to start_ratio
        "start_ratio": 2.4,
    },
    "rider": {"rider_kg": 75.0, "bike_kg": 9.0},
    "physics": {  # used by ftms_erg mode and the ERG-emulated speed display
        "cda": 0.32, "crr": 0.004, "rho": 1.2, "drivetrain_eff": 0.97, "wheel_circ_m": 2.105,
    },
    "cadence": {
        "target": 90,
        # trainer | sensor | derived (from trainer speed: needs physical ratio of chainring/cog)
        "source": "trainer",
        "physical_chainring": 50,
        "physical_cog": 14,
    },
    "erg": {"start_watts": 200, "step_watts": 5},
    "grade": {"start_pct": 0.0, "step_pct": 0.5},
    "display": {
        "power_avg_s": 3,
        "font_size": 72,         # watts; gear/cadence/speed row is half this
        "speed_units": "kmh",    # kmh | mph
        "front_ring_teeth": 0,   # e.g. 50 shows "(50/23.6)" next to the gear; 0 = off
        "fullscreen": False,     # F11 toggles at runtime
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def geometric_ratios(start: float, end: float, step_pct: float) -> list[float]:
    if start <= 0 or end <= start or step_pct <= 0:
        raise ValueError("generate needs 0 < start < end and step_pct > 0")
    n = math.ceil(math.log(end / start) / math.log(1 + step_pct / 100))
    # Spread evenly so the last gear lands exactly on `end`.
    q = (end / start) ** (1 / n)
    return [round(start * q ** i, 4) for i in range(n + 1)]


def load(path: str | None) -> dict:
    raw = {}
    if path:
        text = Path(path).read_text()
        raw = json.loads(text) if path.endswith(".json") else (yaml.safe_load(text) or {})
    cfg = _merge(DEFAULTS, raw)
    g = cfg["gears"]
    ratios = [float(r) for r in g["ratios"]] if g["ratios"] else geometric_ratios(**g["generate"])
    if sorted(ratios) != ratios:
        raise ValueError("gears.ratios must be ascending (easiest first)")
    g["ratios"] = ratios
    if g["start_gear"]:
        g["start_index"] = max(0, min(len(ratios) - 1, int(g["start_gear"]) - 1))
    else:
        g["start_index"] = min(range(len(ratios)), key=lambda i: abs(ratios[i] - g["start_ratio"]))
    if cfg["trainer"]["mode"] not in ("auto", "native", "ftms_erg"):
        raise ValueError("trainer.mode must be auto, native or ftms_erg")
    if cfg["cadence"]["source"] not in ("trainer", "sensor", "derived"):
        raise ValueError("cadence.source must be trainer, sensor or derived")
    if cfg["display"]["speed_units"] not in ("kmh", "mph"):
        raise ValueError("display.speed_units must be kmh or mph")
    c = cfg["controllers"]
    if c["click_v2_mode"] not in CLICK_V2_MODES:
        raise ValueError("controllers.click_v2_mode must be one of " + ", ".join(CLICK_V2_MODES))
    return cfg


CLICK_V2_MODES = ("right_only", "restart_left", "zwift_unlocked")
# Buttons reported by the left side of a Click v2 (separate pod) or Zwift Ride.
LEFT_BUTTONS = {"left", "up", "right", "down", "shift_up_left", "shift_down_left",
                "powerup_left", "onoff_left"}


def mapped_left_buttons(cfg: dict) -> dict[str, list[str]]:
    """{action: [left-side buttons mapped to it]} for actions that use any."""
    c = cfg["controllers"]
    out = {a: [b for b in c[a] if b in LEFT_BUTTONS] for a in ("shift_up", "shift_down", "toggle_erg")}
    return {a: bs for a, bs in out.items() if bs}
