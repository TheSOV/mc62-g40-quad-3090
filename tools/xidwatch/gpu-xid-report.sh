#!/bin/bash
# Follow-up summary: recoveries, Xids across all saved boots, boots, captures, current state.
echo "== Recoveries (/var/log/gpu-xid/recovery.log)"
tail -n 40 /var/log/gpu-xid/recovery.log 2>/dev/null || echo "(none)"
echo; echo "== Xids in all saved boots (Xid 45 omitted)"
journalctl _TRANSPORT=kernel --no-pager -o short-iso -g 'NVRM: Xid' 2>/dev/null | grep -vE 'Xid \(PCI:[^)]*\): 45,' | awk '/^-- Boot/{h=$0; next} {if(h){print h; h=""}; print substr($0,1,160)}'
echo; echo "== Boots"
journalctl --list-boots --no-pager | tail -n 10
echo; echo "== Captures"
ls -1d /var/log/gpu-xid/*-xid* 2>/dev/null
echo; echo "== Current state"
nvidia-smi --query-gpu=index,pci.bus_id,uuid,pstate,temperature.gpu,power.draw,fan.speed --format=csv,noheader
systemctl is-active gpu-xidwatch gpu-telemetry | paste -sd' '
