from vshift import protocol as P
from vshift import config


def test_riding_data_matches_makinolo_capture():
    raw = bytes.fromhex("0308be011050 18bd06 2000 28e2ba01 308beb01".replace(" ", ""))
    assert raw[0] == P.OP_TRAINER_DATA
    r = P.parse_zwift_riding_data(raw[1:])
    assert (r.power, r.cadence, r.speed_kmh) == (190, 80, 8.29)


def test_weights_command_matches_makinolo_capture():
    # 04 2a 08 10 00 20 81 05 28 c4 3b = Physical{gear=0, bike=6.41kg, rider=76.2kg}
    assert P.zwift_gear(0, 6.41, 76.2) == bytes.fromhex("042a08100020810528c43b")


def test_gear_command_roundtrip():
    msg = P.zwift_gear(2.4)
    assert msg[0] == 0x04
    (num, wt, body), = P.pb_decode(msg[1:])
    assert (num, wt) == (5, 2)
    assert P.pb_decode(body) == [(2, 0, 24000)]


def test_simulation_negative_grade_is_zigzag():
    body = P.pb_decode(P.zwift_simulation(-1.5, full=False)[1:])[0][2]
    (num, wt, v), = P.pb_decode(body)
    assert P._unzigzag(v) == -150


def test_click_v2_bitmap_inverted():
    # all bits set except PLUS (0x1000) -> plus pressed
    bitmap = 0xFFFFFFFF & ~0x1000
    frame = bytes([P.OP_CONTROLLER]) + P.pb_uint(1, bitmap)
    assert P.controller_buttons(frame) == {"shift_up_right"}


def test_click_v1_keypad():
    frame = bytes([P.OP_CLICK_KEYPAD]) + P.pb_uint(1, 1) + P.pb_uint(2, 0)
    assert P.controller_buttons(frame) == {"click_minus"}


def test_ftms_encoders():
    assert P.ftms_target_power(250) == bytes([0x05, 0xFA, 0x00])
    assert P.ftms_simulation(-2.0) == bytes([0x11, 0, 0, 0x38, 0xFF, 40, 51])


def test_indoor_bike_data():
    # flags: speed present (bit0=0), cadence (0x04), power (0x40)
    d = (0x0044).to_bytes(2, "little") + (3000).to_bytes(2, "little") \
        + (180).to_bytes(2, "little") + (215).to_bytes(2, "little", signed=True)
    r = P.parse_indoor_bike_data(d)
    assert (r.speed_kmh, r.cadence, r.power) == (30.0, 90.0, 215)


def test_crank_tracker():
    c = P.CrankTracker()
    c.update(10, 0)
    assert round(c.update(11, 683)) == 90  # 1 rev in 683/1024 s


def test_geometric_ratios_step():
    r = config.geometric_ratios(1.2, 4.8, 3.5)
    assert r[0] == 1.2 and r[-1] == 4.8
    steps = [b / a - 1 for a, b in zip(r, r[1:])]
    assert max(steps) <= 0.035 + 1e-3


def test_suggest_gear():
    ratios = [2.0, 2.2, 2.4, 2.6]
    # at 80 rpm in 2.4, gear 2.2 gives 87.3 and 2.0 gives 96 -> 2.2 closest to 90
    assert P.suggest_gear(ratios, 2, 80, 90) == 1


def test_config_example_loads():
    cfg = config.load("config.example.yaml")
    assert cfg["trainer"]["name"] == "D500"
    assert len(cfg["gears"]["ratios"]) > 30


def test_cda_config_changes_physics(tmp_path):
    from vshift.core import Session
    f = tmp_path / "c.yaml"
    f.write_text("physics: {cda: 0.25, trainer_cwa: 0.41}\n")
    cfg = config.load(str(f))
    s = Session(cfg)
    assert s.physics.cda == 0.25
    assert s.physics.power(10) < Session(config.load(None)).physics.power(10)
    body = P.pb_decode(P.zwift_simulation(0, cwa=cfg["physics"]["trainer_cwa"])[1:])[0][2]
    assert (3, 0, 4100) in P.pb_decode(body)
