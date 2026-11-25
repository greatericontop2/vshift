"""Entry point: Tk GUI on the main thread, asyncio + BLE on a worker thread."""
from __future__ import annotations

import argparse
import asyncio
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

from . import config
from .core import Session

log = logging.getLogger("vshift")

KEYS_HELP = ("Up/k: harder   Down/j: easier   e: ERG on/off   +/-: ERG watts   "
             "PgUp/PgDn: grade   F11: fullscreen   q: quit")
KMH_PER_MPH = 1.609344


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


class Gui:
    def __init__(self, root: tk.Tk, session: Session, loop: asyncio.AbstractEventLoop, logq: queue.Queue):
        self.root, self.s, self.loop, self.logq = root, session, loop, logq
        root.title("vshift")
        root.configure(bg="#111")
        big = session.cfg["display"]["font_size"]
        f_big = tkfont.Font(family="DejaVu Sans", size=big, weight="bold")
        f_mid = tkfont.Font(family="DejaVu Sans", size=max(12, big // 2), weight="bold")
        f_gear_sub = tkfont.Font(family="DejaVu Sans", size=max(10, big * 3 // 10), weight="bold")
        f_small = tkfont.Font(family="DejaVu Sans Mono", size=max(10, big // 6))
        fg, dim = "#f2f2f2", "#9a9a9a"

        def lab(parent, text, f, color=fg, **kw):
            w = tk.Label(parent, text=text, font=f, fg=color, bg="#111", **kw)
            return w

        top = tk.Frame(root, bg="#111")
        top.pack(fill="x", padx=16, pady=(12, 0))
        self.power = lab(top, "--- W", f_big)
        self.power.pack(side="left")
        self.power_sub = lab(top, "", f_small, dim, justify="left")
        self.power_sub.pack(side="left", padx=12, anchor="s", pady=(0, 10))

        row = tk.Frame(root, bg="#111")
        row.pack(fill="x", padx=16, pady=4)
        self.cad = lab(row, "-- rpm", f_mid)
        self.cad.pack(side="left")
        self.gear = lab(row, "", f_mid, "#7fd3ff")
        self.gear.pack(side="left", padx=(28, 0))
        # Ratio and chainring/cog equivalent, in smaller text on the same baseline
        self.gear_sub = lab(row, "", f_gear_sub, "#7fd3ff")
        self.gear_sub.pack(side="left", padx=(10, 28), anchor="s", pady=(0, big // 18))
        self.speed = lab(row, "", f_mid, dim)
        self.speed.pack(side="left")

        self.mode = lab(root, "", f_small, fg, anchor="w")
        self.mode.pack(fill="x", padx=16)
        self.hint = lab(root, "", f_small, "#ffd27f", anchor="w")
        self.hint.pack(fill="x", padx=16)
        self.status = lab(root, "", f_small, dim, anchor="w", justify="left")
        self.status.pack(fill="x", padx=16, pady=(6, 0))
        lab(root, KEYS_HELP, f_small, dim, anchor="w").pack(fill="x", padx=16, pady=(4, 0))

        self.logbox = tk.Text(root, height=7, bg="#1b1b1b", fg=dim, font=f_small,
                              relief="flat", state="disabled")
        self.logbox.pack(fill="both", expand=True, padx=16, pady=10)

        bind = {"<Up>": ("shift", 1), "k": ("shift", 1), "<Down>": ("shift", -1), "j": ("shift", -1),
                "e": ("toggle_erg",), "+": ("adjust_erg", 1), "=": ("adjust_erg", 1),
                "-": ("adjust_erg", -1), "<Prior>": ("adjust_grade", 1), "<Next>": ("adjust_grade", -1)}
        for key, (meth, *args) in bind.items():
            root.bind(key, lambda _e, m=meth, a=args: self.call(m, *a))
        root.bind("q", lambda _e: root.destroy())
        root.bind("<F11>", lambda _e: root.attributes("-fullscreen", not root.attributes("-fullscreen")))
        if session.cfg["display"]["fullscreen"]:
            root.attributes("-fullscreen", True)
        self.refresh()

    def call(self, meth: str, *args) -> None:
        asyncio.run_coroutine_threadsafe(getattr(self.s, meth)(*args), self.loop)

    def refresh(self) -> None:
        st, ratios = self.s.state, self.s.ratios
        avg = self.s.cfg["display"]["power_avg_s"]
        self.power.config(text=f"{st.power_avg:4.0f} W" if st.power_avg is not None else "--- W")
        self.power_sub.config(text=f"{avg}s avg\nnow {st.power if st.power is not None else '-'} W"
                              + (f"\nHR {st.hr}" if st.hr else ""))
        self.cad.config(text=f"{st.cadence:3.0f} rpm" if st.cadence is not None else "-- rpm")
        disp = self.s.cfg["display"]
        self.gear.config(text=f"gear {st.gear_index + 1}/{st.gear_count}")
        sub = f"({st.ratio:.2f})"
        if disp["front_ring_teeth"]:
            ring = disp["front_ring_teeth"]
            sub += f" ({ring}/{ring / st.ratio:.1f})"  # cog teeth that give this ratio
        self.gear_sub.config(text=sub)
        if st.speed_kmh is None:
            self.speed.config(text="")
        elif disp["speed_units"] == "mph":
            self.speed.config(text=f"{st.speed_kmh / KMH_PER_MPH:4.1f} mph")
        else:
            self.speed.config(text=f"{st.speed_kmh:4.1f} km/h")
        mode = f"ERG {st.erg_watts} W" if st.erg else f"SIM  grade {st.grade_pct:+.1f}%"
        if self.s.emulating and not st.erg and st.emulated_target is not None:
            mode += f"   (emulated target {st.emulated_target} W)"
        self.mode.config(text=f"{mode}   |   trainer: {st.trainer_mode}")
        tgt = self.s.cfg["cadence"]["target"]
        if st.suggested_index is not None and st.cadence:
            j = st.suggested_index
            pred = st.cadence * st.ratio / ratios[j]
            d = j - st.gear_index
            how = "stay" if d == 0 else f"{'up' if d > 0 else 'down'} {abs(d)}"
            self.hint.config(text=f"target {tgt} rpm -> gear {j + 1} ({how}), predicted {pred:.0f} rpm")
        else:
            self.hint.config(text="")
        ctrl = ", ".join(f"{k}: {v}" for k, v in st.controllers.items()) or "none yet"
        self.status.config(text=f"trainer: {st.trainer_status}\ncontrollers: {ctrl}"
                           + (f"\ncadence sensor: {st.sensor_status}"
                              if self.s.cfg["cadence_sensor"]["enabled"] else ""))
        lines = []
        while not self.logq.empty():
            lines.append(self.logq.get_nowait())
        if lines:
            self.logbox.config(state="normal")
            self.logbox.insert("end", "\n".join(lines) + "\n")
            self.logbox.delete("1.0", "end-300l")
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.root.after(200, self.refresh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Virtual shifting + power display for smart trainers")
    ap.add_argument("-c", "--config", help="YAML or JSON config file")
    ap.add_argument("--simulate", action="store_true", help="no Bluetooth: fake trainer and rider")
    ap.add_argument("--sim-watts", type=float, default=220, help="simulated rider power (SIM mode)")
    ap.add_argument("--sim-cadence", type=float, default=90, help="simulated cadence in ERG mode")
    ap.add_argument("-v", "--verbose", action="store_true", help="log raw BLE traffic")
    ap.add_argument("--scan", nargs="?", type=float, const=15, metavar="SECONDS",
                    help="list nearby Bluetooth devices and exit (default 15 s)")
    args = ap.parse_args(argv)

    if args.scan:
        from .ble import scan
        try:
            asyncio.run(scan(args.scan))
        except Exception as e:  # noqa: BLE001
            print(f"Bluetooth unavailable ({type(e).__name__}: {e}); is bluetoothd running "
                  "and the adapter powered on?", file=sys.stderr)
            return 1
        return 0

    cfg = config.load(args.config)
    logq: queue.Queue = queue.Queue()
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%H:%M:%S")
    for h in (logging.StreamHandler(sys.stderr), QueueHandler(logq)):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if cfg["controllers"]["enabled"] and cfg["controllers"]["click_v2_mode"] == "right_only":
        for action, buttons in config.mapped_left_buttons(cfg).items():
            log.warning("controllers.%s uses left-side button(s) %s, which won't work in "
                        "click_v2_mode: right_only (the Click v2 left pod's buttons are ignored; a Zwift Ride's "
                        "left side needs the Zwift unlock)", action, ", ".join(buttons))

    session = Session(cfg)
    log.info("%d gears, %.2f..%.2f, start gear %d", len(session.ratios), session.ratios[0],
             session.ratios[-1], session.state.gear_index + 1)
    loop = asyncio.new_event_loop()

    async def worker():
        tasks = [asyncio.create_task(session.run())]
        if args.simulate:
            from .sim import SimTrainer
            native = cfg["trainer"]["mode"] != "ftms_erg"
            tasks.append(asyncio.create_task(
                SimTrainer(session, native, args.sim_watts, args.sim_cadence).run()))
        else:
            from .ble import discover_and_run
            tasks.append(asyncio.create_task(discover_and_run(session)))
        try:
            await asyncio.gather(*tasks)
        except Exception:  # noqa: BLE001
            log.exception("worker crashed")

    t = threading.Thread(target=lambda: loop.run_until_complete(worker()), daemon=True)
    t.start()
    root = tk.Tk()
    root.geometry("1280x720")
    Gui(root, session, loop, logq)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
