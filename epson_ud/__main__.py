"""epson-usb-display: use an Epson projector's USB-B "USB Display" port on Linux.

Waits for the projector, adds a virtual monitor to GNOME while it is attached,
and streams that monitor to the projector.
"""

import argparse
import signal
import sys
import threading
import time

import dbus.mainloop.glib
from gi.repository import GLib, GLibUnix

from .display import VirtualDisplay
from .protocol import STATE_IDLE, STATE_RUNNING, Projector, ProtocolError
from .transport import TransportError, find_device


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Session:
    """One projector connection plus its virtual monitor."""

    def __init__(self, fps):
        self.fps = fps
        self.frame = None
        self.cond = threading.Condition()
        self.stopped = threading.Event()
        self.resend = threading.Event()

    def on_frame(self, bgr):
        with self.cond:
            self.frame = bgr
            self.cond.notify()

    def run(self):
        proj = Projector(log=log)
        display = None
        try:
            cap = proj.query_capability()
            w, h = cap.panel
            log(f"projector: panel {w}x{h}, platform {cap.platform}, mode {'Y' if cap.scanlined else 'C'}")
            if not cap.scanlined:
                raise ProtocolError("this projector uses JPEG (C) mode, which is not implemented yet")
            proj.connect(w, h)
            def recreate():
                if self.stopped.is_set():
                    return GLib.SOURCE_REMOVE
                log("GNOME closed the virtual monitor; adding it again")
                display.start()
                self.resend.set()
                return GLib.SOURCE_REMOVE

            display = VirtualDisplay(w, h, self.on_frame, fps=self.fps,
                                     on_closed=lambda: GLib.timeout_add(2000, recreate))
            GLib.idle_add(display.start)
            log("virtual monitor added; arrange it in Settings > Displays")
            self._stream(proj, w, h)
        finally:
            if display:
                GLib.idle_add(display.stop)
            proj.disconnect()
            proj.close()

    def _stream(self, proj, w, h):
        prev = None       # last frame the projector has
        latest = None     # newest captured frame
        last_poll = 0.0
        stats_t, sent = time.monotonic(), 0
        last_full = 0.0
        rows = proj.band_height(w)
        band = rows * w * 3
        while not self.stopped.is_set():
            with self.cond:
                if self.frame is None:
                    self.cond.wait(timeout=0.1)
                if self.frame is not None:
                    latest, self.frame = self.frame, None
            if time.monotonic() - last_poll > 0.1:
                was_running = proj.state == STATE_RUNNING
                proj.read_message()
                last_poll = time.monotonic()
                if proj.state == STATE_IDLE:
                    log("projector stopped the session")
                    return
                if proj.state == STATE_RUNNING and not was_running:
                    prev = None  # projector may have lost the picture while paused
            if time.monotonic() - stats_t > 60:
                if sent:
                    log(f"sent {sent} screen updates in the last minute")
                stats_t, sent = time.monotonic(), 0
            if self.resend.is_set():
                self.resend.clear()
                prev = None
            if latest is None or latest is prev or proj.state != STATE_RUNNING:
                continue
            if len(latest) != w * h * 3:
                continue
            if prev is None or time.monotonic() - last_full > 5:
                # Full frame at start and every few seconds to heal any glitch.
                proj.send_bands(latest, w, h)
                last_full = time.monotonic()
                sent += 1
            elif latest != prev:
                changed = [i for i in range(0, len(latest), band) if latest[i:i + band] != prev[i:i + band]]
                if changed:
                    top = changed[0] // (w * 3)
                    bottom = min(h, changed[-1] // (w * 3) + rows)
                    # An update starting at row 0 is only shown if it runs to
                    # the bottom (verified on hardware), so send the whole frame.
                    if top == 0:
                        bottom = h
                        last_full = time.monotonic()
                    proj.send_bands(latest, w, h, top=top, bottom=bottom)
                    sent += 1
            prev = latest


def main():
    ap = argparse.ArgumentParser(prog="epson-usb-display", description=__doc__)
    ap.add_argument("--fps", type=int, default=60, help="virtual monitor refresh rate (default 60)")
    ap.add_argument("--once", action="store_true", help="exit when the projector disconnects")
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    state = {"session": None}

    def worker():
        while True:
            if not find_device():
                time.sleep(1)
                continue
            s = Session(args.fps)
            state["session"] = s
            try:
                s.run()
            except (TransportError, ProtocolError, OSError) as e:
                log("session ended:", e)
            except Exception as e:  # keep the daemon alive on USB hiccups
                log("session error:", repr(e))
            if args.once:
                GLib.idle_add(loop.quit)
                return
            time.sleep(2)

    def quit_():
        s = state["session"]
        if s:
            s.stopped.set()
        GLib.timeout_add(1500, loop.quit)
        return GLib.SOURCE_REMOVE

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, quit_)
    threading.Thread(target=worker, daemon=True).start()
    log("waiting for Epson projector (USB 04b8:061b)")
    loop.run()


if __name__ == "__main__":
    sys.exit(main())
