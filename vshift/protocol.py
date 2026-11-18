"""Pure (no I/O) encoders/decoders for the BLE protocols vshift speaks.

Sources for the reverse-engineered Zwift protocol:
  - Makinolo, "Zwift Trainer protocol" and "Zwift Ride protocol" write-ups
  - TrainerHUD (MIT), serpentine, qdomyos-zwift, BikeControl
FTMS is the public Bluetooth SIG Fitness Machine Service spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- UUIDs

ZWIFT_SERVICE = "00000001-19ca-4651-86e5-fa29dcdd09d1"
ZWIFT_SERVICE_FC82 = "0000fc82-0000-1000-8000-00805f9b34fb"  # newer firmware
ZWIFT_ASYNC = "00000002-19ca-4651-86e5-fa29dcdd09d1"  # notify: data/buttons
ZWIFT_SYNC_RX = "00000003-19ca-4651-86e5-fa29dcdd09d1"  # write: commands
ZWIFT_SYNC_TX = "00000004-19ca-4651-86e5-fa29dcdd09d1"  # indicate: replies
ZWIFT_COMPANY_ID = 0x094A

FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"
CPS_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"
CSC_SERVICE = "00001816-0000-1000-8000-00805f9b34fb"
CSC_MEASUREMENT = "00002a5b-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"

# Byte 2 of Zwift manufacturer data identifies the device.
ZWIFT_DEVICE_TYPES = {
    0x01: "hub",          # trainer (Zwift Hub / Zwift Ready trainers)
    0x02: "play_right",
    0x03: "play_left",
    0x07: "ride_right",
    0x08: "ride_left",
    0x09: "click_v1",
    0x0A: "click_v2_right",
    0x0B: "click_v2_left",
    0x0E: "play",
}

# ---------------------------------------------------------------- messages

RIDE_ON = b"RideOn"
RIDE_ON_TRAINER = RIDE_ON + bytes([0x02, 0x01])
# Click v2 (see github.com/thomaschampagne/zwiftboard internal/zwift/protocol.go):
# activation is RideOn+02 03, 00 08 00, 00 08 10. The left pod then sends an
# FF 03 <challenge> on the async characteristic; Zwift answers ff 04 00 <signed
# reply from Zwift's servers>. The bare ff 04 00 ack does NOT unlock the left pod,
# it only keeps an already-unlocked pod streaming. A lone 0x18 reboots a pod.
CLICK_V2_ACTIVATION = (RIDE_ON + bytes([0x02, 0x03]), bytes([0x00, 0x08, 0x00]), bytes([0x00, 0x08, 0x10]))
CLICK_V2_ACK = bytes([0xFF, 0x04, 0x00])
CLICK_V2_RESET = bytes([0x18])


def is_unlock_challenge(frame: bytes) -> bool:
    return frame[:2] == b"\xff\x03"
GET_DEVICE_INFO = bytes([0x00, 0x08, 0x00])  # also used as controller keep-alive
UNKNOWN_41 = bytes([0x41, 0x08, 0x05])       # sent by Zwift during trainer init

OP_TRAINER_DATA = 0x03
OP_TRAINER_CMD = 0x04
OP_PLAY_KEYPAD = 0x07
OP_IDLE = 0x15
OP_BATTERY = 0x19
OP_CONTROLLER = 0x23  # Ride / Click v2 button bitmap
OP_CLICK_KEYPAD = 0x37  # Click v1

# ---------------------------------------------------------------- protobuf


def _varint(v: int) -> bytes:
    if v < 0:
        v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag(v: int) -> int:
    return (v << 1) ^ (v >> 63)


def _unzigzag(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def pb_uint(num: int, v: int) -> bytes:
    return _varint(num << 3) + _varint(v)


def pb_sint(num: int, v: int) -> bytes:
    return _varint(num << 3) + _varint(_zigzag(v))


def pb_msg(num: int, body: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(body)) + body


def pb_decode(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Minimal protobuf decoder -> [(field, wiretype, value)]. Stops on garbage."""
    out = []
    i, n = 0, len(buf)

    def rd() -> int:
        nonlocal i
        shift = res = 0
        while i < n:
            b = buf[i]
            i += 1
            res |= (b & 0x7F) << shift
            if not b & 0x80:
                return res
            shift += 7
        raise ValueError("truncated varint")

    try:
        while i < n:
            key = rd()
            num, wt = key >> 3, key & 7
            if wt == 0:
                out.append((num, 0, rd()))
            elif wt == 2:
                ln = rd()
                out.append((num, 2, bytes(buf[i:i + ln])))
                i += ln
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            else:
                break
    except ValueError:
        pass
    return out


# ---------------------------------------------------------------- Zwift trainer


def zwift_gear(ratio: float, bike_kg: float | None = None, rider_kg: float | None = None) -> bytes:
    """HubCommand{ Physical{ GearRatioX10000, BikeWeightx100, RiderWeightx100 } }"""
    body = pb_uint(2, max(0, round(ratio * 10000)))
    if bike_kg is not None:
        body += pb_uint(4, round(bike_kg * 100))
    if rider_kg is not None:
        body += pb_uint(5, round(rider_kg * 100))
    return bytes([OP_TRAINER_CMD]) + pb_msg(5, body)


def zwift_weights(bike_kg: float, rider_kg: float) -> bytes:
    body = pb_uint(4, round(bike_kg * 100)) + pb_uint(5, round(rider_kg * 100))
    return bytes([OP_TRAINER_CMD]) + pb_msg(5, body)


def zwift_simulation(grade_pct: float, full: bool = True, cwa: float = 0.51, crr: float = 0.004) -> bytes:
    """HubCommand{ Simulation{ Wind, InclineX100, CWa*1e4, Crr*1e5 } }.
    Scaling differs from FTMS; wrong scaling makes the trainer behave oddly."""
    body = b""
    if full:
        body += pb_sint(1, 0)
    body += pb_sint(2, round(grade_pct * 100))
    if full:
        body += pb_uint(3, round(cwa * 10000)) + pb_uint(4, round(crr * 100000))
    return bytes([OP_TRAINER_CMD]) + pb_msg(4, body)


def zwift_target_power(watts: int) -> bytes:
    return bytes([OP_TRAINER_CMD]) + pb_uint(3, max(0, int(watts)))


@dataclass
class RidingData:
    power: int | None = None
    cadence: int | None = None
    speed_kmh: float | None = None
    hr: int | None = None


def parse_zwift_riding_data(payload: bytes) -> RidingData:
    """payload excludes the 0x03 opcode. SpeedX100 is km/h*100."""
    r = RidingData()
    for num, wt, v in pb_decode(payload):
        if wt != 0:
            continue
        if num == 1:
            r.power = v
        elif num == 2:
            r.cadence = v
        elif num == 3:
            r.speed_kmh = v / 100
        elif num == 4:
            r.hr = v or None
    return r


# ---------------------------------------------------------------- Zwift controllers

# Ride / Click v2 bitmap (opcode 0x23, field 1). A 0 bit means *pressed*.
RIDE_BITS = {
    "left": 0x0001, "up": 0x0002, "right": 0x0004, "down": 0x0008,
    "a": 0x0010, "b": 0x0020, "y": 0x0040, "z": 0x0080,
    "shift_up_left": 0x0100, "shift_down_left": 0x0200,
    "powerup_left": 0x0400, "onoff_left": 0x0800,
    "shift_up_right": 0x1000, "shift_down_right": 0x2000,
    "powerup_right": 0x4000, "onoff_right": 0x8000,
}


def parse_controller_notification(payload: bytes) -> set[str]:
    """Opcode 0x23 (Zwift Ride, Click v2). Returns names of pressed buttons."""
    bitmap = None
    for num, wt, v in pb_decode(payload):
        if num == 1 and wt == 0:
            bitmap = v
    if bitmap is None:
        return set()
    pressed = ~bitmap
    return {name for name, bit in RIDE_BITS.items() if pressed & bit}


def parse_click_keypad(payload: bytes) -> set[str]:
    """Opcode 0x37 (Click v1): field1 = plus, field2 = minus, 0 = pressed."""
    s = set()
    for num, wt, v in pb_decode(payload):
        if wt == 0 and v == 0:
            if num == 1:
                s.add("click_plus")
            elif num == 2:
                s.add("click_minus")
    return s


def parse_play_keypad(payload: bytes) -> set[str]:
    """Opcode 0x07 (Zwift Play). Field 1: 1 = left unit. Fields 2..7 buttons, 0 = pressed."""
    raw, left = {}, False
    for num, wt, v in pb_decode(payload):
        if wt != 0:
            continue
        if num == 1:
            left = v == 1
        else:
            raw[num] = v
    names = (["up", "left", "right", "down", "shift_up_left", "onoff_left"] if left
             else ["y", "z", "a", "b", "shift_up_right", "onoff_right"])
    return {names[n - 2] for n in range(2, 8) if raw.get(n) == 0}


def controller_buttons(data: bytes) -> set[str] | None:
    """Dispatch an async-characteristic frame; None if it isn't a button frame."""
    if not data:
        return None
    op, payload = data[0], data[1:]
    if op == OP_CONTROLLER:
        return parse_controller_notification(payload)
    if op == OP_CLICK_KEYPAD:
        return parse_click_keypad(payload)
    if op == OP_PLAY_KEYPAD:
        return parse_play_keypad(payload)
    return None


def zwift_device_type(manufacturer_data: dict[int, bytes]) -> str | None:
    """bleak gives {company_id: bytes-after-company-id}. Device type is first byte."""
    d = manufacturer_data.get(ZWIFT_COMPANY_ID)
    if not d:
        return None
    return ZWIFT_DEVICE_TYPES.get(d[0], f"unknown_0x{d[0]:02x}")


# ---------------------------------------------------------------- FTMS


def ftms_request_control() -> bytes:
    return bytes([0x00])


def ftms_start() -> bytes:
    return bytes([0x07])


def ftms_target_power(watts: int) -> bytes:
    return bytes([0x05]) + int(max(-32768, min(32767, watts))).to_bytes(2, "little", signed=True)


def ftms_simulation(grade_pct: float, wind_mps: float = 0.0, crr: float = 0.004, cw: float = 0.51) -> bytes:
    return (bytes([0x11])
            + round(wind_mps * 1000).to_bytes(2, "little", signed=True)
            + round(grade_pct * 100).to_bytes(2, "little", signed=True)
            + bytes([min(255, round(crr * 10000)), min(255, round(cw * 100))]))


@dataclass
class IndoorBikeData:
    speed_kmh: float | None = None
    cadence: float | None = None
    power: int | None = None
    hr: int | None = None


def parse_indoor_bike_data(d: bytes) -> IndoorBikeData | None:
    if len(d) < 2:
        return None
    flags = int.from_bytes(d[0:2], "little")
    r = IndoorBikeData()
    i = 2

    def u16(k):
        return int.from_bytes(d[k:k + 2], "little") if k + 2 <= len(d) else None

    def s16(k):
        return int.from_bytes(d[k:k + 2], "little", signed=True) if k + 2 <= len(d) else None

    if not flags & 0x0001:  # "more data" bit clear => instantaneous speed present
        v = u16(i)
        r.speed_kmh = v / 100 if v is not None else None
        i += 2
    if flags & 0x0002:
        i += 2
    if flags & 0x0004:
        v = u16(i)
        r.cadence = v / 2 if v is not None else None
        i += 2
    if flags & 0x0008:
        i += 2
    if flags & 0x0010:
        i += 3
    if flags & 0x0020:
        i += 2
    if flags & 0x0040:
        r.power = s16(i)
        i += 2
    if flags & 0x0080:
        i += 2
    if flags & 0x0100:
        i += 5
    if flags & 0x0200:
        r.hr = d[i] if i < len(d) else None
    return r


@dataclass
class CrankTracker:
    """Turns cumulative crank revs + 1/1024 s event time (CSC/CPS) into rpm."""
    last_revs: int | None = None
    last_time: int | None = None
    stale_events: int = 0
    rpm: float = 0.0

    def update(self, revs: int, t1024: int) -> float:
        if self.last_revs is None:
            self.last_revs, self.last_time = revs, t1024
            return self.rpm
        dr = (revs - self.last_revs) & 0xFFFF
        dt = (t1024 - self.last_time) & 0xFFFF
        if dr == 0 or dt == 0:
            self.stale_events += 1
            if self.stale_events >= 3:  # ~3 s without a crank event -> stopped
                self.rpm = 0.0
        else:
            self.stale_events = 0
            self.rpm = dr * 60 * 1024 / dt
        self.last_revs, self.last_time = revs, t1024
        return self.rpm


def parse_csc_crank(d: bytes) -> tuple[int, int] | None:
    if not d:
        return None
    flags, i = d[0], 1
    if flags & 0x01:
        i += 6
    if flags & 0x02 and i + 4 <= len(d):
        return int.from_bytes(d[i:i + 2], "little"), int.from_bytes(d[i + 2:i + 4], "little")
    return None


def parse_hr(d: bytes) -> int | None:
    if len(d) < 2:
        return None
    return int.from_bytes(d[1:3], "little") if d[0] & 1 else d[1]


# ---------------------------------------------------------------- physics


@dataclass
class Physics:
    rider_kg: float = 75.0
    bike_kg: float = 9.0
    cda: float = 0.32
    crr: float = 0.004
    rho: float = 1.2
    drivetrain_eff: float = 0.97
    wheel_circ_m: float = 2.105

    def speed_mps(self, cadence: float, ratio: float) -> float:
        return cadence / 60.0 * ratio * self.wheel_circ_m

    def power(self, v: float, grade_pct: float = 0.0) -> float:
        import math
        m, g = self.rider_kg + self.bike_kg, 9.81
        th = math.atan(grade_pct / 100)
        f = m * g * (self.crr * math.cos(th) + math.sin(th)) + 0.5 * self.rho * self.cda * v * v
        return max(0.0, f * v / self.drivetrain_eff)


def suggest_gear(ratios: list[float], cur_idx: int, cadence: float, target_cadence: float) -> int:
    """Gear that would put cadence closest to target at the current road speed.
    Speed (set by power) stays ~constant across a shift, so cadence scales with
    the inverse of the ratio: c' = c * r / r'."""
    if cadence <= 1:
        return cur_idx
    r = ratios[cur_idx]
    return min(range(len(ratios)), key=lambda j: abs(cadence * r / ratios[j] - target_cadence))
