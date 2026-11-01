#!/bin/sh
# Install gpu-fancontrol as a systemd service. Run with sudo from the repo root.
# Needs: python3-pynvml, ipmitool, and BMC credentials in ~/.config/gpu-fancontrol/bmc.env
# (or already in /etc/gpu-fancontrol/bmc.env).
set -eu
cd "$(dirname "$0")"
install -d -m 755 /opt/gpu-fancontrol
install -m 644 tools/bmc.py tools/gputemps.py tools/gpu_fancontrol.py /opt/gpu-fancontrol/
install -d -m 700 /etc/gpu-fancontrol
[ -f /etc/gpu-fancontrol/config.toml ] || install -m 644 examples/config.toml /etc/gpu-fancontrol/config.toml
# Full 'gpufan' BMC profile, recreated by the daemon if the BMC loses it (factory reset after a power cut).
# Sensor and fan numbers are for the MC62-G40: export your own with 'tools/bmc.py profiles' if different.
[ -f /etc/gpu-fancontrol/gpufan.json ] || install -m 644 examples/gpufan.json /etc/gpu-fancontrol/gpufan.json
if [ ! -f /etc/gpu-fancontrol/bmc.env ]; then
    install -m 600 "/home/${SUDO_USER:?run with sudo}/.config/gpu-fancontrol/bmc.env" /etc/gpu-fancontrol/bmc.env
fi
install -m 644 systemd/gpu-fancontrol.service /etc/systemd/system/gpu-fancontrol.service
systemctl daemon-reload
systemctl enable --now gpu-fancontrol.service
sleep 5
systemctl --no-pager status gpu-fancontrol.service | head -15
