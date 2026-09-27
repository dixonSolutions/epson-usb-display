#!/bin/sh
# Install epson-usb-display for the current user (GNOME on Wayland).
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
DEST="$HOME/.local/share/epson-usb-display"

echo "==> Installing udev rule (needs sudo) so your user can talk to the projector"
sudo install -m 644 "$SRC/70-epson-usb-display.rules" /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=04b8 || true

echo "==> Copying program to $DEST"
mkdir -p "$DEST"
cp -r "$SRC/epson_ud" "$DEST/"
python3 -m venv --system-site-packages "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q pyusb

echo "==> Enabling systemd user service"
mkdir -p "$HOME/.config/systemd/user"
cp "$SRC/systemd/epson-usb-display.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now epson-usb-display.service

echo "Done. Plug in the projector's USB-B cable and pick the USB Display source."
echo "Logs: journalctl --user -u epson-usb-display -f"
