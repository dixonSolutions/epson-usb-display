#!/bin/sh
set -e
systemctl --user disable --now epson-usb-display.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/epson-usb-display.service"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/epson-usb-display"
sudo rm -f /etc/udev/rules.d/70-epson-usb-display.rules
sudo udevadm control --reload
echo "Removed."
