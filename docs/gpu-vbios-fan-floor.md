# RTX 3090 blower: lowering the 3000 RPM fan floor (Linux, in memory, no flashing)

Two RTX 3090 Founders Edition PCBs converted to a single-fan blower cooler, running a generic
**PG132** reference image (subsystem `10DE:1454`, VBIOS `94.02.59.00.05`). With the driver loaded,
the blower never goes below **~3330 RPM** (NVML: 42 %, manual 30 % = 3000 RPM target): loud at idle.
Before the driver loads (POST, or with the module unloaded) the same fan is quiet.

**Why:** with GSP firmware the driver maps the fan percent to an RPM target,
`RPM = min + (pct − 30) / 70 × (max − min)`, using the cooler-table limits of the VBIOS. This image
stores **min 3000 RPM / 60 %**, so "30 %" means 3000 RPM. No driver option or registry key changes
it.

**Result:** with an edited image loaded in memory at boot, the blower idles at **1250 RPM (±8)**,
and still ramps normally under load. Two edits are needed: the fan floor, and **`fan_channels` 2 → 1**
(the PG132 image declares two fans, the blower has one; see [the phantom fan channel](#the-phantom-fan-channel)).

> ⚠️ **Linux only.** Tested with driver 615.71.09 (open modules) on Ubuntu 26.04 / kernel 7.0.
> Nothing here was tested on Windows. The card's flash is never written. If the firmware rejects the
> image, the card boots with its stock VBIOS.

## Tools

- [NVIDIA VBIOS Vix Editor](https://github.com/Panchovix/nvidia-vbios-vix-editor) (0.1.38) to edit the image
- [NVIDIA Linux VBIOS Vix Loader](https://github.com/Panchovix/nvidia-linux-vbios-vix-loader) to load it at boot
  (first set up with 0.1.0, currently 0.1.3)
- `nvflash` (5.867, read-only `--save`) to dump the full ROM

## 1. Dump the full ROM

`/sys/bus/pci/devices/*/rom` only returns ~154 KB, which the editor refuses
(`NPDS data chain lacks a complete last image`). nvflash needs the driver unloaded, even for reading:

```bash
sudo systemctl stop nvidia-persistenced
sudo modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia_peermem nvidia
sudo nvflash --list                      # the index matches nvidia-smi
sudo nvflash --index=0 --save gpu0.rom   # ~1 MB; --save only reads
sudo modprobe nvidia && sudo systemctl start nvidia-persistenced
```

Keep the originals. **Never flash an edited image:** its signature is broken.

## 2. Edit the image: one fan channel, then the fan floor

Stock fan table on this image: 2 fan channels, min 3000 RPM / 60 %, curve 3000 @ 60 °C → 4600 @ 70 °C
→ 5000 @ 75 °C, max 5000. The minimum can only go down together with point 1 of the curve.

`fan_channels` has to be edited on its own (the editor refuses it together with other fields:
`SEPARATE_EDIT_REQUIRED`), so it takes two passes:

```bash
nvbios edit gpu0.rom gpu0-1ch.rom --set fan_channels=1
nvbios edit gpu0-1ch.rom gpu0-1ch-fan1250.rom \
  --set fan_1_min_rpm=1250 --set fan_1_curve_1_rpm=1250 \
  --set fan_1_min_pct=22   --set fan_1_curve_1_pct=22
nvbios verify gpu0-1ch-fan1250.rom
nvbios normalize gpu0-1ch-fan1250.rom gpu0-loader.bin --representation ampere-relocated-v1
```

22 % is what the two-channel measurements suggested for 1250 RPM on this blower (~56 RPM per %).
With one channel the RPM loop holds 1250 anyway, and NVML shows measured 30 % = target 30 %. Points
2–3 (load) and the maximum stay stock. Make one payload per card from **its own** dump: even with the
same VBIOS version, the two dumps were not byte-identical.

### The phantom fan channel

The generic PG132 image is for a two-fan design, so with the blower the driver sees two fans: fan 0
(the blower) and fan 1, which always reads **0 RPM against a 30 % target**, on both cards, with the
stock and with the edited image. It has no zero-RPM mode, so to the firmware it is a permanently
stalled fan. The Gigabyte Turbo (a factory blower) has `fan_channels=1`.

With two channels the blower did not reach its RPM target, it settled ~12.5 % above it:

| Table minimum | Measured, 2 channels | Measured, `fan_channels=1` |
|---|---|---|
| 3000 RPM (stock) | ~3330 | — |
| 1250 RPM | ~1415 | **1250 ±8** |
| 1500 RPM | ~1687 | — |

So the ~1415 RPM reported earlier was **not** the blower's floor, as first assumed. With one channel
the RPM loop holds the target exactly and NVML shows a single fan per card. (A similar mismatch,
GSP-RM enumerating a different number of fans than the board has, is reported on a
[Gigabyte RTX 4090](https://forums.developer.nvidia.com/t/fan-issues-in-gigabyte-rtx-4090-on-linux-but-not-windows/376051).)

To watch it: `python3 tools/fanwatch.py 60 1` (RPM, measured % and target % per fan, read-only).

## 3. Driver 615 (open) and build prerequisites

```bash
sudo apt install -y build-essential linux-headers-$(uname -r)
sudo ubuntu-drivers install nvidia:615-open      # 615.71.09 on Ubuntu 26.04
```

## 4. Driver source: GitHub tag + Ubuntu's kernel 7.0 patches

The bare `615.71.09` tag **does not build on Ubuntu's kernel 7.0**
(`nv-linux.h: error: field 'cxlds' has incomplete type`). Ubuntu fixes this only in its DKMS package,
so apply those patches to a copy of the tag:

```bash
git clone --depth 1 --branch 615.71.09 https://github.com/NVIDIA/open-gpu-kernel-modules ogkm
rm -rf ogkm/.git
mkdir dkms && (cd dkms && apt-get download nvidia-dkms-615-open && dpkg-deb -x nvidia-dkms-615-open_*.deb x)
P=dkms/x/usr/src/nvidia-615.71.09/patches
patch -p1 -d ogkm/kernel-open < $P/buildfix_kernel_7.0-dma_buf_invalidate_mappings.patch
patch -p1 -d ogkm/kernel-open < $P/buildfix_kernel_7.0-cxl_structs.patch
```

Check `dkms.conf` in that package for the patches that apply to your kernel (`PATCH_MATCH`).
Neither patch touches the files the loader patches.

## 5. GRUB

The loader requires `GRUB_DEFAULT=saved`, with `saved_entry` set to the **entry id**, not an index.
`grub-set-default 0` makes `reconcile` stop with `foreign or unrelated boot default preserved`.

```bash
sudo cp /etc/default/grub /etc/default/grub.bak
sudo sed -i -e 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/' \
            -e 's/^GRUB_TIMEOUT_STYLE=.*/GRUB_TIMEOUT_STYLE=menu/' \
            -e 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=5/' /etc/default/grub   # visible menu = easy fallback
sudo update-grub
```

## 6. Loader

From a clone of the loader. Use `--jobs` (it defaults to 1, and a full build then takes 10+ minutes):

```bash
sudo ./nvbios system devices
sudo ./nvbios system prepare \
  --device 0000:01:00.0 /path/gpu0-loader.bin \
  --device 0000:41:00.0 /path/gpu2-loader.bin \
  --driver-source /path/ogkm --userspace deb --jobs 24 --output /path/vix-profile
sudo ./nvbios system install --repository "$PWD"
sudo /usr/local/bin/nvbios system enroll --profile /path/vix-profile/profile.json
# saved_entry must be the fallback entry id recorded in the profile:
sudo grub-set-default "$(sudo python3 -c 'import json;print(json.load(open("/path/vix-profile/profile.json"))["fallback_entry"])')"
sudo /usr/local/bin/nvbios system reconcile --kernel "$(uname -r)"
sudo /usr/local/bin/nvbios system enable --kernel "$(uname -r)"
sudo /usr/local/bin/nvbios system status
sudo reboot
```

**Adding a GPU later:** `enroll` refuses while enabled (`BOOTSTRAP_BLOCKED`). This order worked:
`disable` → `prepare` (all GPUs) → `enroll` → `grub-set-default <last ready vbios-… entry>` →
`reconcile` → `enable`.

**New images, or cards moved to other slots (loader 0.1.3):** the override is bound to the PCI
address and to the card's identity. After swapping two cards, `nvidia-vbios-reconcile` reported
`VBIOS_BLOCKED: identity changed`, and each card would have received the image built from the other
card's ROM. Re-enrolling fixes it:

```bash
sudo nvbios system prepare \
  --device 0000:01:00.0 /path/cardA-loader.bin --device 0000:41:00.0 /path/cardB-loader.bin \
  --driver-source /path/ogkm --userspace deb --jobs 24 --output /path/vix-profile-new
sudo nvbios system pre-install
sudo nvbios system enroll --profile /path/vix-profile-new/profile.json
sudo nvbios system reconcile --kernel "$(uname -r)"
sudo nvbios system enable --kernel "$(uname -r)"
sudo nvbios system status          # "ready": true, then reboot
```

After the reboot, the kernel log should show:

```
NVRM: vbios_override: GPU 0000:01:00 selected: 466432 bytes from offset 0x9400 staged for GSP
```

`rejected override … using stock VBIOS` means the firmware ignored it. If anything goes wrong, pick
the normal entry (without "VBIOS override") in the GRUB menu, or run `sudo nvbios system disable`.

## Results

| Idle | Stock image | Floor 1250, 2 channels | Floor 1250, 1 channel |
|---|---|---|---|
| Card A | ~3330 RPM | ~1413 RPM | **1242–1258 RPM** |
| Card B | ~3330 RPM | ~1418 RPM | **1242–1258 RPM** |

Load test (`tools/burn_test.py`, FMA kernel at ~265–272 W, 5–6 min, one card at a time; hotspot and
VRAM from `tools/gputemps.py`):

| | 0000:01:00.0 | 0000:41:00.0 |
|---|---|---|
| Core | 73 °C | 71 °C |
| Hotspot | 83 °C | 81 °C |
| GDDR6X | 78 °C | 76 °C |
| Fan peak | ~4750 RPM | ~4640 RPM |

The load test was done with the 2-channel image. The fan starts ramping at ~64 °C, follows the stock
upper curve, and is back at the floor within a minute after the load stops.

> ⚠️ One of the two converted cards hits a recurring **Xid 62 (PMU halted)**, with the two-channel and
> with the one-channel image (the stock image was never run on Linux long enough to tell). See [gpu-xid62-pmu-halt.md](gpu-xid62-pmu-halt.md): it is not
> known yet whether the image plays a part.

Raw logs: [gist](https://gist.github.com/TheSOV/aeac21be2e075b3e7c029b7388a360f3).
Report upstream: [nvidia-linux-vbios-vix-loader#2](https://github.com/Panchovix/nvidia-linux-vbios-vix-loader/issues/2).

## After driver or kernel updates

The loader rebuilds on kernel updates. On a driver update it downloads the matching GitHub tag,
which on this kernel will probably fail to build for the same CXL reason. Before rebooting, check
`sudo nvbios system status`, and redo step 4 with the new version if needed.
