# RTX 3090 blower: lowering the 3000 RPM fan floor (Linux, in memory, no flashing)

Two RTX 3090 Founders Edition PCBs converted to a single-fan blower cooler, running a generic
**PG132** reference image (subsystem `10DE:1454`, VBIOS `94.02.59.00.05`). With the driver loaded,
the blower never goes below **~3330 RPM** (NVML: 42 %, manual 30 % = 3000 RPM target): loud at idle.
Before the driver loads (POST, or with the module unloaded) the same fan is quiet.

**Why:** with GSP firmware the driver maps the fan percent to an RPM target,
`RPM = min + (pct − 30) / 70 × (max − min)`, using the cooler-table limits of the VBIOS. This image
stores **min 3000 RPM / 60 %**, so "30 %" means 3000 RPM. No driver option or registry key changes
it.

**Result:** with an edited image loaded in memory at boot, the idle speed drops to **~1415 RPM**,
and the fan still ramps normally under load.

> ⚠️ **Linux only.** Tested with driver 615.71.09 (open modules) on Ubuntu 26.04 / kernel 7.0.
> Nothing here was tested on Windows. The card's flash is never written. If the firmware rejects the
> image, the card boots with its stock VBIOS.

## Tools

- [NVIDIA VBIOS Vix Editor](https://github.com/Panchovix/nvidia-vbios-vix-editor) (0.1.38) to edit the image
- [NVIDIA Linux VBIOS Vix Loader](https://github.com/Panchovix/nvidia-linux-vbios-vix-loader) (0.1.0) to load it at boot
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

## 2. Edit the fan floor

Stock fan table on this image: min 3000 RPM / 60 %, curve 3000 @ 60 °C → 4600 @ 70 °C → 5000 @ 75 °C,
max 5000. The minimum can only go down together with point 1 of the curve. This mirrors the
1250 RPM / 25 % floor of a Gigabyte 3090 Turbo image:

```bash
nvbios edit gpu0.rom gpu0-edited.rom \
  --set fan_1_min_rpm=1250 --set fan_2_min_rpm=1250 \
  --set fan_1_curve_1_rpm=1250 --set fan_2_curve_1_rpm=1250 \
  --set fan_1_min_pct=25 --set fan_2_min_pct=25 \
  --set fan_1_curve_1_pct=25 --set fan_2_curve_1_pct=25
nvbios normalize gpu0-edited.rom gpu0-loader.bin --representation ampere-relocated-v1
```

8 fields, 12 bytes. Points 2–3 and the maximum stay stock. Make one payload per card: even with the
same VBIOS version, the two dumps were not byte-identical.

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

After the reboot, the kernel log should show:

```
NVRM: vbios_override: GPU 0000:01:00 selected: 466432 bytes from offset 0x9400 staged for GSP
```

`rejected override … using stock VBIOS` means the firmware ignored it. If anything goes wrong, pick
the normal entry (without "VBIOS override") in the GRUB menu, or run `sudo nvbios system disable`.

## Results

| Idle | Stock image | Edited image |
|---|---|---|
| Card 0000:01:00.0 | ~3330 RPM | **~1413 RPM** |
| Card 0000:41:00.0 | ~3330 RPM | **~1418 RPM** |

Load test (`tools/burn_test.py`, FMA kernel at ~265–272 W, 5–6 min, one card at a time; hotspot and
VRAM from `tools/gputemps.py`):

| | 0000:01:00.0 | 0000:41:00.0 |
|---|---|---|
| Core | 73 °C | 71 °C |
| Hotspot | 83 °C | 81 °C |
| GDDR6X | 78 °C | 76 °C |
| Fan peak | ~4750 RPM | ~4640 RPM |

The fan starts ramping at ~64 °C, follows the stock upper curve, and is back at ~1415 RPM within a
minute after the load stops. It settles at ~1415 RPM rather than 1250, probably the blower's own floor.

Raw logs: [gist](https://gist.github.com/TheSOV/aeac21be2e075b3e7c029b7388a360f3).
Report upstream: [nvidia-linux-vbios-vix-loader#2](https://github.com/Panchovix/nvidia-linux-vbios-vix-loader/issues/2).

## After driver or kernel updates

The loader rebuilds on kernel updates. On a driver update it downloads the matching GitHub tag,
which on this kernel will probably fail to build for the same CXL reason. Before rebooting, check
`sudo nvbios system status`, and redo step 4 with the new version if needed.
