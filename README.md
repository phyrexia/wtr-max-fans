# wtr-max-fans

Fan control for the AOOSTAR WTR MAX on Proxmox VE and Debian. The chassis fans follow the hard disks and the NVMe drive, can run quieter at night, and a separate guard watches the disk temperatures. You configure everything from a terminal menu.

> **Pre-release: under development, not ready for use.**
> Nothing in this README is available yet. Install and command descriptions are the planned v1.0 behaviour and may change before release.

Español: [README.es.md](README.es.md)

---

## Why this exists

The WTR MAX has two fan problems that the stock firmware does not solve:

- **The fan curve follows the wrong sensors.** The firmware curve reacts to two thermistors on the board. On one unit, the board sensors moved 1–2 °C while the disks moved 3–8 °C. The board sensors do not follow the disk cage or the NVMe drive.
- **The firmware can stop the disk-cage fans.** With PWM fans, the firmware curve can stop the rear fans. On the unit where this was measured, the disks reached 56 °C.

The Linux kernel does not drive the chip on this board (ITE IT8613E). The community driver `frankcrawford/it87` does, but it hides the attributes this project needs, so this project carries a three-line patch for it. Stock fans on some units are not PWM at all: they run at full speed whatever the firmware asks for.

`wtr-max-fans` keeps the chip in its own automatic mode and only changes the start temperature point of its curve, using the disk and NVMe temperatures as input. If the software stops, the firmware curve keeps working.

## Hardware

| Board | Status |
|---|---|
| AOOSTAR WTR MAX (DMI product `WTR MAX`, chip ITE IT8613E at Super-I/O port `0x2E`) | Supported in v1 |
| Another board with the same IT8613E chip | Expert mode only (see below) |
| Any other Super-I/O chip | Not supported, never |

Fan channels on the WTR MAX, as measured on one unit. Calibration confirms each one on your machine:

| Channel | Usual fan | Role in `wtr-max-fans` |
|---|---|---|
| `pwm2` | CPU | Not managed. It stays on the firmware curve. |
| `pwm3` | SYS, the bottom fan under the case | Managed, role `base`. Follows the hottest disk and the NVMe. |
| `pwm4` | MEM header | Often a 2-wire fan with no PWM and no tachometer. Not managed. |
| `pwm5` | SATA, the rear fans | Managed, role `disk-cage`. Follows the hottest disk. |

**Requirements**

- AOOSTAR WTR MAX.
- Proxmox VE 8 or 9, or Debian 12 or 13 (OpenMediaVault 7 is Debian 12).
- 4-pin PWM fans on the channels you want to control. Calibration checks each one.
- Internet access during install, to fetch packages.

**Expert mode for other boards with the same chip.** Some boards use the same chip but are not a WTR MAX, and their fan headers may be wired differently. The installer refuses them unless you pass `install --expert-board` or choose the expert flow in the menu. Expert mode:

1. Shows a warning and requires you to type `unsupported`.
2. Installs in **read-only mode**: the guard only monitors and alerts, and nothing is written to the fans.
3. Requires calibration. Each channel must be confirmed by you. "Not sure" leaves the channel unmanaged. There is no default map.
4. Enables fan control only after an explicit "Enable fan control" step.
5. Marks the installation as expert everywhere: on the status screen, in every alert subject, in the report, and in the metric `wtr_max_fans_board_support{support="expert"}`.

The chip check is never overridable.

## Features (v1 plan)

- **Disk-driven fan curves.** The disk-cage fans follow the hottest disk. The base fan follows the hotter of the hottest disk and the NVMe drive.
- **Three profiles:** `quiet`, `balanced` and `cool`. The same temperature points and the same safety ceiling in all three. The table is in the next section.
- **Night mode.** A local-time window, an optional later end on weekends, and an optional holiday file.
- **Thermal guard.** A separate service that turns the fans to full air when a disk gets hot. Shutdown is off unless you enable it. See [Safety](#safety).
- **Guided calibration.** Finds which fan is which, and whether each one responds to PWM. Restores the firmware values it touched when it is done. Each night floor is admitted per fan with the tachometer: a floor is kept only if the fan runs steadily and with margin, otherwise the next higher one is used. If none passes, that fan keeps its day floor at night (the night curve is still lower above 38 °C), and the wizard and the status screen say so.
- **Noise test.** Plays each profile's night level at 42 °C and asks whether it is acceptable. It then proposes the loudest acceptable profile.
- **Clean uninstall.** Restores the firmware values recorded at calibration, then removes the units, the module and the files it installed.
- **Optional Prometheus metrics,** written only when the textfile collector directory already exists.
- **Terminal menu** (whiptail) and a command line for scripts.

### Profiles

Temperature points are the same for all profiles: the fans sit at the floor up to 38 °C, reach the day value at 46 °C, and reach full speed (255) at 52 °C. At night the curve starts from the night floor and reaches the night value at 46 °C. Below 50 °C it is always lower than the day curve, and from 50 °C it equals the day curve. Values are PWM from 0 to 255, with the percentage in brackets.

| Setting | `quiet` | `balanced` | `cool` |
|---|---|---|---|
| Disk cage, floor | 102 (40 %) | 115 (45 %) | 128 (50 %) |
| Disk cage, day at 46 °C | 128 (50 %) | 153 (60 %) | 191 (75 %) |
| Disk cage, night floor | 76 (30 %) | 89 (35 %) | 102 (40 %) |
| Disk cage, night at 46 °C | 115 (45 %) | 128 (50 %) | 153 (60 %) |
| Base, floor | 64 (25 %) | 77 (30 %) | 100 (39 %) |
| Base, day at 46 °C | 77 (30 %) | 100 (39 %) | 128 (50 %) |
| Base, night floor | 51 (20 %) | 64 (25 %) | 77 (30 %) |
| Base, night at 46 °C | 64 (25 %) | 77 (30 %) | 100 (39 %) |

`balanced` is the measured comfort point on one unit: at night the rear fans run from 35 % to 50 % and the bottom fan from 25 % to 30 %, and the disks stay between 33 and 42 °C. `cool` is for warm rooms. The status screen suggests it when the 24-hour disk maximum goes above 46 °C.

The base fan never goes below 20 % PWM in any profile. The lowest value, the `quiet` night floor of 20 %, is used only where calibration confirms it with a tachometer. A `custom` profile lets you set every value in `config.ini`, within the safety rules.

The disks should stay in the 30–45 °C band over the long term. The NVMe drive should stay below 60 °C under sustained load. The CPU fan is never managed.

### Night mode

Night mode uses the system's local time. Set `night_start` and `night_end` (for example 21:00 and 09:00). You can add a later end on weekends and a holiday file. No holiday file is shipped. An unreadable holiday file is treated as a normal day, so the fans run at day values and not less air. A configuration where night equals day is an error: `status` shows it, and the curve falls back to the profile's factory values. To run the day curve all day, use `mode = day` or `night off`.

## Safety

**What the software never does**

- It never writes a managed fan below 20 % PWM (51 of 255).
- The controller never writes `pwmN` or changes the chip's mode. It only changes the start point of the chip's own curve. The one exception is the guard's last resort, used only when the start-point attribute is missing. It is sticky until you run `acknowledge`.
- It never controls the CPU fan or the MEM channel.
- It never runs next to `fancontrol`, `fan2go` or another `it87` module. The installer refuses and explains how to migrate.
- It never triggers a shutdown from the NVMe. The NVMe throttles itself; the software raises the fans to full air.
- It never shuts down unless you turn that on.
- It never sends data off the machine. The services have no network access and there is no telemetry.

**What happens when something fails**

| Failure | Behaviour |
|---|---|
| The controller crashes or hangs | The chip keeps the last start point, which is never below the floor. A backup restores the profile values within 5 seconds. The service restarts. |
| The driver is missing (after a kernel update, or Secure Boot not yet enrolled) | The controller stops without writing anything. The fans follow the firmware curve. The guard alerts once per boot. |
| A disk or the NVMe cannot be read for three cycles | The channel goes to full speed. |
| A fan stops while its channel is at or above the floor | The other managed channel goes to full speed, and you get an alert. |

**The thermal guard**

The guard is a separate service. It runs even if the controller is stopped.

| Step | Trigger | Action |
|---|---|---|
| Full air | A disk reaches 54 °C, or the NVMe reaches 70 °C | Fans to 100 %. Released below 50 °C for disks. |
| Alert | Every guard action | Journal entry, mail to root if `sendmail` exists, and an optional hook command. |
| Shutdown (opt-in) | One disk at 58 °C or more on three consecutive readings 30 seconds apart, **and** a second disk at 50 °C or more | Clean shutdown after a one-minute warning. Never within 30 minutes of a previous thermal shutdown. The NVMe never counts here. |

The default is **alert**. The installer asks whether to enable automatic shutdown, with this rule explained, and the default answer is no. In the menu, enabling shutdown requires typing `shutdown`. With only one disk, shutdown is not possible: the guard alerts and the menu says so. The NVMe never triggers a shutdown.

**Secure Boot**

If Secure Boot is on, the module is unsigned. The installer signs it with a key for this machine (MOK) and imports it. You confirm the key once in the MOK Manager screen at the next boot. Until that happens, the fans stay on the firmware curve.

**Other safety rules**

- The installer refuses to run while another fan controller or another `it87` DKMS package is active.
- The driver source is checked against the hashes in the repository before it is built.
- Both services run with restricted privileges and a closed device list. The controller never writes without the driver's start-point attribute present on every managed channel.

## Install (planned, v1.0)

Install from a tagged release, not from a `curl | bash` line:

```sh
apt install git            # only if git is missing
git clone --branch v1.0.0 --depth 1 https://github.com/phyrexia/wtr-max-fans.git
cd wtr-max-fans
sudo ./install.sh
```

The installer:

1. Checks that the board and chip are a supported WTR MAX. It stops with a plain message if not.
2. Installs the packages it needs: `dkms`, the compiler, the kernel headers, `smartmontools`, `whiptail` and `mokutil` if Secure Boot is on.
3. Builds and loads the patched driver with DKMS.
4. Installs and starts the guard in alert mode.
5. Opens the calibration wizard. It shows which fans respond, asks you to confirm each role, runs the noise test, and asks about automatic shutdown (default no).
6. Saves the calibration and starts the controller.

Expected time on a stock Proxmox VE 9 with internet access: about 10 minutes, including the DKMS build.

**Why not `curl | bash`?** The script runs as root and builds a kernel module. A local copy can be read before it runs. If the connection drops, a piped script can run half-way. A tagged clone also pins the exact bytes, and you can check the signature with `git verify-tag`. Release tarballs with `SHA256SUMS` are available for people without git.

**Upgrade.** Clone the new tag and run `sudo ./install.sh` again. It upgrades in place, keeps your settings and calibration, and rebuilds the module only when the driver changed.

## Using the menu

```sh
sudo wtr-max-fans
```

```
wtr-max-fans 1.0.0 — AOOSTAR WTR MAX fan control
  1  Status                fans, disk and NVMe temperatures, profile, protection, driver
  2  Calibrate fans        detect which fan is which and whether it responds (required once)
  3  Profile               quiet / balanced / cool
  4  Night mode            on/off, hours, weekends, holidays
  5  Overheat protection   alert (default) / shutdown
  6  Noise test            hear the profiles again
  7  Report                bundle for an issue
  8  Uninstall
  0  Quit
```

Screenshots: coming with v1.0.

## Command line (planned)

Every menu action is also a command. Use the command line for scripts and remote sessions. With no terminal, the menu refuses and prints the matching command.

| Command | Purpose |
|---|---|
| `wtr-max-fans check` | Read-only checks: board, chip, driver, conflicting software. Writes nothing. |
| `wtr-max-fans calibrate [--yes]` | Guided calibration. `--yes` keeps the defaults and skips the noise test. |
| `wtr-max-fans profile quiet\|balanced\|cool` | Set the profile. |
| `wtr-max-fans night on\|off [--start HH:MM] [--end HH:MM]` | Night mode. |
| `wtr-max-fans protection alert\|shutdown` | Set the guard action. |
| `wtr-max-fans status [--json]` | Status, in text or JSON. |
| `wtr-max-fans acknowledge` | Clear the sticky emergency state after you fix the cause. |
| `wtr-max-fans restore-firmware` | Put back the firmware values recorded at calibration, without uninstalling. |
| `wtr-max-fans report` | Create the bundle for an issue report. |
| `wtr-max-fans uninstall [--yes] [--purge]` | Remove the software. |

### Exit codes

| Code | Meaning |
|---|---|
| 3 | Not a recognised WTR MAX, or the chip is not IT8613E. Nothing was installed. |
| 4 | The driver loaded but the patch is not active. The installer removes what it added. |
| 5 | Another fan controller, or another `it87` DKMS package, is present. The message explains the migration. |
| 6 | A prerequisite is missing. The exact `apt` command is printed. |
| 78 | Controller service: the driver or its attribute is missing. Nothing was written. |

## Metrics (optional)

If the textfile collector directory exists (for example `/var/lib/prometheus/node-exporter`), the services write `wtr_max_fans_*` metrics there: disk and NVMe temperatures, PWM targets and applied values per channel and role, mode, state, and guard state. `contrib/prometheus/` has example alert rules with their tests. They are not installed automatically. `wtr-max-fans status --json` gives the same data without Prometheus.

## FAQ

**Are my fans PWM?**
PWM fans have four pins. The calibration tests each channel: it checks that the fan slows down when the chip asks, and that the tachometer reads a speed. A fan that does not respond is reported as such and left alone. Replace it with a 4-pin PWM fan to get control.

**Why not `fancontrol`?**
`fancontrol` uses the chip's manual mode. If the process is killed, the chip keeps its last duty cycle. It cannot read disk temperatures without an hwmon input, it does not handle standby disks, and it has no night mode. It also cannot use the chip's automatic curve with a start point, which is what this project relies on.

**What happens when I update the kernel?**
DKMS rebuilds the driver for the new kernel when the kernel headers are installed. After a kernel update, run `sudo wtr-max-fans check`. If the running kernel has no module, the fans go back to the firmware curve. The guard then logs an alert and mails root once per boot.

**Which way do the fans blow?**
The rear fans exhaust air out of the chassis. On Noctua fans, the side with the label is the air outlet. See [Noctua's FAQ](https://www.noctua.at/en/support/faqs/how-do-i-determine-my-fans-airflow-direction) for orientation details.

**What about positive pressure?**
This software does not control pressure. This chassis is meant to draw air from the front, across the disks, and out through the rear fans. Keep the fans as installed; you do not need to turn them around.

**TrueNAS or Unraid?**
Not yet. v1 targets Proxmox VE and Debian, including OpenMediaVault. Other bases are out of scope for v1.

## Uninstall (planned)

Use menu item 8, or:

```sh
sudo wtr-max-fans uninstall
```

Uninstall stops the controller, restores the firmware values recorded at calibration, and removes the services, the program, the DKMS module, the module-load entry, the metrics file and the state directory. It keeps `/etc/wtr-max-fans/` (your settings and calibration) unless you pass `--purge`. It leaves the apt packages installed and lists them so you can remove them yourself. No reboot is needed: the chip is back on the firmware's own values immediately.

## Contributing and reporting a board

- **Report a board or a problem.** Run `sudo wtr-max-fans report` (planned) and attach the bundle to an issue. Issue reports must include this bundle. The bundle contains DMI strings, `dkms status`, the hwmon listing, a disk scan, a temperature summary, both configuration files, the last 500 journal lines and the version. It has no serial numbers. The journal lines can include your hostname, so review the file before you post it.
- **Security.** Report vulnerabilities through GitHub's private vulnerability reporting. See `SECURITY.md`.
- **Contributions.** Read `CONTRIBUTING.md`. Tests must pass locally and in CI, and each functional change goes in its own commit.

## Development

```sh
python3 -m pytest -q tests
```

The tests use a fake system root and need no hardware. CI runs shellcheck, pytest on Python 3.11 and 3.13, `promtool` on `contrib/prometheus/`, a compile of the driver against Debian 12, Debian 13 and current Proxmox VE headers, and `reuse lint`.

## License

- `driver/` is **GPL-2.0-only**. It is derived from the Linux `it87` driver.
- Everything else is **MIT**.
- Each file has SPDX headers, and the full texts are in `LICENSES/` (REUSE layout).

The driver and the controller run as separate processes and only share sysfs.

## Credits

- The driver is [`frankcrawford/it87`](https://github.com/frankcrawford/it87), pinned at commit `bd7a566c`, with a patch from this project.
- Ideas and prior art: [fan2go](https://github.com/markusressel/fan2go), [hddfancontrol](https://github.com/desbma/hddfancontrol), [UGREEN-Fan-Control](https://github.com/0n1cOn3/UGREEN-Fan-Control) and lm-sensors `pwmconfig`.

## Trademarks

Not affiliated with or endorsed by AOOSTAR. AOOSTAR and WTR MAX are trademarks of their respective owners.
