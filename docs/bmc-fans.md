# MC62-G40 BMC: fan control and the "all fans to 100 %" spikes

Board: Gigabyte MC62-G40 (WRX80), BMC AST2600 with AMI MegaRAC firmware **13.02.10**, BIOS R06.

## Symptom

Every few minutes all fans (case, CPU, chipset) jump to full speed for 40–75 s, then drop back.
The SEL has no entry for it, and temperatures are flat.

## Cause (measured)

The BMC enters a fail-safe mode whenever **any fan runs at ~600 RPM** as the BMC reads it, even
though the configured thresholds are much lower (LNC 300 / LCR 150). The BMC reads fans in
150 RPM steps and never reports below ~450–600. The more fans sit near 600 RPM, the more often
the spikes happen.

| Test | Fans at ≤ 600 RPM | Spikes |
|---|---|---|
| All fans 30 % | case fans + CPU (450–600) | every ~2.5 min |
| CPU 55 %, rest 30 % | case fans (600) | every ~2.5 min |
| Case 55 %, CPU on its own rule (600) | CPU | every ~13 min |
| All fans 55 % | none (CPU 900, case 1050) | none in 25 min |
| Case 35 % (600 RPM) | case fans | after 17 min |
| **Case 40 % (750), CPU min 35 % (750)** | **none** | **none in 50 min** |

Ruled out: the profile rules themselves (a single fixed rule also spikes), empty fan headers,
and the `VR_DIMMG1_IOUT` sensor.

**Rule of thumb: no fan may sit at ≤ 600 RPM. With 150 RPM steps, 750 RPM is the lowest useful
idle speed.** `tools/tune_floor.py` finds the lowest duty per fan group automatically (bottom-up,
20 min per step, 30 min confirmation).

## Sensor map (this board)

| Sensor # | Name | Notes |
|---|---|---|
| 184 | CPU0_FAN | CPU cooler |
| 185 | FCH_FAN | chipset fan (≥ 1050 RPM at 30 %, never an issue) |
| 189, 190 | SYS_FAN4, SYS_FAN5 | case fans in front of the GPUs (in this build) |
| 186–188, 160–162 | SYS_FAN1–3, SYS_FAN6–8 | empty here |
| 1 | CPU0_TEMP | |
| 2 | PCH_TEMP | |
| 8–11, 13 | HIC_TEMP_0..4 | PCIe area, useful as a safety net for GPU-side fans |
| 14 | VR_P0_TEMP | CPU VRM |

Duty → RPM measured here: CPU fan (Noctua) 30 % = 450–600, 35 % = 750, 55 % = 900. Case fans
30–35 % = 600, 40 % = 750, 55 % = 1050, 70 % = 1350.

## How to control the fans

- **No `ipmitool raw` command** sets fan duty on Gigabyte BMCs (others have scanned every OEM
  netfn on MZ32/MZ72/MZ92/R282).
- The **Redfish OEM** fan profile (`/redfish/v1/Chassis/Self/Thermal/FanprofileService/Fanprofile`)
  accepts PUTs, but on this firmware they did not take effect, and its Thermal readings looked
  cached.
- The **web API** (what the web UI uses) works:

| Call | Purpose |
|---|---|
| `POST /api/session` (form `username`, `password`) | login → cookie + `CSRFToken` (send it as `X-CSRFTOKEN`) |
| `GET /api/sensors` | live readings |
| `GET /api/settings/fanprofile/collection` | saved profiles |
| `PUT /api/settings/fanprofile/collection/<name>` | change a profile (full object) |
| `GET` / `POST /api/settings/fanprofile/mode` (`{"strMode": "<name>"}`) | active profile |
| `GET /api/logs/event` | SEL |
| `DELETE /api/session` | logout |

Changes take 20–60 s to apply. Profiles survive host reboots. Policy type 2 = Slope (linear between
`arrRef` points); **avoid type 1 = Step**, which put this BMC in fail-safe mode. A fixed duty is a
Slope policy on an always-present sensor: `arrRef [0, 127]`, `arrDuty [D, D]`.

## Profile used here (`gpufan`)

A copy of `default` without the case fans in the factory rules, plus:

| Sensors | Fans | Ref (°C) | Duty (%) | Purpose |
|---|---|---|---|---|
| CPU0_TEMP | SYS_FAN4/5 | 0 → 127 | 40 → 40 | fixed duty for the GPU case fans (a daemon can update it) |
| CPU0_TEMP | CPU0_FAN | 45 / 70 / 85 | 35 / 85 / 100 | factory CPU rule, minimum raised to 35 % |
| HIC_TEMP_0..4 | CPU0_FAN | 35 / 80 | 35 / 100 | factory rule, minimum 35 % |
| VR_P0_TEMP | CPU0_FAN | 45 / 95 | 35 / 100 | factory rule, minimum 35 % |
| PCH_TEMP | FCH_FAN | 48 / 95 | 30 / 100 | factory |
| HIC_TEMP_0..4 | SYS_FAN4/5 | 60 / 75 | 40 / 100 | safety net if PCIe-area temperatures rise |

`tools/bmc.py duty N` changes the fixed duty. `tools/bmc.py mode default` goes back to factory.

## Notes

- **A power cut reset this BMC to factory settings** (13.02.10): default password, custom fan profiles
  gone, new DHCP address. Keep a copy of your profile (`tools/bmc.py profiles`) and the password in a
  file. Recovery from the host, without BMC credentials:
  `sudo ipmitool user list 1`, then `sudo ipmitool user set password <id> '<password>'` and
  `sudo ipmitool user enable <id>`. [`gpu-fancontrol`](gpu-fancontrol.md) does both automatically.

- Without the dedicated management LAN port (MLAN) cabled, the BMC used NC-SI and the host could
  not reach it.
- The Redfish Host Interface (`usb0`, BMC at 169.254.0.17) only appears with
  `power_save_mode=0`, the BMC resets that on every reboot, and Redfish on `usb0` accepted
  requests **without credentials**. Not used.
- Newer firmware exists (BIOS R10–R14, BMC 13.06.x; 13.06.05 patches an AMI MegaRAC
  vulnerability). Not tested here.
- Related: [Level1Techs: MC62-G40 BMC ignoring custom fan profiles](https://forum.level1techs.com/t/gigabyte-bmc-is-ignoring-my-custom-fan-profiles/241149),
  [mz32-fan-control](https://github.com/moriyasujapan/mz32-fan-control).
