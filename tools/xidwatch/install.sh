#!/bin/sh
# Install GPU telemetry, the Xid watcher and automatic recovery. Run with sudo from this directory.
set -eu
cd "$(dirname "$0")"
install -d /usr/local/lib/gpu-xidwatch /var/log/gpu-telemetry /var/log/gpu-xid
install -m 755 gpu_telemetry.py gpu_xidwatch.py /usr/local/lib/gpu-xidwatch/
install -m 755 gpu-xid-capture.sh gpu-reset-one.sh gpu-xid-recover.sh /usr/local/sbin/
install -m 755 gpu-xid-report.sh /usr/local/bin/gpu-xid-report
[ -e /etc/default/gpu-xid-recover ] || install -m 644 gpu-xid-recover.default /etc/default/gpu-xid-recover
install -m 644 gpu-telemetry.service gpu-xidwatch.service gpu-bmc-watchdog-off.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable gpu-bmc-watchdog-off.service
systemctl enable --now gpu-telemetry.service gpu-xidwatch.service
systemctl restart gpu-xidwatch.service
echo "BMC watchdog: $(ipmitool mc watchdog get 2>&1 | grep -E 'Timer Use|Is Running|Action' | tr -s ' ' | paste -sd';')"
echo "Optional (GSP firmware logs, next reboot): install -m 644 nvidia-firmware-logs.conf /etc/modprobe.d/"
sleep 3
systemctl --no-pager --lines=3 status gpu-telemetry.service gpu-xidwatch.service
tail -n 4 /var/log/gpu-telemetry/$(date +%F).csv
