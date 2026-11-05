#!/bin/bash
# Automatic recovery of a dead GPU. Started by gpu_xidwatch.py on Xid 62/74/79/119/120, or 154 not back to None.
# Usage: gpu-xid-recover.sh <bus 0000:BB:00> <xid>
#   1. Diagnostic capture (gpu-xid-capture.sh).
#   2. PCI reset of the GPUs (gpu-reset-one.sh --yes; all of them by default) and a health check of every GPU.
#   3. If that fails: reboot the server (at most MAX_REBOOTS_24H per 24 h), with the BMC watchdog armed
#      in case the shutdown hangs in the driver.
# Trace: /var/log/gpu-xid/recovery.log (+ journal, tag gpu-xid-recover). Config: /etc/default/gpu-xid-recover.
set -u
BUS=${1:?bus}; XID=${2:?xid}
AUTO_RECOVER=1; AUTO_REBOOT=1; MAX_REBOOTS_24H=2; WATCHDOG_MIN=20
RESET_ALL=1                           # 1 = reset every GPU (avoids leaving an NVLink peer in "Reset required")
NVLINK_GROUPS="0000:01:00,0000:41:00" # if RESET_ALL=0: GPUs bridged by NVLink are reset together
VLLM_UNIT=flashnext
[ -r /etc/default/gpu-xid-recover ] && . /etc/default/gpu-xid-recover
STATE=/var/lib/gpu-xidwatch; LOG=/var/log/gpu-xid/recovery.log
mkdir -p "$STATE" /var/log/gpu-xid
log() { local m; m="$(date '+%F %T') [$BUS xid$XID] $*"; echo "$m" >>"$LOG"; logger -t gpu-xid-recover "$*"; }
notify() { log "$*"; wall "gpu-xid-recover: $*" 2>/dev/null; }

# Wait for a background process with a deadline. Not timeout(1): a process stuck inside the driver never dies.
wait_deadline() { local pid=$1 secs=$2 t=0; while kill -0 "$pid" 2>/dev/null; do sleep 2; t=$((t+2)); [ $t -ge "$secs" ] && return 124; done; wait "$pid"; }

# One recovery at a time.
exec 9>"$STATE/recover.lock"; flock 9

T0=$(date +%s)
notify "Xid $XID on $BUS. Step 1: diagnostic capture."
/usr/local/sbin/gpu-xid-capture.sh "$BUS" "$XID" & wait_deadline $! 900
rc=$?; log "capture done (rc=$rc, $(( $(date +%s)-T0 )) s): $(ls -1dt /var/log/gpu-xid/*-${BUS//:/_}-xid* 2>/dev/null | head -1)"

[ "$AUTO_RECOVER" = 1 ] || { notify "AUTO_RECOVER=0: nothing more to do. Reboot by hand."; exit 0; }

ALL_GPUS=$(lspci -D -d 10de: | awk '$1 ~ /\.0$/ {sub(/\.0$/,"",$1); print $1}' | paste -sd' ')
NGPUS=$(wc -w <<<"$ALL_GPUS")
TARGETS=$BUS
for g in $NVLINK_GROUPS; do case ",$g," in *",$BUS,"*) TARGETS=${g//,/ };; esac; done
[ "$RESET_ALL" = 1 ] && TARGETS=$ALL_GPUS

verify() {
  local q n b
  for b in $ALL_GPUS; do   # every GPU, not just the one that failed
    q=$(timeout 30 nvidia-smi -i "$b.0" -q 2>&1) || { log "check: nvidia-smi -q fails on $b: $(head -c 200 <<<"$q")"; return 1; }
    grep -qE 'GPU Recovery Action +: None' <<<"$q" || { log "check: $b $(grep -i 'Recovery Action' <<<"$q" | tr -s ' ')"; return 1; }
  done
  n=$(timeout 30 nvidia-smi --query-gpu=pci.bus_id,pstate --format=csv,noheader 2>&1 | grep -E '^0000' | grep -vcE 'ERR|Unknown')
  [ "$n" -ge "$NGPUS" ] || { log "check: only $n of $NGPUS GPUs healthy"; return 1; }
  if journalctl -k --since "@$T1" --no-pager | grep -qE "RmInitAdapter failed|NVRM: Xid \(PCI:[0-9a-f:]+\): (62|74|79|119|120),"; then
    log "check: driver errors after the reset: $(journalctl -k --since "@$T1" --no-pager | grep -E 'RmInitAdapter|Xid' | tail -2 | tr '\n' ' ')"; return 1; fi
  return 0
}

T1=$(date +%s)
notify "Step 2: PCI reset of $TARGETS (inference stops for a few minutes)."
/usr/local/sbin/gpu-reset-one.sh $TARGETS --yes >/dev/null 2>&1 & wait_deadline $! 600
rc=$?
log "gpu-reset-one finished rc=$rc in $(( $(date +%s)-T1 )) s (log: $(ls -1t /var/log/gpu-xid/reset-*.log 2>/dev/null | head -1))"
if [ $rc = 0 ]; then
  T1=$(( $(date +%s) - 5 )); sleep 60
  if verify; then
    notify "RESET OK: $TARGETS recovered without a reboot. $(timeout 20 nvidia-smi --query-gpu=pci.bus_id,pstate,temperature.gpu,power.draw,fan.speed --format=csv,noheader | tr '\n' ';')"
    echo "$(date +%s) $BUS reset-ok" >>"$STATE/history"
    # Follow-up: is inference back and are the GPUs still fine after 20 min? (closes fd 9: does not hold the lock)
    ( exec 9>&-; sleep 1200; log "follow-up +20 min: $VLLM_UNIT=$(systemctl is-active "$VLLM_UNIT"); $(timeout 20 nvidia-smi --query-gpu=index,pci.bus_id,pstate,temperature.gpu,power.draw,memory.used --format=csv,noheader | tr '\n' ';')" ) &
    exit 0
  fi
fi

# Step 3: reboot.
echo "$(date +%s) $BUS reset-failed" >>"$STATE/history"
[ "$AUTO_REBOOT" = 1 ] || { notify "Reset FAILED and AUTO_REBOOT=0: reboot by hand."; exit 1; }
now=$(date +%s); touch "$STATE/reboots"
recent=$(awk -v t=$((now-86400)) '$1>t' "$STATE/reboots" | wc -l)
if [ "$recent" -ge "$MAX_REBOOTS_24H" ]; then
  notify "Reset FAILED and already $recent automatic reboots in 24 h: NOT rebooting (avoids a loop). Needs a human."
  exit 1
fi
echo "$now $BUS xid$XID" >>"$STATE/reboots"
# BMC watchdog (IPMI Set Watchdog Timer: SMS/OS use, hard reset): if the shutdown hangs in the driver,
# the BMC resets the machine after WATCHDOG_MIN minutes. gpu-bmc-watchdog-off.service disarms it at boot.
c=$((WATCHDOG_MIN*600))   # in 100 ms units
if ipmitool raw 0x06 0x24 0x04 0x01 0x00 0x10 $(printf '0x%02x 0x%02x' $((c & 255)) $((c >> 8))) >/dev/null 2>&1 \
   && ipmitool mc watchdog reset >/dev/null 2>&1; then
  log "BMC watchdog armed: hard reset in $WATCHDOG_MIN min unless the system is back up"
else
  log "WARNING: could not arm the BMC watchdog"
fi
notify "Step 3: reset FAILED, rebooting now (automatic reboot $((recent+1))/$MAX_REBOOTS_24H in 24 h)."
sync; systemctl reboot
