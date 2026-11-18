# vshift

Virtual shifting with your own gear table, plus a big power display, for smart
trainers on Linux. Works with a Zwift Cog + Click (or Zwift Ride / Play) without
Zwift. Python, bleak (Bluetooth LE), Tkinter.

Status: tested against a built-in simulator and protocol captures only. It has not
been run against a real trainer or Click yet, so expect to look at `-v` logs the
first time.

## Install (Ubuntu 22.04)

    sudo apt install python3-tk python3-venv
    python3 -m venv .venv && . .venv/bin/activate
    pip install -e .
    cp config.example.yaml config.yaml   # edit name / gears

## Run

    vshift -c config.yaml               # real hardware
    vshift -c config.yaml --simulate    # no Bluetooth; rider holds --sim-watts (default 220)
    vshift -c config.yaml -v            # log raw BLE frames

Keys: Up/k harder, Down/j easier, e toggle ERG, +/- ERG watts, PgUp/PgDn grade, q quit.
Click / Ride buttons map through `controllers.shift_up` etc. in the config.

## How shifting works

`trainer.mode`:

- `native` – speaks the Zwift trainer protocol (service `00000001-19ca-...`): RideOn
  handshake, then sends each gear ratio straight to the trainer, which handles the
  resistance itself. This is what Zwift does on "Zwift Ready" trainers.
- `ftms_erg` – plain FTMS. The app models road speed from your cadence x gear ratio
  and sends the power that speed would need on the current grade as an ERG target
  (twice a second). Works on any FTMS trainer, but reacts a bit slower.
- `auto` – try native, fall back to `ftms_erg` if the trainer doesn't answer RideOn.

If native gears feel uniformly too hard/easy, adjust `trainer.native_ratio_scale`.

Gear ratios are chainring/cog, e.g. 50/14 = 3.57. The default ladder goes from
1.2 to 4.8 in 3.5% steps (42 gears); at 90 rpm a 3.5% step is ~3 rpm, so you can
always land within ~1.6 rpm of target. The yellow line suggests the gear that gets
you closest to `cadence.target` at your current power: new cadence ≈ cadence x
current_ratio / new_ratio.

ERG mode sets power exactly, so cadence is whatever you pedal; the gear only matters
in SIM mode.

## Cadence

`cadence.source`: `trainer` (from the trainer's data), `sensor` (a BLE CSC cadence
sensor; set `cadence_sensor.enabled`), or `derived` (from flywheel speed and the
physical ring/cog the chain sits on). The Van Rysel D100 reports broken cadence, so
use `sensor` or `derived` there.

## Limitations

- Only one app can control the trainer at a time. Close Zwift / phone apps first.
- Click v2 needs the daily unlock from the Zwift Companion/Zwift app (open it once,
  pair, close). The app sends the known unlock frame but cannot replace that.
- Click v1 on old firmware uses an encrypted handshake that isn't implemented; update
  the firmware via Zwift Companion.
- Whether Van Rysel trainers accept the native Zwift gear commands is unverified
  (TrainerHUD reports they do); `auto` falls back to FTMS.

## Protocol references

Makinolo's [Zwift trainer protocol notes](https://www.makinolo.com/blog/2024/10/20/zwift-trainer-protocol/),
[TrainerHUD](https://github.com/hugoBourretDesmarais/TrainerHUD),
[serpentine](https://github.com/chrisperkles/serpentine),
[zwiftboard](https://github.com/thomaschampagne/zwiftboard),
[qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift),
[pizero_bikecomputer](https://github.com/hishizuka/pizero_bikecomputer).

## Tests

    pip install pytest && pytest -q
