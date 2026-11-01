# MC62-G40 + 4× RTX 3090: quiet fans for a home inference server

Notes and tools from building an LLM inference server (vLLM) on a **Gigabyte MC62-G40** (WRX80,
Threadripper PRO 5965WX) with **four RTX 3090**, on Ubuntu 26.04. The aim: as quiet as possible at
idle, without compromising cooling.

## What's here

| Problem | Finding | Doc |
|---|---|---|
| All fans jump to 100 % every few minutes | The BMC fail-safe fires when **any fan runs at ~600 RPM**, despite much lower thresholds. 750 RPM (case 40 %, CPU 35 %) is the floor here | [docs/bmc-fans.md](docs/bmc-fans.md) |
| No way to set BMC fan duty from Linux | No `ipmitool raw` command, and Redfish OEM PUTs don't apply. The **web API** does | [docs/bmc-fans.md](docs/bmc-fans.md) |
| RTX 3090 blower at 3330 RPM at idle | The VBIOS fan table stores a **3000 RPM minimum**. An edited image loaded **in memory** (no flashing) brings idle to **~1415 RPM** with normal ramp-up under load. Linux only | [docs/gpu-vbios-fan-floor.md](docs/gpu-vbios-fan-floor.md) |
| Vix Loader doesn't build on Ubuntu kernel 7.0 | Needs Ubuntu's DKMS patches (CXL, dma_buf) on top of the GitHub tag | [docs/gpu-vbios-fan-floor.md](docs/gpu-vbios-fan-floor.md#4-driver-source-github-tag--ubuntus-kernel-70-patches) |
| Case fans should follow the GPUs | `gpu-fancontrol`: a systemd daemon that sets SYS_FAN4/5 from the hottest GPU core/hotspot/VRAM, and goes to 100 % if it stops or crashes | [docs/gpu-fancontrol.md](docs/gpu-fancontrol.md) |
| No hotspot / VRAM temperature on GeForce | NVML 615 still doesn't report them. Read BAR0 MMIO (GA102) | [tools/gputemps.py](tools/gputemps.py) |

## Hardware

| | |
|---|---|
| Board | Gigabyte MC62-G40, BMC AST2600 (AMI MegaRAC 13.02.10), BIOS R06 |
| CPU | AMD Threadripper PRO 5965WX |
| GPUs | 2× RTX 3090 FE PCB converted to a single-fan blower (generic PG132 VBIOS, subsystem `10DE:1454`), 1× Gigabyte RTX 3090 Turbo, 1× RTX 3090 FE |
| Slots | PCIe1 FE · PCIe3 Turbo · PCIe5 converted · (PCIe6 empty) · PCIe7 converted |
| OS | Ubuntu 26.04, kernel 7.0, NVIDIA 615.71.09 (open kernel modules) |

## Tools

All in Python 3, standard library plus `python3-pynvml` for the GPU tools.

| Script | What it does |
|---|---|
| [`tools/gpu_fancontrol.py`](tools/gpu_fancontrol.py) | The case-fan daemon (installed by [`install.sh`](install.sh) with [`systemd/gpu-fancontrol.service`](systemd/gpu-fancontrol.service)) |
| [`tools/bmc.py`](tools/bmc.py) | BMC web API client and CLI: `sensors`, `profiles`, `mode`, `duty` |
| [`tools/tune_floor.py`](tools/tune_floor.py) | Finds the lowest spike-free duty, bottom-up, separately for case fans and CPU fan |
| [`tools/spikelog.py`](tools/spikelog.py) | Logs BMC fans and marks fail-safe spikes |
| [`tools/gputemps.py`](tools/gputemps.py) | Core (NVML), hotspot and GDDR6X (MMIO) temperatures for every GPU (root) |
| [`tools/burn_test.py`](tools/burn_test.py) | Compute load on one GPU (PTX kernel through libcuda, no CUDA toolkit) with a temperature cutoff |
| [`tools/gpufan_test.py`](tools/gpufan_test.py) | Manual GPU fan test through NVML (root), restores auto mode at the end |

BMC credentials go in `~/.config/gpu-fancontrol/bmc.env` or `/etc/gpu-fancontrol/bmc.env`
(chmod 600). See [`examples/bmc.env.example`](examples/bmc.env.example).

```bash
python3 tools/bmc.py sensors FAN
python3 tools/tune_floor.py            # takes 1-2 h; leaves profile 'gpufan' tuned
sudo python3 tools/gputemps.py 10      # every 10 s
python3 tools/burn_test.py 0 300 83    # GPU 0, 5 min, stop at 83 °C
```

## Other notes

- **NVLink:** an RTX A6000 2-slot bridge between the two converted cards (PCIe5 and PCIe7, correct
  spacing) shows every link inactive, with both the open and the proprietary driver. Still unresolved.
  Suspects: bridge seating, or the PG132 image on a PG136 board.
- **Power limit for inference:** planned sweep with NVIDIA GenAI-Perf / AIPerf against vLLM.

## Disclaimer

These are notes from one machine. Editing VBIOS images, running BMC fans close to their limits or
loading patched kernel modules can damage hardware or leave a system unbootable. Use at your own
risk. Not affiliated with Gigabyte, NVIDIA or the Vix projects.

## License

MIT
