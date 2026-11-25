"""Bluetooth LE backend (bleak / BlueZ on Linux).

One scanner finds the trainer, Zwift controllers and an optional cadence
sensor; each device then gets its own connect/reconnect task.
"""
from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import protocol as P
from .config import LEFT_BUTTONS
from .core import Session

log = logging.getLogger("vshift")

SCAN_RESTART_S = 10
CONTROLLER_TYPES = {"click_v1", "click_v2_left", "click_v2_right", "play_left", "play_right",
                    "play", "ride_left", "ride_right"}


def _can_respond(client: BleakClient, uuid: str) -> bool:
    ch = client.services.get_characteristic(uuid)
    return ch is not None and "write" in ch.properties


async def _write(client: BleakClient, uuid: str, data: bytes) -> None:
    log.debug("-> %s %s", uuid[4:8], data.hex(" "))
    await client.write_gatt_char(uuid, data, response=_can_respond(client, uuid))


# ===================================================================== trainer


class BleTrainer:
    def __init__(self, session: Session, device: BLEDevice):
        self.session, self.device = session, device
        self.client: BleakClient | None = None
        self.native = False
        self.has_ftms = False
        self._rideon = asyncio.Event()
        self._ftms_lock = asyncio.Lock()
        self._ftms_reply: asyncio.Future | None = None
        self._ftms_granted = False
        self._grade = 0.0

    def _drag(self) -> dict:
        ph = self.session.cfg["physics"]
        return {"cwa": ph["trainer_cwa"], "crr": ph["crr"]}

    # ---------- Trainer protocol (called by Session)

    async def set_gear(self, ratio: float) -> None:
        if self.native:
            await self._zwift(P.zwift_gear(ratio))

    async def set_grade(self, grade_pct: float) -> None:
        self._grade = grade_pct
        if self.native:
            await self._zwift(P.zwift_simulation(grade_pct, full=True, **self._drag()))
        elif self.has_ftms:
            d = self._drag()
            await self._ftms(P.ftms_simulation(grade_pct, crr=d["crr"], cw=d["cwa"]))

    async def set_erg(self, watts: int | None) -> None:
        if watts is None:
            return  # Session re-applies gear/grade (sim mode) right after
        await self.set_power_target(watts)

    async def set_power_target(self, watts: int) -> None:
        if self.has_ftms:
            await self._ftms(P.ftms_target_power(watts))
        elif self.native:
            await self._zwift(P.zwift_target_power(watts))

    # ---------- writes

    async def _zwift(self, data: bytes) -> None:
        if self.client and self.client.is_connected:
            try:
                await _write(self.client, P.ZWIFT_SYNC_RX, data)
            except Exception as e:  # noqa: BLE001 - log and carry on, reconnect handles the rest
                log.warning("trainer zwift write failed: %s", e)

    async def _ftms(self, cmd: bytes) -> None:
        """FTMS control point is request/response: one command in flight at a time."""
        if not (self.client and self.client.is_connected):
            return
        async with self._ftms_lock:
            for c in ([P.ftms_request_control(), P.ftms_start()] if not self._ftms_granted else []) + [cmd]:
                loop = asyncio.get_running_loop()
                self._ftms_reply = loop.create_future()
                try:
                    await _write(self.client, P.FTMS_CONTROL_POINT, c)
                    res = await asyncio.wait_for(self._ftms_reply, 2.5)
                except asyncio.TimeoutError:
                    log.warning("FTMS op 0x%02x: no response", c[0])
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("FTMS write failed: %s", e)
                    return
                if c[0] == 0x00:
                    self._ftms_granted = res == 0x01
                if res != 0x01:
                    log.warning("FTMS op 0x%02x result %d", c[0], res)

    # ---------- notifications

    def _on_ftms_cp(self, _, data: bytearray) -> None:
        if len(data) >= 3 and data[0] == 0x80 and self._ftms_reply and not self._ftms_reply.done():
            self._ftms_reply.set_result(data[2])

    def _on_ibd(self, _, data: bytearray) -> None:
        r = P.parse_indoor_bike_data(bytes(data))
        if not r:
            return
        if self.native:
            # Power/cadence come from the Zwift riding-data stream; FTMS speed is
            # still the real flywheel speed, which "derived" cadence needs.
            self.session.on_trainer_data(wheel_kmh=r.speed_kmh)
        else:
            self.session.on_trainer_data(power=r.power, cadence=r.cadence, hr=r.hr, wheel_kmh=r.speed_kmh)

    def _on_cps(self, _, data: bytearray) -> None:
        if len(data) >= 4 and not self.native and not self.has_ftms:
            self.session.on_trainer_data(power=int.from_bytes(data[2:4], "little", signed=True))

    def _on_zwift_tx(self, _, data: bytearray) -> None:
        log.debug("trainer <-(sync) %s", bytes(data).hex(" "))
        if bytes(data[:6]) == P.RIDE_ON:
            self._rideon.set()

    def _on_zwift_async(self, _, data: bytearray) -> None:
        if data and data[0] == P.OP_TRAINER_DATA:
            r = P.parse_zwift_riding_data(bytes(data[1:]))
            self.session.on_trainer_data(power=r.power, cadence=r.cadence, speed_kmh=r.speed_kmh, hr=r.hr)

    # ---------- lifecycle

    async def run(self) -> None:
        st = self.session.state
        while True:
            st.trainer_status = f"connecting to {self.device.name or self.device.address}"
            disconnected = asyncio.Event()
            try:
                async with BleakClient(self.device, disconnected_callback=lambda _: disconnected.set(),
                                       timeout=20) as client:
                    self.client = client
                    await self._setup(client)
                    st.trainer_status = f"connected: {self.device.name or self.device.address}"
                    await self.session.on_trainer_ready(self)
                    while not disconnected.is_set():
                        try:
                            await asyncio.wait_for(disconnected.wait(), 5)
                        except asyncio.TimeoutError:
                            if self.native and not st.erg:  # keep-alive, like Zwift does
                                await self._zwift(P.zwift_simulation(self._grade, full=False))
            except Exception as e:  # noqa: BLE001
                log.warning("trainer: %s", e)
            self.client, self.native, self._ftms_granted = None, False, False
            st.trainer_status = "disconnected, retrying"
            await asyncio.sleep(3)

    async def _setup(self, client: BleakClient) -> None:
        svc = client.services
        has = lambda u: svc.get_characteristic(u) is not None  # noqa: E731
        self.has_ftms = has(P.FTMS_CONTROL_POINT)
        if has(P.FTMS_INDOOR_BIKE_DATA):
            await client.start_notify(P.FTMS_INDOOR_BIKE_DATA, self._on_ibd)
        if self.has_ftms:
            await client.start_notify(P.FTMS_CONTROL_POINT, self._on_ftms_cp)
        if has(P.CPS_MEASUREMENT):
            await client.start_notify(P.CPS_MEASUREMENT, self._on_cps)
        log.info("trainer services: FTMS=%s Zwift=%s", self.has_ftms, has(P.ZWIFT_SYNC_RX))

        if self.session.cfg["trainer"]["mode"] == "ftms_erg" or not has(P.ZWIFT_SYNC_RX):
            return
        await client.start_notify(P.ZWIFT_ASYNC, self._on_zwift_async)
        await client.start_notify(P.ZWIFT_SYNC_TX, self._on_zwift_tx)
        for attempt in range(3):
            self._rideon.clear()
            await _write(client, P.ZWIFT_SYNC_RX, P.RIDE_ON_TRAINER)
            try:
                await asyncio.wait_for(self._rideon.wait(), 4)
                break
            except asyncio.TimeoutError:
                log.info("trainer: no RideOn reply (attempt %d)", attempt + 1)
        else:
            log.warning("trainer ignored the Zwift handshake; falling back to FTMS")
            return
        self.native = True
        r = self.session.cfg["rider"]
        # Init sequence mirrors what Zwift sends (per TrainerHUD / Makinolo captures).
        for msg in (P.UNKNOWN_41,
                    P.zwift_gear(self.session.native_ratio(), r["bike_kg"], r["rider_kg"]),
                    P.GET_DEVICE_INFO,
                    P.zwift_simulation(self.session.state.grade_pct, full=True, **self._drag())):
            await _write(client, P.ZWIFT_SYNC_RX, msg)
            await asyncio.sleep(0.15)
        log.info("trainer accepted RideOn: native virtual shifting active")


# ===================================================================== controllers


class BleController:
    ACK_INTERVAL_S = 15      # harmless no-solver ack, as zwiftboard does
    KEEPALIVE_S = 20
    RESET_GRACE_S = 15       # if the pod doesn't drop after 0x18, reconnect ourselves
    QUIET_BEFORE_RESET_S = 1.5  # don't reboot the pod in the middle of a press

    def __init__(self, session: Session, device: BLEDevice, kind: str):
        self.session, self.device, self.kind = session, device, kind
        self.label = f"{kind} {device.address[-5:]}"
        self._pressed: set[str] = set()
        self._rideon = asyncio.Event()
        self._encrypted = False
        self._client: BleakClient | None = None
        self._last_press = 0.0
        c = session.cfg["controllers"]
        self.click_v2 = kind.startswith("click_v2")
        self.restart_left = kind == "click_v2_left" and c["click_v2_mode"] == "restart_left"
        # The Click v2 left pod is the pair's BLE anchor: it relays the right pod's
        # presses as well as its own, so each pod only acts on its own side's buttons
        # (otherwise one press would shift twice). In right_only mode the left pod
        # stays connected to keep the pair's link up, but none of its buttons act.
        if kind == "click_v2_left":
            self.own = set() if c["click_v2_mode"] == "right_only" else set(LEFT_BUTTONS)
        elif kind == "click_v2_right":
            self.own = set(P.RIDE_BITS) - LEFT_BUTTONS
        else:
            self.own = None  # Click v1 / Play / Ride: accept everything
        self.actions = {**{b: "up" for b in c["shift_up"]}, **{b: "down" for b in c["shift_down"]},
                        **{b: "erg" for b in c["toggle_erg"]}}

    def _status(self, s: str) -> None:
        self.session.state.controllers[self.label] = s

    def _send(self, data: bytes) -> None:
        """Fire-and-forget write from a notification callback."""
        if self._client is not None:
            asyncio.create_task(_write(self._client, P.ZWIFT_SYNC_RX, data))

    def _on_tx(self, _, data: bytearray) -> None:
        d = bytes(data)
        log.debug("%s <-(sync) %s", self.label, d.hex(" "))
        if d[:6] == P.RIDE_ON:
            # A reply carrying a 64-byte public key means old firmware that wants
            # the encrypted (ECDH + AES-CCM) handshake, which isn't implemented.
            self._encrypted = len(d) > 8 and not self.click_v2
            self._rideon.set()

    def _on_async(self, _, data: bytearray) -> None:
        d = bytes(data)
        if P.is_unlock_challenge(d):
            # A locked pod answers the handshake with a challenge instead of RideOn.
            # We can't produce Zwift's signed reply; the bare ack is the fallback.
            log.debug("%s unlock challenge %s", self.label, d.hex(" "))
            self._rideon.set()
            self._send(P.CLICK_V2_ACK)
            return
        pressed = P.controller_buttons(d)
        if pressed is None:
            return
        if self.own is not None:
            pressed &= self.own
        if pressed:
            self._last_press = time.monotonic()
        for b in pressed - self._pressed:  # act on press edges only
            act = self.actions.get(b)
            log.debug("%s button %s -> %s", self.label, b, act)
            if act == "up":
                asyncio.create_task(self.session.shift(+1))
            elif act == "down":
                asyncio.create_task(self.session.shift(-1))
            elif act == "erg":
                asyncio.create_task(self.session.toggle_erg())
        self._pressed = pressed

    async def _handshake(self, client: BleakClient) -> None:
        self._rideon.clear()
        if self.click_v2:
            for frame in P.CLICK_V2_ACTIVATION:
                await _write(client, P.ZWIFT_SYNC_RX, frame)
                await asyncio.sleep(0.1)
        else:
            await _write(client, P.ZWIFT_SYNC_RX, P.RIDE_ON)
        try:
            await asyncio.wait_for(self._rideon.wait(), 5)
        except asyncio.TimeoutError:
            log.warning("%s: no RideOn reply", self.label)
        if self._encrypted:
            log.warning("%s wants the encrypted handshake; update its firmware in "
                        "Zwift Companion", self.label)

    async def _session(self, client: BleakClient, disconnected: asyncio.Event) -> bool:
        """Runs one connection. Returns True if it ended because we rebooted the pod."""
        connected_at = last_ack = last_keep = time.monotonic()
        reset_at = None
        while not disconnected.is_set():
            try:
                await asyncio.wait_for(disconnected.wait(), 1)
                break
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if reset_at is not None:
                if now - reset_at > self.RESET_GRACE_S:
                    log.info("%s didn't drop after reset; reconnecting", self.label)
                    break
                continue
            if self.restart_left:
                left = self.session.cfg["controllers"]["restart_left_s"] - (now - connected_at)
                self._status(f"ready, restart in {max(0, left):.0f}s")
                if left <= 0 and now - self._last_press >= self.QUIET_BEFORE_RESET_S:
                    log.info("%s: scheduled reboot for a fresh unlock window", self.label)
                    await _write(client, P.ZWIFT_SYNC_RX, P.CLICK_V2_RESET)
                    self._status("restarting")
                    reset_at = now
                    continue
            if self.click_v2 and now - last_ack >= self.ACK_INTERVAL_S:
                await _write(client, P.ZWIFT_SYNC_RX, P.CLICK_V2_ACK)
                last_ack = now
            if now - last_keep >= self.KEEPALIVE_S:
                await _write(client, P.ZWIFT_SYNC_RX, P.GET_DEVICE_INFO)
                last_keep = now
        return reset_at is not None

    async def run(self) -> None:
        while True:
            self._status("connecting")
            disconnected = asyncio.Event()
            planned = False
            try:
                async with BleakClient(self.device, disconnected_callback=lambda _: disconnected.set(),
                                       timeout=20) as client:
                    self._client = client
                    await client.start_notify(P.ZWIFT_ASYNC, self._on_async)
                    await client.start_notify(P.ZWIFT_SYNC_TX, self._on_tx)
                    await self._handshake(client)
                    self._status("ready" if self.own != set() else "connected (link only)")
                    log.info("%s ready", self.label)
                    planned = await self._session(client, disconnected)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: %s", self.label, e)
            self._client = None
            self._pressed.clear()
            self._status("restarting" if planned else "disconnected")
            # After a planned reboot the pod re-advertises within a second or two.
            await asyncio.sleep(0.5 if planned else 3)


# ===================================================================== cadence sensor


class BleCadenceSensor:
    def __init__(self, session: Session, device: BLEDevice):
        self.session, self.device = session, device
        self._crank = P.CrankTracker()

    def _on_csc(self, _, data: bytearray) -> None:
        r = P.parse_csc_crank(bytes(data))
        if r:
            self.session.on_sensor_cadence(self._crank.update(*r))

    async def run(self) -> None:
        st = self.session.state
        while True:
            st.sensor_status = "connecting"
            disconnected = asyncio.Event()
            try:
                async with BleakClient(self.device, disconnected_callback=lambda _: disconnected.set(),
                                       timeout=20) as client:
                    await client.start_notify(P.CSC_MEASUREMENT, self._on_csc)
                    st.sensor_status = "connected"
                    await disconnected.wait()
            except Exception as e:  # noqa: BLE001
                log.warning("cadence sensor: %s", e)
            st.sensor_status = "disconnected"
            await asyncio.sleep(3)


# ===================================================================== discovery


def _match(dev: BLEDevice, want_addr: str, want_name: str) -> bool:
    if want_addr:
        return dev.address.lower() == want_addr.lower()
    return bool(want_name) and want_name.lower() in (dev.name or "").lower()


async def discover_and_run(session: Session) -> None:
    cfg = session.cfg
    tcfg, ccfg, scfg = cfg["trainer"], cfg["controllers"], cfg["cadence_sensor"]
    started: set[str] = set()
    trainer_started = sensor_started = False
    tasks: list[asyncio.Task] = []
    zwift_seen: dict[str, str] = {}  # address -> type, for diagnostics
    adv_count = 0

    def on_adv(dev: BLEDevice, adv: AdvertisementData) -> None:
        nonlocal trainer_started, sensor_started, adv_count
        adv_count += 1
        uuids = [u.lower() for u in adv.service_uuids]
        ztype = P.zwift_device_type(adv.manufacturer_data)
        if ztype and zwift_seen.get(dev.address) != ztype:
            zwift_seen[dev.address] = ztype
            log.info("seen Zwift device %s %s (rssi %s)", ztype, dev.address, adv.rssi)
        if dev.address in started:
            return

        if not trainer_started:
            explicit = tcfg["address"] or tcfg["name"]
            is_trainer = (_match(dev, tcfg["address"], tcfg["name"]) if explicit
                          else P.FTMS_SERVICE in uuids or ztype == "hub")
            if is_trainer:
                trainer_started = True
                started.add(dev.address)
                log.info("found trainer %s (%s)", dev.name, dev.address)
                tasks.append(asyncio.create_task(BleTrainer(session, dev).run()))
                return

        if ztype in CONTROLLER_TYPES:
            if not ccfg["enabled"]:
                started.add(dev.address)
                log.info("not using %s %s: controllers.enabled is false", ztype, dev.address)
            elif ccfg["addresses"] and dev.address.lower() not in [a.lower() for a in ccfg["addresses"]]:
                started.add(dev.address)
                log.info("not using %s %s: not in controllers.addresses", ztype, dev.address)
            else:
                started.add(dev.address)
                log.info("found controller %s %s", ztype, dev.address)
                tasks.append(asyncio.create_task(BleController(session, dev, ztype).run()))
            return

        if scfg["enabled"] and not sensor_started:
            explicit = scfg["address"] or scfg["name"]
            if (_match(dev, scfg["address"], scfg["name"]) if explicit else P.CSC_SERVICE in uuids):
                sensor_started = True
                started.add(dev.address)
                log.info("found cadence sensor %s (%s)", dev.name, dev.address)
                tasks.append(asyncio.create_task(BleCadenceSensor(session, dev).run()))

    # Keep scanning for the whole session so a Click woken up later is picked up.
    # Some adapters silently stop LE scanning while they connect to another device,
    # so the scan is restarted every SCAN_RESTART_S seconds.
    await _drop_leftover_connections()
    log.info("scanning... press a button on each Click pod to wake it")
    last_hint = time.monotonic()
    try:
        await _scan_forever(on_adv, ccfg, zwift_seen, lambda: adv_count, last_hint)
    finally:
        # Cancel device tasks so each `async with BleakClient` disconnects cleanly;
        # otherwise bluetoothd keeps the links up after we exit.
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("disconnected all devices")


async def _drop_leftover_connections() -> None:
    from . import bluez
    try:
        devs = await bluez.connected_devices()
    except Exception as e:  # noqa: BLE001
        log.debug("couldn't list BlueZ connections: %s", e)
        return
    for d in devs:
        if d.kind:
            log.info("dropping leftover connection to %s %s (%s)", d.kind, d.address, d.name or "no name")
            try:
                await bluez.disconnect(d.path)
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't disconnect %s: %s", d.address, e)
    if any(d.kind for d in devs):
        await asyncio.sleep(1.5)  # give devices a moment to start advertising again


async def _scan_forever(on_adv, ccfg, zwift_seen, adv_count, last_hint) -> None:
    while True:
        try:
            async with BleakScanner(on_adv):
                await asyncio.sleep(SCAN_RESTART_S)
        except Exception as e:  # noqa: BLE001  (e.g. InProgress while a connect is running)
            log.debug("scan restart failed: %s", e)
            await asyncio.sleep(2)
        log.debug("scan cycle done, %d advertisements so far", adv_count())
        if ccfg["enabled"] and not any(t in CONTROLLER_TYPES for t in zwift_seen.values()) \
                and time.monotonic() - last_hint > 30:
            last_hint = time.monotonic()
            log.info("no Zwift controller seen yet (%d advertisements received); press a "
                     "button on each pod, and make sure no phone/app is connected to it", adv_count())


# ===================================================================== --scan


async def scan(seconds: float = 15) -> None:
    """Print every BLE device seen, flagging trainers, Zwift controllers and sensors."""
    seen: dict[str, tuple] = {}

    def on_adv(dev: BLEDevice, adv: AdvertisementData) -> None:
        uuids = [u.lower() for u in adv.service_uuids]
        kinds = []
        if P.FTMS_SERVICE in uuids:
            kinds.append("FTMS trainer")
        if P.ZWIFT_SERVICE in uuids or P.ZWIFT_SERVICE_FC82 in uuids:
            kinds.append("Zwift service")
        zt = P.zwift_device_type(adv.manufacturer_data)
        if zt:
            kinds.append(f"zwift:{zt}")
        if P.CSC_SERVICE in uuids:
            kinds.append("cadence sensor")
        if P.CPS_SERVICE in uuids:
            kinds.append("power meter")
        row = (dev.name or adv.local_name or "", ", ".join(kinds))
        if seen.get(dev.address) != row:
            if dev.address not in seen or kinds:
                print(f"{dev.address}  rssi {adv.rssi:4}  {row[0]!r:28} {row[1]}", flush=True)
            seen[dev.address] = row

    from . import bluez
    try:
        held = [d for d in await bluez.connected_devices() if d.kind]
    except Exception:  # noqa: BLE001
        held = []
    for d in held:
        print(f"{d.address}  already connected in BlueZ, so not advertising: {d.kind} {d.name!r}"
              " (vshift drops this on start; or: bluetoothctl disconnect " + d.address + ")")
    print(f"scanning for {seconds:.0f} s -- press a button on each Click pod to wake it")
    async with BleakScanner(on_adv):
        await asyncio.sleep(seconds)
    hits = {a: r for a, r in seen.items() if r[1]}
    print(f"\n{len(seen)} devices, {len(hits)} relevant:")
    for a, (name, kinds) in hits.items():
        print(f"  {a}  {name!r:28} {kinds}")
    if not any("zwift:click" in k or "zwift:ride" in k or "zwift:play" in k for _, k in hits.values()):
        print("  no Zwift controller seen: is it awake, and not connected to another app or phone?")
