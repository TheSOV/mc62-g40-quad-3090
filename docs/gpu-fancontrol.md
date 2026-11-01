# gpu-fancontrol: case fans from GPU temperatures

A small daemon that drives the case fans in front of the GPUs (BMC `SYS_FAN4/5`) from the hottest
GPU reading. **The GPUs keep their own fan control** (their VBIOS curve; the two blower cards run the
edited image from [gpu-vbios-fan-floor.md](gpu-vbios-fan-floor.md)), so if the daemon dies, each
GPU still protects itself.

## How it works

- Every 2 s it reads core (NVML), hotspot and GDDR6X (BAR0 MMIO, root) on every GPU.
- Each sensor type has a curve, and the highest demand across all GPUs wins:

| Sensor | Curve (°C → duty %) |
|---|---|
| Core | 55 → 40, 60 → 55, 70 → 80, 78 → 100 |
| Hotspot | 65 → 40, 80 → 60, 90 → 100 |
| GDDR6X | 75 → 40, 90 → 70, 100 → 100 |

- It writes the fixed duty of the BMC profile `gpufan` (see [bmc-fans.md](bmc-fans.md)).
  **Minimum 40 % (750 RPM)**: at ~600 RPM the BMC fail-safe sends every fan to 100 %.
- **Up at once, down slowly:** at most −5 % per 10 s, and only if the duty for T + 3 °C is lower too
  (hysteresis). BMC writes are slow (applied in 20–60 s) and rate-limited.
- If the BMC loses the `gpufan` mode (e.g. after a BMC reset), the daemon re-applies it. If the BMC
  IP changed (DHCP), it finds it again with `ipmitool lan print 1`.

## Safety

| Event | Result |
|---|---|
| Core reading missing or implausible | failsafe duty (100 %) |
| Service stopped, or main process crashes | `ExecStopPost` sets 100 % (`--failsafe`), then `Restart=always` after 5 s |
| Daemon hangs | systemd watchdog (120 s) restarts it |
| Host down | the BMC keeps the last duty plus its own PCIe-temperature (HIC_TEMP) rule |

Tested on this machine:

- **Load on one GPU** (core up to 74 °C): duty 40 → 90 % within ~40 s, then back to 40 % in steps of
  5 % after the load.
- **`kill -9` of the main process:** `FAILSAFE (requested): 100%` at once, restarted 6 s later, ramped
  back down.
- **`systemctl restart`:** failsafe at 100 %, then down to 40 % in about 2 minutes.

Note: `systemctl kill -s KILL` kills every process in the unit, including the `ExecStopPost`
process, so it is not a valid crash test. Kill only the main PID:
`sudo kill -9 $(systemctl show -p MainPID --value gpu-fancontrol)`.

## Install

Needs `python3-pynvml`, `ipmitool`, and the profile `gpufan` on the BMC (its first policy must be the
fixed duty for SYS_FAN4/5, see [bmc-fans.md](bmc-fans.md)). BMC credentials go in
`~/.config/gpu-fancontrol/bmc.env` (see [examples/bmc.env.example](../examples/bmc.env.example)).

```bash
python3 tools/gpu_fancontrol.py --dry-run     # compute and print, no BMC writes
sudo ./install.sh                             # /opt/gpu-fancontrol, /etc/gpu-fancontrol, systemd
journalctl -u gpu-fancontrol -f
```

The settings live in `/etc/gpu-fancontrol/config.toml` (template: [examples/config.toml](../examples/config.toml)).
Restart the service after changing it. A restart passes through the failsafe duty, so expect the
fans to spin up for about 2 minutes.
