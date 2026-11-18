import asyncio
import logging
from types import SimpleNamespace

import pytest

from vshift import ble, config, protocol as P
from vshift.app import main  # noqa: F401  (import check)
from vshift.core import Session


def make_cfg(**ctrl):
    cfg = config.load(None)
    cfg["controllers"].update(ctrl)
    return cfg


def test_default_mapping_is_right_pod_only():
    cfg = config.load(None)
    assert cfg["controllers"]["click_v2_mode"] == "right_only"
    assert config.mapped_left_buttons(cfg) == {}


def test_left_buttons_detected():
    cfg = make_cfg(shift_down=["click_minus", "shift_up_left"], toggle_erg=["up", "a"])
    assert config.mapped_left_buttons(cfg) == {"shift_down": ["shift_up_left"], "toggle_erg": ["up"]}


def test_bad_mode_rejected(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("controllers: {click_v2_mode: both}\n")
    with pytest.raises(ValueError):
        config.load(str(f))


def test_challenge_detection():
    assert P.is_unlock_challenge(bytes.fromhex("ff0300aabb"))
    assert not P.is_unlock_challenge(bytes.fromhex("ff0400"))
    assert not P.is_unlock_challenge(bytes.fromhex("2308ffff"))


class FakeClient:
    pass


def run_session(kind, mode, restart_s, seconds, press_times=()):
    """Drive BleController._session with a fake clock-free client; return writes."""
    writes = []

    async def fake_write(_client, _uuid, data):
        writes.append((round(asyncio.get_running_loop().time() - t0), bytes(data)))

    async def go():
        nonlocal t0
        cfg = make_cfg(click_v2_mode=mode, restart_left_s=restart_s)
        c = ble.BleController(Session(cfg), SimpleNamespace(address="AA:BB:CC:DD:EE:FF"), kind)
        c.RESET_GRACE_S = 2
        t0 = asyncio.get_running_loop().time()
        disc = asyncio.Event()
        return await asyncio.wait_for(c._session(FakeClient(), disc), seconds)

    t0 = 0.0
    orig = ble._write
    ble._write = fake_write
    try:
        planned = asyncio.run(go())
    except asyncio.TimeoutError:
        planned = None
    finally:
        ble._write = orig
    return planned, writes


def test_restart_left_sends_reset_then_reconnects():
    planned, writes = run_session("click_v2_left", "restart_left", 2, 8)
    assert planned is True                      # session ended because of our reset
    resets = [t for t, d in writes if d == P.CLICK_V2_RESET]
    assert resets == [2]


def test_right_pod_never_reset():
    planned, writes = run_session("click_v2_right", "restart_left", 2, 4)
    assert planned is None                      # still running when we stopped it
    assert P.CLICK_V2_RESET not in [d for _, d in writes]


def test_zwift_unlocked_never_resets_left():
    planned, writes = run_session("click_v2_left", "zwift_unlocked", 2, 4)
    assert planned is None
    assert P.CLICK_V2_RESET not in [d for _, d in writes]


def test_right_only_warns_on_left_mapping(tmp_path, caplog, monkeypatch):
    f = tmp_path / "c.yaml"
    f.write_text("controllers: {shift_down: [shift_up_left]}\n")
    import vshift.app as app
    monkeypatch.setattr(app.tk, "Tk", lambda: (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(app.threading, "Thread", lambda **k: SimpleNamespace(start=lambda: None))
    with caplog.at_level(logging.WARNING, logger="vshift"), pytest.raises(SystemExit):
        app.main(["-c", str(f), "--simulate"])
    assert "shift_up_left" in caplog.text and "right_only" in caplog.text


def test_speed_units_validated(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("display: {speed_units: furlongs}\n")
    with pytest.raises(ValueError):
        config.load(str(f))
