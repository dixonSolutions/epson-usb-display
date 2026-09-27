# epson-usb-display

Use an Epson projector's **USB-B "USB Display"** port on Linux. The projector shows up in GNOME as a
normal monitor (Settings → Displays) that you can extend onto, mirror or rearrange. No HDMI or VGA
cable needed.

Epson only ships a Windows and macOS driver for this feature. This project is an independent
reimplementation based on a reverse-engineered protocol spec ([docs/PROTOCOL.md](docs/PROTOCOL.md)).
It runs entirely in userspace and needs no kernel module.

**Website:** https://dixonsolutions.github.io/epson-usb-display/

## Status

| | |
|---|---|
| Tested projector | Epson unit enumerating as `04b8:061b` "EPSON VS340/X300/X130/X04" with 1024×768 panel (firmware reports platform 11, "PGUD") |
| Desktop | GNOME 50 on Wayland (Fedora 43) |
| Speed | ~10 fps full-screen; only changed rows are resent |
| Works | virtual monitor in GNOME, extend/mirror, test pattern tool |
| Untested | unplug/replug while running, projector remote buttons (the kernel already exposes them as a HID keyboard) |
| Not yet | JPEG ("C" mode) projectors, audio, KDE/wlroots desktops |

Other Epson models using the same USB ID (EB-X04, EB-X130, EB-X300, VS340, …) will probably work.
Please open an issue with the log output if yours doesn't.

## Install

Requirements: GNOME on Wayland, Python 3, PyGObject, GStreamer with the PipeWire plugin, and
dbus-python (all preinstalled on Fedora Workstation).

```sh
git clone https://github.com/dixonSolutions/epson-usb-display.git
cd epson-usb-display
./install.sh
```

The installer:
1. Adds a udev rule so your logged-in user can access the projector over USB (asks for sudo once).
2. Copies the program to `~/.local/share/epson-usb-display` and installs `pyusb` into a venv.
3. Enables a systemd **user** service that waits for the projector.

Then connect the USB-B cable and choose the **USB Display** source on the projector. A new
1024×768 monitor appears within a few seconds. Unplug the cable and it disappears.

```sh
journalctl --user -u epson-usb-display -f     # logs
./uninstall.sh                                # remove everything
```

### Run without installing

```sh
python3 -m venv --system-site-packages .venv && .venv/bin/pip install pyusb
sudo cp 70-epson-usb-display.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger
.venv/bin/python -m epson_ud                  # virtual monitor
.venv/bin/python -m epson_ud.testpattern 8    # colour bars for 8 seconds
```

## How it works

```
GNOME (Mutter ScreenCast.RecordVirtual) ── PipeWire ── GStreamer (BGR 1024×768)
      │ virtual monitor "Meta-0"                              │
      └── you drag windows onto it                            ▼
                                           epson_ud: diff rows → 20-line bands
                                                              │
                        SCSI vendor cmd 0xD9 in USB Bulk-Only Transport (libusb)
                                                              ▼
                                  projector's virtual CD-ROM (interface 0)
```

* The projector presents a fake CD-ROM (holding the Windows installer) plus HID keyboard and mouse.
  The Windows driver sends the picture as **vendor SCSI commands** (opcode `0xD9`) to that CD-ROM.
* The Linux kernel only lets root send vendor SCSI opcodes, so `epson_ud` briefly detaches
  `usb-storage` from interface 0 and speaks USB Bulk-Only Transport itself through libusb.
* Session: capability query (`0xC3`) → connect (`0xC0 'Y'`) → projector replies BUSY, REDY, RESUME →
  frames are sent as full-width 20-line bands of raw BGR24 (`0xA0`) → disconnect (`0xC0 'D'`).

Full byte-level details are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Legal

This is an independent interoperability project, not affiliated with or endorsed by Seiko Epson.
No Epson code or binaries are included or redistributed. "Epson" is a trademark of Seiko Epson
Corporation. MIT licensed.
