"""USB Mass Storage Bulk-Only Transport, spoken directly over libusb.

The projector exposes its USB Display channel as vendor SCSI commands sent to
the virtual CD-ROM on interface 0.  The kernel refuses vendor opcodes from
non-root users on /dev/sg*, so we detach usb-storage and do BOT ourselves.
"""

import struct
import usb.core
import usb.util

VID, PID = 0x04B8, 0x061B
CBW_SIG = 0x43425355  # 'USBC'
CSW_SIG = 0x53425355  # 'USBS'


class TransportError(Exception):
    pass


def find_device():
    return usb.core.find(idVendor=VID, idProduct=PID) is not None


class BulkOnly:
    def __init__(self, timeout_ms=5000):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            raise TransportError("Epson projector (04b8:061b) not found")
        self.timeout = timeout_ms
        self.tag = 1
        self.intf = 0
        if self.dev.is_kernel_driver_active(self.intf):
            self.dev.detach_kernel_driver(self.intf)
            self._reattach = True
        else:
            self._reattach = False
        usb.util.claim_interface(self.dev, self.intf)
        cfg = self.dev.get_active_configuration()
        alt = cfg[(self.intf, 0)]
        self.ep_out = usb.util.find_descriptor(
            alt, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(
            alt, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.intf)
            if self._reattach:
                self.dev.attach_kernel_driver(self.intf)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)

    def _reset_recovery(self):
        # BOT reset + clear halts on both bulk endpoints.
        self.dev.ctrl_transfer(0x21, 0xFF, 0, self.intf, None, self.timeout)
        self.dev.clear_halt(self.ep_in)
        self.dev.clear_halt(self.ep_out)

    def command(self, cdb, data_out=None, data_in_len=0):
        """Run one SCSI command. Returns bytes read (data-in) or b''."""
        cdb = bytes(cdb)
        if data_out is not None and data_in_len:
            raise ValueError("bidirectional transfers not supported")
        length = len(data_out) if data_out is not None else data_in_len
        flags = 0x80 if data_in_len else 0x00
        tag = self.tag
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        cbw = struct.pack("<IIIBBB", CBW_SIG, tag, length, flags, 0, len(cdb)) + cdb.ljust(16, b"\0")
        self.ep_out.write(cbw, self.timeout)

        result = b""
        try:
            if data_out:
                self.ep_out.write(data_out, self.timeout)
            elif data_in_len:
                result = bytes(self.ep_in.read(data_in_len, self.timeout))
        except usb.core.USBError as e:
            if e.errno != 32:  # EPIPE = stall; CSW still follows
                raise
            self.dev.clear_halt(self.ep_in if data_in_len else self.ep_out)

        try:
            csw = bytes(self.ep_in.read(13, self.timeout))
        except usb.core.USBError as e:
            if e.errno != 32:
                raise
            self.dev.clear_halt(self.ep_in)
            csw = bytes(self.ep_in.read(13, self.timeout))
        sig, rtag, residue, status = struct.unpack("<IIIB", csw)
        if sig != CSW_SIG or rtag != tag:
            self._reset_recovery()
            raise TransportError(f"bad CSW {csw.hex()}")
        if status == 2:
            self._reset_recovery()
            raise TransportError("phase error")
        if status == 1:
            raise TransportError(f"command {cdb.hex()} failed (CHECK CONDITION)")
        return result

    def request_sense(self):
        return self.command(bytes([0x03, 0, 0, 0, 18, 0]), data_in_len=18)

    def inquiry(self):
        return self.command(bytes([0x12, 0, 0, 0, 36, 0]), data_in_len=36)
