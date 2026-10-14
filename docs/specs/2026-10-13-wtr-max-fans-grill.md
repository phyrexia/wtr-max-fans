# Grill — `wtr-max-fans`: disk-aware fan control for the AOOSTAR WTR MAX, for other owners

- **Date**: 2026-10-13
- **Participants**: the maintainer (Owner: vision, hardware, measured values), Designer (this grill)
- **Profile**: `feature` (delivery-lifecycle phase 1)
- **Origin** (Owner, 2026-10-13, translated): "Create a repo for the new machine driver with the improved fan-curve
  profile and the option to turn on the quiet night mode, so that other WTR MAX owners can use it easily. It may need
  a web page or something simple to configure for non-technical people, in a public git repo."

Decisions the Owner already took and this grill does not reopen: repository `phyrexia/wtr-max-fans` (private until
release, public at v1.0.0); v1 targets Proxmox VE and Debian (OpenMediaVault counts as Debian); a terminal menu
(whiptail), no web server; the driver patch stays in this repository and is **not** submitted to `frankcrawford/it87`.

"AOOSTAR" and "WTR MAX" are used descriptively to identify the hardware. This project is not affiliated with,
sponsored by or endorsed by AOOSTAR. Nothing in this document or the repository may suggest otherwise.

## Problem (one line, no solution)

An owner of an AOOSTAR WTR MAX running Proxmox VE or Debian has no supported way to make the chassis fans follow the
temperature of the hard disks or to quiet them at night: the firmware fan curve reacts to two board thermistors that do
not track the disk cage, the mainline kernel does not drive this Super-I/O, and with PWM fans the firmware curve can
stop the disk-cage fans entirely while the disks pass 50 °C.

## What is already known (consulted before asking)

- **The maintainer's private deployment** (same hardware, 2026-10-13): an out-of-tree `it87` driver (frankcrawford,
  pinned commit `bd7a566c`) built with DKMS plus a three-line visibility patch; a controller that keeps the chip in
  automatic mode and writes only the per-channel Start PWM from the hottest disk's SMART temperature, with a day and a
  night curve; a separate thermal guard (full air at 54 °C, corroborated shutdown at 58 °C); 81 unit tests; a spec
  audited in four rounds and an implementation audited in two. Everything below that is "known" comes from there.
- **Measured on that unit**: stock fans were not PWM (they ran at full speed regardless of the curve, which is why the
  firmware curve had never mattered); after swapping to Noctua PWM fans the firmware curve stopped the rear fans and the
  disks reached 56 °C; the MEM header carries a 2-wire fan (no PWM, no tachometer); SYS is the bottom fan and SATA the
  rear fans; board thermistors moved 1–2 °C while disks moved 3–8 °C; rear fans at 60 % PWM (153/255) are comfortable
  and 75 % adds noise without lowering disk temperature; bottom fan at 30 % (77/255) at night is not noticed; with
  those values the six 4 TB disks sit at 33–42 °C.
  *Note (2026-10-14):* "not noticed" was measured against the daytime 39 % (100/255), not as a night level. The first
  night with the controller (rear 60 % and bottom 30 % both day and night) brought no audible change, and the
  maintainer asked for a real night mode. The private spec rev. 5.1 and ADR 001 §3.7 now give each role a night floor
  of its own (rear 89 = 708 rpm, bottom 64 = 511 rpm, tachometer-verified) and a night level at 46 °C strictly below
  the day level (rear 128, bottom 77); 51 on the bottom fan (274 rpm) was measured stable but rejected for lack of
  margin over the datasheet minimum.
- **Chip facts** (read on that unit): ITE IT8613E at Super-I/O port `0x2E`, environment controller at base `0xA30`;
  mainline 7.0 does not know the chip and with `force_id=0x8622` misreports `pwm4`/`pwm5`; the per-channel register
  base+3 is both the manual duty and the Start PWM of the automatic curve; if a controller dies in manual mode the chip
  stays at the last duty forever; the driver's `is_visible` bug hides `pwm4..6_auto_start` (hence the patch).
- **Project memory and library**: the project memory returned the driver README and the deployment commit (base of
  everything above). The curated library (`sb query … --research biblioteca`, three queries on open-source installers,
  licensing and fail-safe defaults) returned nothing applicable; the state of the art below comes from public sources.
- **State of the art** (public projects, fetched 2026-10-13): fan2go (Go, AGPL-3.0; one-off calibration that maps PWM
  to RPM and finds the start PWM; manual PWM mode; `onExit: restore`; disk temperatures via `drivetemp`), hddfancontrol
  (Rust, GPL-3.0; hottest disk drives a linear ramp; `pwm-test` subcommand; manual mode), UGREEN-Fan-Control (bash +
  `it87` DKMS for UGREEN NAS; installed by `git clone` + `sudo ./scripts/install.sh`; PWM auto-detection at start;
  silent/quiet/turbo presets), lm-sensors `pwmconfig` (5 s settle, a fan is "controlled" when its RPM drops below 5/6 of
  the original), the Proxmox VE Secure Boot wiki (DKMS signs with `/var/lib/dkms/mok.pub`; enrol with
  `mokutil --import`, finish in MOK Manager at reboot). None of them combines disk-driven curves, a night mode, a
  hybrid mode that survives the daemon dying, a shutdown guard and a menu for non-technical users on this chip.

## Assumptions challenged

| # | Assumption | Question | Answer (source) | Status |
|---|---|---|---|---|
| A1 | Every WTR MAX carries an IT8613E at `0x2E`/`0xA30` with the same channel map (CPU=`pwm2`, SYS=`pwm3`, MEM=`pwm4`, SATA=`pwm5`). | How do we know on a unit we have never seen? | Known for one unit (DMI `TianBei` / `WTR MAX`, chip ID `0x8613` rev `0x0C`). Board revisions, BIOS versions and header wiring of other units are unknown. The product must **verify** (DMI + Super-I/O ID + hwmon `name`) and **measure** (tachometer test per channel) instead of assuming. | pending → handled by design: refuse on identity mismatch, calibrate the map per unit |
| A2 | The fans respond to PWM. | What if the owner still has the stock fans? | On the maintainer's unit the stock fans were **not** PWM: they ran at full speed whatever the register said. A DC fan on these headers cannot be slowed by this chip (no DC-mode evidence for the IT8613E was found). | **refuted** for stock fans → the product must detect PWM response per channel and say plainly "this fan does not respond; nothing to control" |
| A3 | Users will accept an automatic power-off when disks overheat. | Would a non-technical owner expect their NAS to shut down on its own? Would a corrupt sensor shut it down? | The maintainer's guard shuts down only with two disks corroborating, after an audit found the single-sensor rule dangerous. Still, a surprise power-off of a host running VMs and containers is the most destructive thing this software can do. | **refuted as a default** → default = full air + alert; shutdown is opt-in from the menu with the corroboration rule and a plain-language warning |
| A4 | The owner has Prometheus, a node exporter and a holiday calendar. | What of the private deployment is environment, not product? | Prometheus labels (`instance=…`), a city-specific holiday list, the hostname check, the CPU-turbo tmpfile, the alert rules bound to a home monitoring stack. | **refuted** → metrics only when a textfile-collector directory exists; schedule = local time window, weekend option, optional holiday file (none shipped); no hostnames, no addresses |
| A5 | Non-technical owners can run a terminal command. | Is "git clone + sudo ./install.sh" within reach? | Proxmox VE ships a web shell (node → Shell); OMV/Debian owners already use SSH to install anything. UGREEN-Fan-Control and the Proxmox helper scripts have the same entry point. A web UI would add a network listener on the NAS and an authentication layer for a one-time setup. | confirmed (Owner decided: menu, no web) |
| A6 | Building a kernel module is acceptable for this audience. | What breaks? | Needs `dkms`, a compiler and the running kernel's headers (`proxmox-default-headers` on PVE, `linux-headers-amd64` on Debian); on every kernel update DKMS must rebuild before reboot; Secure Boot requires MOK enrolment with a reboot and a keyboard. Without the module nothing works. | confirmed with conditions → the installer installs the prerequisites, checks `dkms status` after kernel updates, guides MOK enrolment, and the fans always fall back to the firmware curve |
| A7 | The improved curve values travel as-is. | Are 153/77 universal? | They are the maintainer's measured comfort points for Noctua fans in one room. Other fans have other RPM/PWM relations and other noise. | **partly refuted** → ship three profiles (quiet / balanced / cool) with the same temperature points and safety ceiling; a guided noise test picks the profile; the disk-temperature thresholds (46/50/52/54/58 °C) are universal because they come from the disks' specification sheets (60 °C hard limit), not from the room |
| A8 | "Disks" means the rotational SATA disks in the cage. | NVMe? USB? SSD? | NVMe lives elsewhere and has its own 70 °C class limit; USB disks come and go; SATA SSDs run cool. The guard is about spinning disks in the cage with no fan of their own. | confirmed → v1 manages rotational ATA/SCSI disks on internal transports only; NVMe-driven curve for the bottom fan is a v1.x item |
| A9 | Keeping the patch private is harmless. | What does not contributing upstream cost? | We own the pin: every upstream change (kernel compatibility fixes) must be re-pinned and the patch re-applied by us; a vendored copy of the driver source protects installs from upstream history changes. | confirmed (Owner decision) → vendor the pinned source plus the patch; a CI job compiles it against Debian 12, Debian 13 and current PVE headers |
| A10 | One controller per chip. | What if `fancontrol`, fan2go or the maintainer's private units are already installed? | Two writers on the same registers oscillate. | confirmed → the installer refuses to install while any other fan controller unit is active and offers the migration path |
| A11 | Nobody but the maintainer will install it. | Is this worth a product instead of a dotfile? | Unknown; the WTR MAX is a 2025 product and no public Linux fan-control project for it exists (search, 2026-10-13). The no-build criterion below bounds the investment. | pending → measured after release |

## Hard constraints (with numbers)

- **Who uses it, how often**: owners of a WTR MAX on PVE 8/9 or Debian 12/13 (OMV 7), one install per machine, then
  one menu visit per profile change. Planning figure: ≤ 100 installs in the first year, one maintainer, support budget
  ≤ 2 h/week. Issue reports must come with the output of `wtr-max-fans report` (no interactive debugging).
- **Baseline without it**: the firmware curve, which on the maintainer's unit let disks reach 56 °C with PWM fans;
  the only alternative is raw `/dev/port` writes.
- **Safety invariants** (non-negotiable, testable):
  1. A managed channel is never written below 20 % PWM (51/255); profile floors are ≥ that.
  2. Managed channels stay in the chip's automatic mode (`pwmN_enable=2`); the controller writes only
     `pwmN_auto_start` and, once, the flattened curve points. Never `pwmN`, never `pwmN_enable=0/1` from the controller.
  3. If the controller dies (SIGKILL, crash, watchdog), the chip keeps the last Start PWM ≥ floor; `ExecStopPost`
     restores the profile's backup values within 5 s.
  4. Without the patched driver attribute on **every** managed channel, the controller writes nothing at all.
  5. The guard never dies because an action failed; full air at ≥ 54 °C within 30 s; shutdown (only if enabled)
     needs one disk ≥ 58 °C for three consecutive 30 s readings **and** a second disk ≥ 50 °C, and never inside the
     30 min caution window after a thermal shutdown.
  6. The CPU channel (`pwm2`, firmware curve on the CPU sensor) and any channel without a verified PWM response are
     never managed.
  7. A channel reported as "stopped" (0 RPM with PWM ≥ floor for 3 cycles) puts the other managed channel at 100 %.
- **Identity gate**: install refuses (exit code 3, plain-language reason) unless DMI product matches the known list
  **and** the Super-I/O answers ID `0x8613` at `0x2E` **and**, once the driver is loaded, the hwmon device is named
  `it8613`. No override flag in v1.
- **Calibration bounds**: ≤ 30 s of probing per channel; probe values between 31 % (80) and 100 %; every register
  touched is snapshotted before and restored after; a fan counts as PWM-controlled only if its RPM falls below 5/6 of
  the full-speed reading (the `pwmconfig` criterion) and the tachometer reads > 0 at full speed.
- **Dependencies**: Python ≥ 3.11 standard library only (no pip), `smartmontools`, `whiptail`, `dkms`, `gcc`/`make`,
  kernel headers. Everything apt-installable from the distribution's own repositories. Install on a stock PVE 9 with
  internet in ≤ 10 min including the DKMS build (≤ 2 min on this CPU).
- **Install surface**: `/usr/local/sbin`, `/usr/local/lib/wtr-max-fans`, `/etc/wtr-max-fans/`, `/etc/systemd/system/`,
  `/usr/src/wtr-max-fans-it87-<ver>/`, `/etc/modules-load.d/`, `/var/lib/wtr-max-fans/`, and the Prometheus textfile
  directory only if it already exists. Uninstall removes all of it, restores the firmware curve values it changed
  (Start temperature, Start PWM) and leaves `/etc/wtr-max-fans/` only on request.
- **Data touched and owner**: disk SMART attributes (read only, standby-aware: a sleeping disk is never woken),
  Super-I/O registers (owned by the firmware; we restore what we change), systemd units. No network access from the
  services (`PrivateNetwork=yes`); no telemetry; nothing leaves the machine.
- **What it replaces or duplicates**: replaces the maintainer's private deployment (migration defined in the ADR);
  duplicates the idea of hddfancontrol/fan2go but not their manual-mode mechanism.
- **Irreversible**: nothing by design. The only non-reversible action the software can take is the opt-in shutdown,
  which is a clean `shutdown -h +1` with a cancel window. Publishing the repo is irreversible in the social sense
  (forks), hence the trademark notice and licences from the first public commit.
- **Licensing**: `driver/` is GPL-2.0-only (derivative of the Linux `it87` driver); everything else MIT. Rationale: the
  driver has no choice; MIT lets TrueNAS/Unraid plugin authors embed the controller and the calibration logic without
  licence friction, which is the most likely route to the systems v1 leaves out; the two parts are separate processes
  that only share sysfs, so there is no derivative relation. SPDX headers per file and a `LICENSES/` directory (REUSE).
- **Language**: code, config keys, metrics, menu and `README.md` in English; `README.es.md` as a maintained mirror.

## Out of scope (explicit)

- TrueNAS SCALE, Unraid, Ubuntu, any non-Debian base; non-x86.
- A web interface, an API, a Home Assistant integration.
- Controlling the CPU fan (`pwm2`) or the MEM channel; NVMe-driven curves (v1.x).
- Shipping Prometheus alert rules as part of the install (an example rule file lives in `contrib/`).
- `.deb` packaging, an apt repository, upstream submission of the patch.
- Any BIOS/firmware change; the installer only documents the recommended firmware Start PWM fallback.
- PID control, per-disk curves, RPM-targeted control.
- CPU power/turbo tuning (it was part of the maintainer's deployment and is unrelated to fans).

## No-build criterion

Evidence that would cancel or stop the idea:

- The mainline kernel (or the PVE kernel) gains native IT8613E support with correct `pwm4`/`pwm5` and visible
  `auto_start` attributes: the driver part is dropped and the controller runs on the in-tree driver.
- The identity gate or the calibration fails on a second WTR MAX for reasons the design cannot absorb (different chip,
  different base, channels not measurable by tachometer): the product stays a maintainer's tool and the repo says so.
- No second owner reports a successful install within 90 days of the public release: stop investing in UX (menu,
  screenshots, translations) and keep only correctness fixes.
- A reviewer shows that the hybrid mode (automatic + Start PWM) cannot be made safe on some firmware revision (for
  example, a BIOS that re-arms the Off temperature at runtime): then the design must be redone, not patched.

## Open questions for the Owner (max 4, each with a recommendation)

1. **Shutdown default.** Ship with the guard in `alert` mode (full air + journal + mail to root if `sendmail` exists +
   optional hook command), with `shutdown` as an explicit opt-in from the menu that shows the corroboration rule?
   **Recommendation: yes.** A surprise power-off is the one thing this software can do that an owner cannot undo.
2. **Boards with the same chip that are not a WTR MAX** (the WTR PRO shares the IT8613E at `0xA30`). Allow an expert
   override of the DMI check in v1? **Recommendation: no.** v1 refuses; `wtr-max-fans report` collects DMI and chip
   data so a WTR PRO owner can open an issue, and v1.x adds the board to the list after one confirmed calibration.
3. **Migration of the maintainer's unit.** Replace the private deployment with the public product at v1.0.0-rc (the
   product's hardware test is the migration itself, with a two-minute rollback to the private installer), and switch
   the home alert rules to the product's metric names? **Recommendation: yes**, with a guard-overlap sequence so that
   there is never a minute without a thermal guard and never a second with two controllers.
4. **Alert delivery for non-technical owners.** Journal + `mail` to root (which Proxmox forwards to the admin e-mail)
   + an optional command hook, and nothing else in v1? **Recommendation: yes.** Proxmox's notification API and
   desktop notifications would each add a platform-specific surface for a message that mail already carries.

## Next step

Write ADR 001 (`docs/adrs/001-wtr-max-fans-architecture.md`) from this grill, then run `/grill-me adr` on it before
calling it approved. Exit of this phase: the Owner confirms the problem line and the hard constraints.
