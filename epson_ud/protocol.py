"""Epson USB Display ("UD") protocol.

Reverse engineered from Epson USB Display 1.6x for Windows (EMP_PJCON.dll).
See docs/PROTOCOL.md for the byte-level reference.
"""

import struct
import time

from .transport import BulkOnly, TransportError

CMD_READ = 0x00
CMD_IMAGE = 0x80
CMD_BAND = 0xA0
CMD_CONNECT = 0xC0
CMD_STATUS = 0xC1
CMD_CAPABILITY = 0xC3

MSG_KEY, MSG_STATUS, MSG_SERVER_CTRL, MSG_CAPABILITY = 1, 4, 5, 6

STATE_IDLE, STATE_CONNECTING, STATE_PAUSED, STATE_RUNNING = 0, 4, 8, 9


class ProtocolError(Exception):
    pass


def compact_len(n):
    # Tight-style compact length, but always 3 bytes.
    return bytes([(n & 0x7F) | 0x80, ((n >> 7) & 0x7F) | 0x80, (n >> 14) & 0xFF])


def eprd(body, le_length=False):
    return b"EPRD0600" + bytes(8) + struct.pack("<I" if le_length else ">I", len(body)) + body


class Capability:
    def __init__(self, blob):
        self.raw = blob
        self.platform = None
        self.panel = None
        self.framebuffers = []
        self.encodings = []
        self.functions = bytes(8)
        self._parse(blob)

    def _parse(self, b):
        if len(b) < 4 or b[0] != 0:
            raise ProtocolError(f"bad capability blob {b[:16].hex()}")
        i, end = 4, 3 + struct.unpack_from("<H", b, 1)[0]
        while i < min(end, len(b)):
            tag = b[i]
            if 1 <= tag <= 8:
                ln, i = b[i + 1], i + 2
            else:
                ln, i = struct.unpack_from("<H", b, i + 1)[0], i + 3
            if tag == 5 and ln == 0x28:  # firmware under-reports this (see spec 3.2)
                ln = 0x2C
            v = b[i:i + ln]
            i += ln
            if tag == 1:
                self.platform = v[0]
            elif tag == 3:
                self.panel = struct.unpack_from("<HH", v)
            elif tag == 4:
                self.framebuffers = [(e[0],) + struct.unpack_from("<HH", e, 2)
                                     for e in (v[k:k + 6] for k in range(0, len(v) - 5, 6))]
            elif tag == 5:
                for k in range(0, len(v) - 21, 22):
                    f = struct.unpack_from("<BB10H", v, k)
                    self.encodings.append(dict(type=f[0], sub=f[1], align=(f[2], f[3]), unit=(f[4], f[5]),
                                               min=(f[6], f[7]), max=(f[8], f[9])))
            elif tag == 8:
                self.functions = bytes(v)

    @property
    def pause_resume(self):
        return bool(self.functions[0] & 0x04)

    @property
    def scanlined(self):
        return bool(self.functions[3] & 0x02)

    def jpeg(self):
        for e in self.encodings:
            if e["type"] == 7 and e["sub"] == 9:
                return e
        return None

    def __repr__(self):
        return (f"Capability(platform={self.platform}, panel={self.panel}, fb={self.framebuffers}, "
                f"functions={self.functions.hex()}, encodings={self.encodings})")


class Projector:
    def __init__(self, log=print):
        self.t = BulkOnly(timeout_ms=3000)
        self.log = log
        self.state = STATE_IDLE
        self.cap = None
        self.max_transfer = 0xFFFF
        self.short_packet = 0x40
        self.on_key = None

    def close(self):
        self.t.close()

    # --- transport -----------------------------------------------------
    def send(self, cmd, data):
        """Write `data` with econ command `cmd`, chunked like Epson's DeviceIO."""
        off, remaining = 0, len(data)
        while remaining:
            chunk = min(self.max_transfer, remaining)
            if self.short_packet > 0 and chunk % self.short_packet == 0:
                chunk = chunk // 2 + 1
            if remaining == self.max_transfer + 1:
                chunk = self.max_transfer // 2
            remaining -= chunk
            cdb = struct.pack(">BBBIB4x", 0xD9, 0, cmd, chunk, 0x45 if remaining == 0 else 0x43)
            self.t.command(cdb, data_out=data[off:off + chunk])
            off += chunk

    def _read_raw(self, n):
        cdb = struct.pack(">BBBIB4x", 0xD9, 0, CMD_READ, n, 0x45)
        return self.t.command(cdb, data_in_len=n)

    def read_message(self):
        """Return (type, payload) or None if the projector has nothing to say."""
        buf = self._read_raw(256)
        if len(buf) < 6 or buf[0] != 0xFF:
            return None
        mtype = buf[1] & 0x7F
        n = struct.unpack_from(">I", buf, 2)[0]
        payload = buf[6:6 + min(n, 250)]
        if n > 250:
            if n > 500:
                return None
            payload += self._read_raw(n - 250)
        self._handle(mtype, payload)
        return mtype, payload

    def _handle(self, mtype, p):
        if mtype == MSG_STATUS:
            s = p[:4]
            if s == b"REDY" and self.state == STATE_CONNECTING:
                self.state = STATE_PAUSED if self.cap.pause_resume else STATE_RUNNING
            self.log(f"status {s!r} -> state {self.state}")
        elif mtype == MSG_SERVER_CTRL and len(p) >= 2:
            action = {0: "resume", 1: "pause", 2: "stop"}.get(p[1], p[1])
            self.log(f"server control: {action}")
            if p[1] == 0:
                self.state = STATE_RUNNING
            elif p[1] == 1:
                self.state = STATE_PAUSED
            elif p[1] == 2:
                self.state = STATE_IDLE
        elif mtype == MSG_KEY and self.on_key and len(p) >= 2:
            self.on_key(p[0], p[1])
        elif mtype == MSG_CAPABILITY:
            self.cap = Capability(p)

    # --- session -------------------------------------------------------
    def query_capability(self, tries=200):
        self.send(CMD_CAPABILITY, b"\xC3\x00")
        for _ in range(tries):
            m = self.read_message()
            if m and m[0] == MSG_CAPABILITY:
                return self.cap
            time.sleep(0.1)
        raise ProtocolError("no capability reply")

    def connect(self, width, height, mode=None):
        mode = mode or ("Y" if self.cap.scanlined else "C")
        self.mode = mode
        if self.cap.platform in (5, 8):
            self.max_transfer, self.short_packet = 0xFFFF, 0x40
        else:
            self.max_transfer, self.short_packet = 0x10000, -1
        p = bytearray(42)
        p[0], p[1] = 0xC0, ord(mode)
        struct.pack_into("<HH", p, 2, width, height)
        p[6], p[7], p[8], p[9] = (0x18 if mode == "Y" else 0x20), 0x10, 0, 1
        struct.pack_into("<HHH", p, 0x0A, 0xFF, 0xFF, 0xFF)
        p[0x10], p[0x11], p[0x12] = 0x10, 0x08, 0
        self.send(CMD_CONNECT, bytes(p))
        self.state = STATE_CONNECTING
        for _ in range(100):
            self.send(CMD_STATUS, b"\xC1\x43")
            self.read_message()
            if self.state in (STATE_PAUSED, STATE_RUNNING):
                return self.state
            time.sleep(0.1)
        raise ProtocolError("projector never reported REDY")

    def wait_running(self, timeout=None):
        t0 = time.monotonic()
        while self.state == STATE_PAUSED:
            self.read_message()
            if timeout is not None and time.monotonic() - t0 > timeout:
                return False
            time.sleep(0.1)
        return self.state == STATE_RUNNING

    def send_screen_format(self, width, height):
        body = bytes([0xC8, 0, 0, 0]) + struct.pack(">HH", width, height) + \
            bytes([0x20, 0x20, 0, 1]) + struct.pack(">HHH", 0xFF, 0xFF, 0xFF) + bytes([0x10, 0x08, 0, 0, 0, 0])
        self.send(CMD_IMAGE, eprd(body, le_length=True))

    def send_rects(self, rects):
        """rects: list of (x, y, w, h, jpeg_bytes)."""
        body = bytearray(struct.pack(">BBH", 0, 0, len(rects)))
        for x, y, w, h, jpg in rects:
            body += struct.pack(">HHHHI", x, y, w, h, 7) + b"\x90" + compact_len(len(jpg)) + jpg
        self.send(CMD_IMAGE, eprd(bytes(body)))

    def band_height(self, width):
        # Bands must fit one unchunked transfer (spec 8.3).
        return 20 if width < 1280 else (16 if width == 1280 else 8)

    def send_bands(self, bgr, width, height, top=0, bottom=None):
        """Send rows [top, bottom) of a packed top-down BGR24 frame as 0xA0 bands."""
        bottom = height if bottom is None else bottom
        top &= ~1
        stride = width * 3
        bh = self.band_height(width)
        y = top
        while y < bottom:
            h = min(bh, bottom - y)
            last = 1 if y + h >= bottom else 0
            cdb = struct.pack(">BBBHHBHBB", 0xD9, 0, CMD_BAND, y, h, 0x45, 0, last, 0)
            self.t.command(cdb, data_out=bgr[y * stride:(y + h) * stride])
            y += h

    def disconnect(self):
        try:
            self.send(CMD_CONNECT, b"\xC0\x44" + bytes(40))
        except TransportError:
            pass
        self.state = STATE_IDLE
