# Xid 62 "PMU has halted" on an RTX 3090: diagnosis and recovery without a reboot

One of the two converted RTX 3090 blowers (see [gpu-vbios-fan-floor.md](gpu-vbios-fan-floor.md))
dies every few hours to days, almost always with no load. Driver 615.71.09 (open modules, GSP
firmware), Ubuntu 26.04, kernel 7.0.

```
NVRM: Xid (PCI:0000:41:00): 62, 324f811e 0000dcb0 00000000 202cc3ca 202ccede 202ccc5e 202cbb7c 202cc690
NVRM: GPU2 _kgspRpcGspEventPmuHalted: Received signal from GSP that PMU has halted.
NVRM: Xid (PCI:0000:41:00): 154, GPU recovery action changed from 0x0 (None) to 0x1 (PF FLR)
```

Xid 62 is `PMU_HALT_ERROR`: the PMU, the falcon microcontroller that runs power, clocks and fans,
has stopped. The driver marks the GPU fatal (`kernel_gsp.c`, `_kgspRpcGspEventPmuHalted`) and every
call returns `NV_ERR_RESET_REQUIRED`. `nvidia-smi -r` answers `Not Supported` on GeForce.

**Short version:** no fix is known. The same signature is reported on Turing, Ampere, Ada and
Blackwell with drivers 580 to 615. **A PCI reset from sysfs does bring the GPU back without
rebooting**, as long as NVLink peers are reset with it. [`tools/xidwatch`](../tools/xidwatch) does
it automatically.

## Occurrences

Card A (`29daefaf`) and card B (`25a0c115`) are the two converted blowers, each with a VBIOS override.

| # | When | Bus | Card | Image | Before |
|---|---|---|---|---|---|
| 1 | 11-02 23:49 | 01:00 | B | fan floor 1250, 2 channels | idle the whole boot (2 h) |
| 2 | 11-04 03:38 | 01:00 | B | same | idle, load ended 67 min before |
| 3 | 11-04 22:29 | 41:00 | B | same, cards swapped between slots | idle, load ended 50 min before |
| 4 | 11-05 00:46 | 41:00 | B | `fan_channels=1`, floor 1250 | **19 s after a load ended** |
| 5 | 11-18 05:44 | 41:00 | B | same | 16 s after a load ended (P2 → P5) |
| 6 | 11-18 06:09 | 41:00 | B | same | 16 s after a load ended, 22 min after boot |
| 7 | 11-18 16:31 | 41:00 | B | same | 16 s after a load ended |

- **The failure follows the card**, not the slot: after swapping cards A and B, it moved with B.
- **The payload is identical every time**, byte for byte.
- **Not thermal and not power:** 37–64 °C at the time; BMC 12 V rail at 11.96 V, no SEL events.
- **No PCIe errors** in the AER status registers. ASPM and runtime D3 are off on all GPUs.
- **Not the phantom fan channel:** occurrence 4 happened with the one-channel image.
- **It does not need hours:** occurrence 4 came 47 min after the GPU had been re-initialised.
- **Occurrence 4 happened on a P-state drop.** A load ended at 00:46:29. At 00:46:48–49 all four GPUs
  dropped from P2 to P8 (PCIe gen 4 → gen 1) together, and card B's PMU halted in that same second.
  Occurrences 2 and 3, though, were in steady P8 with no transition in the 1 s telemetry.
- The same cards ran for months on a Supermicro H11SSL-i (PCIe 3.0) with Windows and the Game Ready
  driver, with the stock VBIOS, and never failed.

## Inference, not training, and a precursor (2026-11-18)

From the 1 s telemetry of 11-11 to 11-18 and the captures
([`tools/xidwatch/analysis`](../tools/xidwatch/analysis)):

- **All seven occurrences had an inference server (vLLM) holding ~23 GB on card B.** None in ~12
  days of fine-tuning in between, with a similar number of load → idle transitions.
- **Occurrences 5-7 were not at idle.** Sustained 99 % load, the load ends, ~16 s idle in P2 at
  1695 MHz, and the PMU halts exactly on the P2 → P5 drop, the step where the memory clock goes from
  9501 to 810 MHz. That drop happened 61 times in those 8 days, on training days too; it is the moment
  of failure, not the cause. The clock spike at the end of a load (~1920 MHz) is normal and is not it.
- **Precursor:** 10-50 min before each of occurrences 5-7, card B reported SW power cap
  (`clocks_event_reasons` bit `0x4`) while drawing 150-190 W under a 220 W limit, with the other three
  cards at 210-220 W on the same work (power ratio 0.79-0.88). Idle P2 power also fell to 75-108 W
  instead of ~121 W. Temperatures were normal (core 64-68 °C, VRAM 74-82 °C). In 708 ten-minute
  windows it shows up only before these failures. Our reading: the PMU's power management misbehaves
  before the PMU halts.
- **Detector:** [`gpu_precursor.py`](../tools/xidwatch/gpu_precursor.py), run inside
  `gpu_telemetry.py`. With all GPUs at ≥ 90 %, a sample is suspect if a GPU has SW power cap and draws
  under 0.9 × the mean of the others; it alerts (journal tag `gpu-precursor`, `wall`,
  `/var/log/gpu-xid/precursor.log`, plus `nvidia-smi -q -d POWER,CLOCK,PERFORMANCE,VOLTAGE`) when ≥ 30 %
  of the last 10 min are suspect. Replayed over 11-11 to 11-18 it fires 1.5, 13 and 8.5 min before
  occurrences 5-7, with no false positives (at 15-20 % it also fires on an uneven training load). Replay:
  `python3 gpu_precursor.py /var/log/gpu-telemetry/*.csv*`.
- **Power limit:** the 220 W limit and the VBIOS override arrived on the same day (11-01), and there was
  no Xid before; the two cannot be separated with this data. Under load card B sits at the limit
  100 % of the time and the precursor is a limiter fault, so since 11-18 18:03 card B alone runs at 260 W
  (`WATTS_BY_UUID` in the power-limit script of
  [quad-3090-llm-bench](https://github.com/TheSOV/quad-3090-llm-bench)). Under sustained load: 231-260 W,
  core 65-68 °C, VRAM 76-82 °C, the same VRAM as at 220 W.

## Other people with the same problem

- [open-gpu-kernel-modules#1307](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1307):
  RTX 5060 Ti / 5070 / 5070 Ti / 5090, drivers 580–615 including 615.71.09. Idle, after 2–5 h, no
  precursor. Same payload layout (`<id> <0000xxxx> 00000000` + five `0x20xxxxxx` words, likely PMU
  code addresses). About 5 s later some get a GSP CrashCat dump
  (`GSP task watchdog timeout @ pc:…, partition:4#0, task:3`); this server never logged one.
- [#1473](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1473): RTX 4070, 615.78.08, after
  S3 resume. One report says only removing standby power for ≥60 s cleared it.
- [RTX 2080 Ti thread](https://forums.developer.nvidia.com/t/xid-62-pmu-halt-on-rtx-2080-ti-identical-payload-3x-gsp-on-and-off-gpu-unrecoverable/384008):
  identical payload repeated, **with GSP on and with GSP off**. NVIDIA filed internal bug 6869496.
- [RTX 4060 Laptop, idle](https://forums.developer.nvidia.com/t/rtx-4060-laptop-xid-62-xid-120-gsp-crashes-in-idle-reproducible-on-both-open-and-proprietary-driver-610-43-02-610-43-03/377032),
  open and proprietary modules.
- [RTX PRO 6000 under vLLM](https://forums.developer.nvidia.com/t/xid-62-xid-154-gsp-pmu-halt-crash-on-rtx-pro-6000-blackwell-during-llm-inference/375503).
- [dev.to: Troubleshooting NVIDIA Error XID 62](https://dev.to/faliye/troubleshooting-nvidia-error-xid-62-1f04)
  (RTX 5080): failure on a P8 → P0 transition; locking a minimum clock with `nvidia-smi -lgc`
  (tested for 10 minutes only); the sysfs reset procedure used below.

What others tried without success: disabling ASPM, persistence mode, a lower power limit, other
driver versions (580/590/595/610/615), `NVreg_EnableGpuFirmware=0`. One RTX 2070 Super ran 7 days
clean with GSP off on the proprietary 580 module; it did not help a 2080 Ti or a 3060 Ti. GSP off is
not an option here anyway: the VBIOS override loader needs GSP. No release note mentions a fix
(checked up to 615.78.08).

## Collecting data NVIDIA can use

- **`nvidia-debugdump -N -f nvlog.zip`** works right after the Xid: about 2.7 MB with the driver's
  internal buffers, including the PMU and GSP logs (`PMU1`, `PMU2`, `GSP1`…). It is NVIDIA's binary
  format; only they can decode it. `nvidia-debugdump -D` adds `rm_<n>.pb` for the other GPUs; the dead
  one returns `GPU requires reset`.
- **GSP firmware logs are off on release drivers.** `NVreg_EnableGpuFirmwareLogs` defaults to 2,
  which only logs on debug builds. Set it to 1
  ([`nvidia-firmware-logs.conf`](../tools/xidwatch/nvidia-firmware-logs.conf)). It applies at the
  next module load; a GPU reset does not reload the module.
- `nvidia-bug-report.sh` has the kernel log and `nvidia-smi -q`, but no PMU state.
- The PMU cannot be dumped directly from user space. The nvlog is the closest thing.

## Recovery without a reboot

The sysfs procedure: stop everything that holds `/dev/nvidia*`, unbind the GPU (and its HDMI audio
function), PCI reset (FLR / bus reset), remove, rescan. Without the reset step the GSP reportedly
fails to boot (`unexpected WPR2 already up`).

```bash
sudo tools/xidwatch/gpu-reset-one.sh 0000:41:00                # dry run
sudo tools/xidwatch/gpu-reset-one.sh 0000:01:00 0000:21:00 0000:41:00 0000:42:00 --yes
```

Results here:

- **On a healthy GPU:** back in ~20 s. The VBIOS override is applied again (the loader hooks GPU
  init, not boot), fan at 1250 RPM, Recovery Action None. The other GPUs are not affected: each GPU
  sits on its own root port (`reset_method`: `flr bus`).
- **After a real Xid 62 (occurrence 4): the GPU came back healthy.** It took 84 s including stopping
  vLLM.
- **NVLink side effect:** card B is bridged to card A. Resetting B alone dropped the links, and A
  then logged `Xid 74, NVLink: fatal error detected on link 0..3`, followed by `Xid 154`, and needed a
  reset as well. **Reset NVLink peers together.** Resetting all four GPUs at once worked cleanly:
  Recovery Action None on all four, NVLink back at 4 × 14.06 GB/s.

If a step hangs (a process stuck in the driver), escalate: `sudo reboot`; if shutdown hangs,
SysRq `s`, `u`, `b` through `/proc/sysrq-trigger`, which skips the driver's shutdown callback; last
resort, `ipmitool chassis power cycle`. If the GPU is still in a bad state after a warm reboot, power
off for a minute (see #1473).

## Automatic recovery: [`tools/xidwatch`](../tools/xidwatch)

| File | Role |
|---|---|
| `gpu_telemetry.py` + `gpu-telemetry.service` | One `nvidia-smi -l 1` process → `/var/log/gpu-telemetry/YYYY-MM-DD.csv` (P-state, clocks, throttle reasons, power, temperature, fan, PCIe gen/width, utilisation). 7 days kept, gzipped |
| `gpu_xidwatch.py` + `gpu-xidwatch.service` | Follows the kernel log. On Xid 62/74/79/119/120, or a 154 that does not go back to None, runs `gpu-xid-recover.sh`; on other Xids, only the capture |
| `gpu-xid-capture.sh` | `/var/log/gpu-xid/<date>-<bus>-xid<N>/`: debugdump first, then kernel/service logs, last 20 min of telemetry, `lspci -vvv`, BMC SEL/SDR, `nvidia-smi -q`, NVLink, bug report |
| `gpu-xid-recover.sh` | Capture → reset (all GPUs by default) → checks every GPU after 60 s → follow-up after 20 min. If the reset fails: reboot, at most 2 per 24 h, with the BMC watchdog armed |
| `gpu-reset-one.sh` | The sysfs reset, for one or more GPUs; dry run without `--yes` |
| `gpu-bmc-watchdog-off.service` | Disarms the BMC watchdog at boot |
| `gpu-xid-report.sh` | Summary: recoveries, Xids across all saved boots, boots, captures |
| `gpu-xid-recover.default` | `/etc/default/gpu-xid-recover`: on/off switches, reboot cap, services to stop and start. List the unit that launches vLLM: if vLLM is a child of another unit that is not listed, the reset aborts |
| `gpu_precursor.py` | Precursor detector fed by `gpu_telemetry.py` (see above) |
| `analysis/` | Scripts over the saved telemetry: states per day, load → idle drops, features of each drop, power ratio between cards, clock spike at load end |

```bash
sudo tools/xidwatch/install.sh
gpu-xid-report
```

The watchdog is the IPMI one (`Set Watchdog Timer`, SMS/OS use, hard reset), armed with
`ipmitool raw 0x06 0x24 0x04 0x01 0x00 0x10 <lsb> <msb>` + `ipmitool mc watchdog reset`. The
AST2600 BMC on the MC62-G40 accepts it (`ipmitool mc watchdog get` → SMS/OS, Hard Reset). The board
has no `/dev/watchdog`.

## Still open

- Whether the converted cards' generic PG132 image (or the override) plays a part. Removing the
  override is not an option here (the stock fan curve is too loud).
- Whether 260 W on card B reduces the failures: observing for a few days from 11-18.
- If it does not: a stepped memory-clock descent. Card B supports memory clocks of 405, 810, 5001,
  9501 and 9751 MHz (5001 never shows up in the telemetry). A service would raise the floor while the card is
  busy (`nvidia-smi -lmc 9501,9751`) and step down at idle (`-lmc 5001,9751` → `-lmc 810,9751` →
  `-rmc`) instead of the direct 9501 → 810 drop. Locking clocks permanently (`-lgc`) was rejected:
  ~120 W per card at idle.
- After the timeout fix, the automatic recovery path has not yet run on a real Xid.
- Sending the nvlog to NVIDIA (#1307 or linux-bugs@nvidia.com): there is no confirmed Ampere case there.
