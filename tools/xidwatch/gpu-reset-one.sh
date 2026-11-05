#!/bin/bash
# Recover dead GPUs (Xid 62/154 "GPU requires reset") without rebooting the server.
# Usage: sudo gpu-reset-one.sh 0000:41:00 [0000:01:00 ...]         (dry run: prints what it would do)
#        sudo gpu-reset-one.sh 0000:41:00 [0000:01:00 ...] --yes   (does it)
#
# Community procedure (dev.to "Troubleshooting NVIDIA Error XID 62"; open-gpu-kernel-modules #1080):
# free the GPU, unbind it from the driver, PCI reset (FLR or bus reset), remove it, rescan the bus.
# Without the reset the GSP does not boot again ("unexpected WPR2 already up").
# Tested on this server (RTX 3090, 615.71.09 open, GSP): it brought back a GPU after a real Xid 62 PMU halt.
#
# GPUs joined by NVLink must be reset TOGETHER: resetting only one drops the links and the peer
# ends up in "Reset required" (Xid 74). Resetting all GPUs is the simplest option.
# Each GPU here sits on its own root port, so a bus reset of one does not touch the others.
# If a step hangs (process stuck in D state), the way out is a reboot, or a power cycle from the BMC.
set -u
STOP_SERVICES="flashnext gpu-telemetry gpu-fancontrol gpu-power-limit.timer nvidia-persistenced"
START_SERVICES="nvidia-persistenced gpu-power-limit.timer gpu-power-limit.service gpu-fancontrol gpu-telemetry flashnext"
[ -r /etc/default/gpu-xid-recover ] && . /etc/default/gpu-xid-recover
GO=; BUSES=()
for a in "$@"; do [ "$a" = --yes ] && GO=--yes || BUSES+=("$a"); done
[ ${#BUSES[@]} -gt 0 ] || { echo "usage: gpu-reset-one.sh 0000:BB:00 [0000:CC:00 ...] [--yes]"; exit 1; }
BUS=${BUSES[0]}
LOG=/var/log/gpu-xid/reset-${BUS//:/_}-$(date +%Y%m%d-%H%M%S).log
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
for b in "${BUSES[@]}"; do [ -e /sys/bus/pci/devices/$b.0 ] || { echo "no such device: $b.0"; exit 1; }; done
mkdir -p /var/log/gpu-xid; exec > >(tee "$LOG") 2>&1

step() { echo; echo "== $*"; }
do_() { echo "+ $*"; [ "$GO" = --yes ] && { timeout 90 bash -c "$*"; r=$?; [ $r = 124 ] && { echo "!! hung for 90 s: reboot or power cycle from the BMC"; exit 2; }; return $r; }; return 0; }

step "State before: ${BUSES[*]}"
for b in "${BUSES[@]}"; do
  nvidia-smi -i "$b.0" --query-gpu=index,uuid,pstate --format=csv,noheader 2>&1 | head -2
  nvidia-smi -i "$b.0" -q 2>&1 | grep -i 'Recovery Action'
  lspci -s "${b#0000:}.0" -vv 2>/dev/null | grep -E 'LnkSta:|UESta|CESta|DevSta'
  echo "reset_method: $(cat /sys/bus/pci/devices/$b.0/reset_method)"
done
[ "$GO" = --yes ] || echo "(DRY RUN: add --yes to do it)"

step "Stop everything that uses the GPUs"
do_ "systemctl stop $STOP_SERVICES"
busy=$(fuser /dev/nvidia* 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -un | paste -sd,)
[ -n "$busy" ] && { echo "Processes still using the GPUs:"; ps -o pid,user,cmd -p "$busy" | cut -c1-120; [ "$GO" = --yes ] && { echo "Stopping here."; exit 1; }; }

step "Unbind audio and GPU functions from their drivers"
for b in "${BUSES[@]}"; do
  [ -e /sys/bus/pci/drivers/snd_hda_intel/$b.1 ] && do_ "echo $b.1 > /sys/bus/pci/drivers/snd_hda_intel/unbind"
  [ -e /sys/bus/pci/drivers/nvidia/$b.0 ] && do_ "echo $b.0 > /sys/bus/pci/drivers/nvidia/unbind"
done

step "PCI reset of each function"
for b in "${BUSES[@]}"; do do_ "echo 1 > /sys/bus/pci/devices/$b.0/reset"; done

step "Remove and rescan"
for b in "${BUSES[@]}"; do
  [ -e /sys/bus/pci/devices/$b.1 ] && do_ "echo 1 > /sys/bus/pci/devices/$b.1/remove"
  do_ "echo 1 > /sys/bus/pci/devices/$b.0/remove"
done
do_ "sleep 2; echo 1 > /sys/bus/pci/rescan; sleep 10"

step "Check"
[ "$GO" = --yes ] && {
  journalctl -k --since -2min --no-pager | grep -E "vbios_override|RmInitAdapter|Xid|WPR2" | tail -10
  for b in "${BUSES[@]}"; do echo "$b: $(nvidia-smi -i "$b.0" -q 2>&1 | grep -iE 'Recovery Action' | tr -s ' ')"; done
  nvidia-smi --query-gpu=index,uuid,pci.bus_id,pstate,temperature.gpu,power.draw --format=csv,noheader
}

step "Start the services again"
do_ "systemctl start $START_SERVICES"
echo; echo "Log: $LOG. If a GPU is not healthy (RmInitAdapter failed, ERR!), reboot (or power cycle from the BMC)."
