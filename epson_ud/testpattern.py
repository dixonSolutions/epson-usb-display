"""Show SMPTE-style colour bars on the projector for a few seconds.

    python -m epson_ud.testpattern [seconds]

Useful to check the USB link and colour order without involving GNOME.
Stop the epson-usb-display service first; only one program can own the projector.
"""

import sys
import time

from PIL import Image, ImageDraw

from .protocol import Projector

BARS = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
        (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8
    p = Projector()
    try:
        cap = p.query_capability()
        w, h = cap.panel
        img = Image.new("RGB", (w, h), (32, 32, 32))
        d = ImageDraw.Draw(img)
        for i, c in enumerate(BARS):
            d.rectangle([i * w // len(BARS), 0, (i + 1) * w // len(BARS), h * 2 // 3], fill=c)
        d.text((20, h * 2 // 3 + 20), "epson-usb-display test pattern: white yellow cyan green magenta RED BLUE black",
               fill=(255, 255, 255))
        r, g, b = img.split()
        bgr = Image.merge("RGB", (b, g, r)).tobytes()

        p.connect(w, h)
        p.wait_running(timeout=15)
        t0, frames, last = time.monotonic(), 0, 0.0
        while time.monotonic() - t0 < seconds:
            p.send_bands(bgr, w, h)
            frames += 1
            if time.monotonic() - last > 0.1:
                p.read_message()
                last = time.monotonic()
        print(f"{frames} frames in {seconds:.0f}s ({frames / seconds:.1f} fps)")
    finally:
        p.disconnect()
        p.close()


if __name__ == "__main__":
    main()
