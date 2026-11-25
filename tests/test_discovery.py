import asyncio
from types import SimpleNamespace

from vshift import ble, config, protocol as P
from vshift.core import Session


def adv(uuids=(), mfr=None, rssi=-60):
    return SimpleNamespace(service_uuids=list(uuids), manufacturer_data=mfr or {}, rssi=rssi,
                           local_name=None)


def test_click_found_after_trainer(monkeypatch):
    started = []

    class FakeScanner:
        def __init__(self, cb):
            self.cb = cb

        async def __aenter__(self):
            # trainer first, then the left pod (company 0x094A, type 0x0B), then the right pod
            self.cb(SimpleNamespace(address="D2:D5", name="D500V2-5362"),
                    adv([P.FTMS_SERVICE], {P.ZWIFT_COMPANY_ID: b"\x01"}))
            self.cb(SimpleNamespace(address="F4:C4", name=None), adv([P.ZWIFT_SERVICE], {P.ZWIFT_COMPANY_ID: b"\x0b"}))
            self.cb(SimpleNamespace(address="F4:C4", name=None), adv([P.ZWIFT_SERVICE], {P.ZWIFT_COMPANY_ID: b"\x0b"}))
            self.cb(SimpleNamespace(address="E1:E1", name=None), adv([], {P.ZWIFT_COMPANY_ID: b"\x0a"}))
            return self

        async def __aexit__(self, *a):
            pass

    async def fake_run(self):
        started.append((type(self).__name__, self.device.address))

    monkeypatch.setattr(ble, "BleakScanner", FakeScanner)
    monkeypatch.setattr(ble, "SCAN_RESTART_S", 0.01)
    monkeypatch.setattr(ble.BleTrainer, "run", fake_run)
    monkeypatch.setattr(ble.BleController, "run", fake_run)
    cfg = config.load("config.example.yaml")

    async def go():
        try:
            await asyncio.wait_for(ble.discover_and_run(Session(cfg)), 0.1)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    assert started == [("BleTrainer", "D2:D5"), ("BleController", "F4:C4"), ("BleController", "E1:E1")]
