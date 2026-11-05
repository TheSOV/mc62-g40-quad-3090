#!/bin/bash
# Diagnostic capture after an NVIDIA Xid. Usage: gpu-xid-capture.sh <PCI bus> <xid>
# Writes /var/log/gpu-xid/<date>-<bus>-xid<N>/. Must run BEFORE any GPU reset or reboot.
set -u
BUS=${1:-unknown}; XID=${2:-0}
VLLM_UNIT=flashnext            # inference service whose log is worth keeping (override in the config)
[ -r /etc/default/gpu-xid-recover ] && . /etc/default/gpu-xid-recover
TS=$(date +%Y%m%d-%H%M%S)
DIR=${GPU_XID_DIR:-/var/log/gpu-xid}/${TS}-${BUS//:/_}-xid${XID}
TEL=${GPU_TELEMETRY_DIR:-/var/log/gpu-telemetry}
mkdir -p "$DIR"; cd "$DIR" || exit 1
msg="Xid $XID on GPU $BUS: capturing to $DIR"
logger -p kern.crit -t gpu-xid "$msg"
wall "$msg (do not reboot until it finishes)" 2>/dev/null

run() { local out=$1; shift; timeout "${T:-60}" "$@" >"$out" 2>&1; }

# 0. Driver/GSP internal dump first (nvlog, includes the PMU and GSP log buffers): only NVIDIA can decode it.
#    -D also writes rm_<n>.pb, except for the GPU that needs a reset.
T=120 run debugdump-nvlog.txt nvidia-debugdump -N -f "$DIR/nvlog.zip"
T=300 run debugdump-all.txt   nvidia-debugdump -D -f "$DIR/debugdump-all.zip"

# 1. Cheap things that don't touch the GPU
run kernel.log           journalctl -k -b --no-pager -o short-precise
run system-30min.log     journalctl -b --no-pager -o short-precise --since -30min
run fancontrol-60min.log journalctl -u gpu-fancontrol --no-pager -o short-precise --since -60min
run vllm-30min.log       journalctl -u "$VLLM_UNIT" --no-pager -o short-precise --since -30min
{ for f in $(ls -1 "$TEL"/*.csv 2>/dev/null | tail -2); do tail -n 4800 "$f"; done; } > telemetry-last.csv  # ~20 min, 4 GPUs
cp /proc/cmdline cmdline.txt; uname -a > uname.txt; uptime > uptime.txt

# 2. PCIe link state and error registers (this BIOS does not hand AER to the kernel)
for dev in $(lspci -D -d 10de: | awk '{print $1}'); do
  run "lspci-${dev//:/_}.txt" lspci -vvv -s "$dev"
done
run lspci-tree.txt lspci -tv

# 3. BMC: power/voltage events and sensors
run bmc-sel.txt ipmitool sel elist
run bmc-sdr.txt ipmitool sdr elist full

# 4. GPU state (may be slow or fail with a dead GPU)
T=120 run nvidia-smi-q.txt nvidia-smi -q
T=60  run nvlink-status.txt nvidia-smi nvlink -s
T=60  run nvlink-errors.txt nvidia-smi nvlink -e
T=60  run topo.txt nvidia-smi topo -m

# 5. Full NVIDIA bug report; safe mode if it hangs
[ -n "${SKIP_BUGREPORT:-}" ] || T=600 run bug-report-run.txt nvidia-bug-report.sh --output-file "$DIR/nvidia-bug-report.log" \
  || T=600 run bug-report-safe-run.txt nvidia-bug-report.sh --safe-mode --output-file "$DIR/nvidia-bug-report-safe.log"

{ echo "Xid $XID on $BUS at $TS"; grep -E 'Xid|PmuHalted|vbios_override' kernel.log; } > SUMMARY.txt
chmod -R a+rX "$DIR"
logger -p kern.crit -t gpu-xid "capture finished: $DIR"
wall "Xid capture finished: $DIR" 2>/dev/null
