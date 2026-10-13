# ADR 001 — `wtr-max-fans`: architecture of the public fan-control package for the AOOSTAR WTR MAX

- **Status**: proposed
- **Date**: 2026-10-13
- **Profile**: `feature` (delivery-lifecycle phase 2)
- **Grill**: `docs/specs/2026-10-13-wtr-max-fans-grill.md`
- **Deciders**: the maintainer (Owner), Designer
- **Audit receipt**: pending (phase 3; `docs/analysis/2026-10-13-wtr-max-fans-redteam-verdict.md` once issued)

"AOOSTAR" and "WTR MAX" identify the hardware descriptively. This project is not affiliated with, sponsored by or
endorsed by AOOSTAR.

---

## 1. Context

The grill fixed the problem: a WTR MAX owner on Proxmox VE or Debian cannot make the chassis fans follow the hard
disks or quiet them at night, because the firmware curve follows board thermistors that do not track the disk cage,
the mainline kernel does not drive the ITE IT8613E Super-I/O, and with PWM fans the firmware curve can stop the
disk-cage fans while the disks pass 50 °C.

A working, audited answer exists on one unit: the maintainer's private deployment (2026-10-13) — the out-of-tree
`frankcrawford/it87` driver pinned at commit `bd7a566c8a60fe0308d67e26761e16260368191a` with a three-line patch
(`0001-it87-expose-pwm4-6-auto-start.patch`, sha256 `299828452dc427e4df6cc07d9dc771b5dd585916b08acfe9c9903673c4ee0baf`),
a controller that keeps the chip in automatic mode and writes only the Start PWM from the hottest disk's SMART
temperature, a separate thermal guard, 81 unit tests, and systemd hardening. This ADR decides how that becomes a
product other owners can install from a public repository without reading a register map, what generalises, what is
removed, and how it is tested without breaking the maintainer's unit.

Decisions already taken by the Owner and taken as given: repository `phyrexia/wtr-max-fans`; v1 for Proxmox VE 8/9
and Debian 12/13 (OpenMediaVault 7 is Debian 12); whiptail menu, no web server; the patch stays here, no upstream
submission.

## 2. Investigation: what was consulted and what it contributed

| Source | What it contributed |
|---|---|
| Project memory of the maintainer's repository (`engine.py query`, two queries) | The driver README (chip identity, register map, DKMS procedure, supply-chain hashes, reversal) and the deployment commit. Base of §3.2–§3.6. |
| The private spec (revision 4, four audit rounds) and its implementation receipts | Hybrid mode, the control law and its numbers, the guard's five steps, the common write precondition, the 17 unit-test cases, the host tests H0–H11. Base of §3.5–§3.6 and §3.11. |
| Curated library (`sb query … --research biblioteca`: installers for non-technical users, open-source licensing, fail-safe defaults) | **Nothing applicable** (results were unrelated to this domain). |
| [fan2go](https://github.com/markusressel/fan2go) | One-off calibration persisted to a database (start PWM, max PWM, PWM→RPM map); `onExit: restore`; disk sensors via `/dev/disk/by-id`. We adopt "calibrate once, persist" and "restore on exit"; we reject its manual-mode mechanism and its AGPL-3.0 licence (would bind the whole product). |
| [hddfancontrol](https://github.com/desbma/hddfancontrol) | Hottest disk drives a linear ramp; `pwm-test` as a separate subcommand; standby-aware probing. Adopted in the private spec; kept. |
| [UGREEN-Fan-Control](https://github.com/0n1cOn3/UGREEN-Fan-Control) | The closest precedent in audience and stack: bash daemon + `it87` DKMS, installed by `git clone` and `sudo ./scripts/install.sh`, PWM auto-detection at start, silent/quiet/turbo presets. Confirms the install entry point (§3.10) and the preset idea (§3.7). |
| lm-sensors [`pwmconfig`](https://raw.githubusercontent.com/lm-sensors/lm-sensors/master/prog/pwm/pwmconfig) | Calibration timings and threshold: 5 s settle, a fan is linked to an output when its RPM falls below 5/6 of the original, a reading of 0 at PWM 255 means "not controllable by this output". Adopted in §3.4. |
| [Proxmox VE Secure Boot wiki](https://pve.proxmox.com/wiki/Secure_Boot_Setup) | PVE ≥ 8.1 supports Secure Boot; DKMS generates `/var/lib/dkms/mok.pub` on first build; enrol with `mokutil --import`, reboot, finish in MOK Manager; key paths configurable in `/etc/dkms/framework.conf`. Base of §3.2 Secure Boot. |
| [Kernel `it87` documentation](https://docs.kernel.org/next/hwmon/it87.html) | `ignore_resource_conflict` and `force_id` are testing/escape hatches with documented risks; neither is used. |
| [`drivetemp` documentation](https://www.kernel.org/doc/html/latest/hwmon/drivetemp.html) | Alternative temperature source via hwmon; its standby behaviour was not verified in this research, so `smartctl -n standby` stays (§4). |
| [FHS 3.0 §4.9](https://refspecs.linuxfoundation.org/FHS_3.0/fhs/ch04s09.html) | `/usr/local` is for locally installed software that must survive distribution updates. Base of the install surface (§3.9). |
| [REUSE specification 3.3](https://reuse.software/spec-3.3/) | Per-file SPDX headers and a `LICENSES/` directory; no directory-level licence rule, so the dual licensing is expressed per file (§3.15). |
| curl-pipe-bash analyses ([Kicksecure](https://www.kicksecure.com/wiki/Dev/curl_bash_pipe), [arp242](https://www.arp242.net/curl-to-sh)) | Partial execution when the connection drops; pipeline exit masking; server-side detection of piped requests. Base of §4 (install method). |
| Trademark wording in community projects ([synology-drive-tools](https://codeberg.org/tomkalei/synology-drive-tools), a UGREEN Home Assistant wiki) | "Not affiliated with … Inc." plus a list of the marks used. Adopted for the README and this ADR. |
| Not found (searched 2026-10-13) | Any public Linux fan-control project for the WTR MAX or WTR PRO; the WTR PRO/OmniOS write-up (the private README cites it for "same chip, same base `0xA30`"); public reports on stock fan type across units; a public IT8613E datasheet (fan/PWM counts, DC mode); build reports of `frankcrawford/it87` on kernels 6.14–7.0 other than the maintainer's own build on `7.0.14-23-pve`. These gaps are handled by measuring at install (§3.3–§3.4) and compiling in CI (§3.14) instead of assuming. |

## 3. Decision

### 3.0 Summary

| # | Decision | Why (short) |
|---|---|---|
| D1 | **One repository, four parts**: `driver/` (vendored pinned source + patch + `dkms.conf`), `wtrfans/` (Python 3.11+ stdlib package: detect, calibrate, controller, guard, menu), `install.sh` (thin bash entry), `contrib/` (optional Prometheus rules). | Each part has a different licence, test and failure domain. |
| D2 | **Driver vendored at the pin**, patch applied by DKMS (`PATCH[0]`), DKMS package name `wtr-max-fans-it87`, `AUTOINSTALL="yes"`. | No network or git needed at install; the exact bytes are hashed in the repo and compiled in CI; a distinct DKMS name cannot collide with a distro `it87` package. |
| D3 | **Identity gate, no override**: DMI product in the known list **and** Super-I/O ID `0x8613` **and** hwmon `name == it8613`; otherwise exit 3 with a plain reason. | The grill's hard constraint; the WTR PRO question is deferred to v1.x. |
| D4 | **Guided calibration once**, persisted to `/etc/wtr-max-fans/hardware.ini`: per channel, PWM response (yes / no / no tachometer), role (`disk-cage`, `base`, `cpu`, `unmanaged`), firmware register snapshot. The controller does not start without it. | Fans and wiring differ per unit; the file is the contract between what was measured and what is controlled, and the source for a faithful uninstall. |
| D5 | **Hybrid mode kept verbatim**: managed channels in automatic (`pwmN_enable=2`), flattened curve (Off 0 °C, Start 0 °C, Full 80 °C, slope 0), the controller writes only `pwmN_auto_start`; common precondition (every managed channel has `auto_start`, no guard step active) before any write; `ExecStopPost` backup. | Audited four times; it is what makes "the daemon died" harmless. |
| D6 | **Control law kept**, parametrised by role and profile: hottest disk, piecewise-linear, day and night curves converging at 50 °C, 255 at 52 °C, step 20/4 per cycle, deadband 6, floor ≥ 51 (20 %). | Same numbers, same tests; only the sources of the per-channel constants change. |
| D7 | **Three profiles** (`quiet`, `balanced`, `cool`) with identical temperature points and safety ceiling; `balanced` = the maintainer's measured values; a **noise test** in the wizard picks one. | Comfort depends on the fans and the room; safety does not. |
| D8 | **Night mode generic**: local time, `night_start`/`night_end`, optional later end on weekends, optional holiday file (none shipped); unreadable holiday file ⇒ treated as a normal day. | Nothing of the maintainer's calendar travels; failing towards the day curve means more air, never less. |
| D9 | **Guard default `alert`** (full air at 54 °C + journal + mail to root when `sendmail` exists + optional hook); **`shutdown` opt-in** from the menu with the corroboration rule shown; with a single disk, shutdown degrades to alert and the menu says so. | A surprise power-off is the one irreversible action; the corroboration rule needs two disks. |
| D10 | **Menu = `whiptail` driven from Python**, every menu action also a CLI subcommand; no TTY ⇒ CLI only. | Zero extra packages on Debian (whiptail is `Priority: important`); testable without a terminal. |
| D11 | **Install = `git clone` (tagged release) + `sudo ./install.sh`**; `install.sh` installs apt prerequisites and hands over to the Python installer; `--check` is read-only; the same command upgrades. | Local copy, reviewable, resumable, auditable version; no `curl \| bash`. |
| D12 | **Uninstall restores the firmware values recorded at calibration** (Start temperature, Start PWM per channel), removes units, files, DKMS module and `modules-load.d` entry; keeps `/etc/wtr-max-fans/` unless `--purge`. | "Leaves the firmware in charge" has to mean the firmware's own numbers, not constants. |
| D13 | **Refuse to coexist**: another fan controller unit active (`fancontrol`, `fan2go`, the maintainer's legacy units) or another DKMS package building `it87` ⇒ stop with the migration text. | Two writers on one register oscillate; the migration is explicit. |
| D14 | **Tests**: pytest with a fake system root (sysfs, `/etc`, `/run`, `/var`, fake `smartctl`/`systemctl`/`shutdown`/`sendmail` on `PATH`), run remotely on `qf-test`; GitHub Actions CI: shellcheck, pytest on Python 3.11 and 3.13, `promtool` for `contrib/`, driver compile against Debian 12, Debian 13 and current PVE headers in containers, `install.sh --check` must exit 3 in a container. | Nothing in CI needs the chip; the chip-dependent part is compiled, not run. |
| D15 | **Hardware test = the migration of the maintainer's unit** at `v1.0.0-rc.1`, with a guard-overlap sequence and a two-minute rollback to the private installer. | The only WTR MAX we have; the private deployment is the rollback. |
| D16 | **SemVer, signed tags, GitHub Releases with `SHA256SUMS`, `CHANGELOG.md`**; `README.md` + `README.es.md` with menu screenshots, a Safety section and an Uninstall section; `driver/` GPL-2.0-only, the rest MIT, REUSE layout. | Public repo hygiene from the first public commit. |

### 3.1 Repository layout (to be created)

```
wtr-max-fans/
├── install.sh                    # bash, ≤ 150 lines: root check, apt prerequisites, exec python3 -I bin/wtr-max-fans install
├── bin/wtr-max-fans              # Python entry point (installed to /usr/local/sbin/wtr-max-fans)
├── wtrfans/                      # Python package (installed to /usr/local/lib/wtr-max-fans/wtrfans)
│   ├── sysroot.py                #   single WTR_FANS_SYSROOT prefix for /sys, /etc, /run, /var, /dev (tests inject it)
│   ├── detect.py                 #   DMI, Super-I/O ID probe (driver unloaded only), hwmon discovery, other-controller detection
│   ├── hwmon.py                  #   it87 sysfs reads/writes, register snapshot/restore
│   ├── smart.py                  #   disk discovery, smartctl -j -n standby -A, plausibility
│   ├── calibrate.py              #   PWM-response test, role assignment, noise test, hardware.ini writer
│   ├── curve.py                  #   profiles, day/night curves, step/deadband law
│   ├── schedule.py               #   night window, weekend, holidays file
│   ├── controller.py             #   service loop (Type=notify), prepare, backup
│   ├── guard.py                  #   thermal guard (steps 0–5), alert/shutdown actions, acknowledge
│   ├── alerts.py                 #   journal, sendmail to root, hook command
│   ├── metrics.py                #   textfile writer (atomic tmp+rename) when a textfile dir exists
│   ├── config.py                 #   config.ini / hardware.ini (configparser), validation, defaults
│   ├── installer.py              #   install / upgrade / uninstall / check / dkms / secure boot
│   ├── ui.py                     #   whiptail wrapper + plain fallback
│   └── menu.py                   #   menu tree
├── systemd/
│   ├── wtr-max-fans-controller.service
│   └── wtr-max-fans-guard.service
├── driver/
│   ├── LICENSE                   # GPL-2.0-only
│   ├── README.md                 # pin, hashes, patch rationale, how to bump the pin
│   ├── SHA256SUMS                # of every file under upstream/ and patches/
│   ├── dkms.conf
│   ├── modules-load.d/it87.conf
│   ├── patches/0001-it87-expose-pwm4-6-auto-start.patch
│   └── upstream/                 # it87.c, compat.h, Makefile, LICENSE, VERSION — pristine bd7a566c
├── config/
│   ├── config.ini.example
│   └── holidays.txt.example
├── contrib/prometheus/           # example rules + promtool unit tests; not installed
├── tests/                        # pytest
├── docs/
│   ├── specs/2026-10-13-wtr-max-fans-grill.md
│   ├── adrs/001-wtr-max-fans-architecture.md
│   ├── analysis/                 # audit receipts
│   └── img/                      # menu screenshots
├── .github/workflows/ci.yml
├── LICENSES/                     # MIT.txt, GPL-2.0-only.txt (REUSE)
├── LICENSE                       # MIT (everything outside driver/)
├── README.md, README.es.md, CHANGELOG.md, SECURITY.md, CONTRIBUTING.md, VERSION
└── tasks/todo.md, tasks/lessons.md
```

### 3.2 Driver

- **Pin**: `frankcrawford/it87` commit `bd7a566c8a60fe0308d67e26761e16260368191a` (upstream `it87.c` sha256
  `b11c5dd669b6a2a38ad39952784bfeff320e539843d22639c97cf6a11d25d703`), vendored pristine under `driver/upstream/` with
  its GPL-2.0 licence file; `driver/SHA256SUMS` records every file and the installer verifies it before `dkms add`.
- **Patch**: `driver/patches/0001-it87-expose-pwm4-6-auto-start.patch` (sha256 `29982845…0baf`, three lines in
  `it87_auto_pwm_is_visible()`), applied by DKMS at build time through `PATCH[0]` so that the vendored tree stays
  auditable as "upstream + diff".
- **`dkms.conf`**:

  ```
  PACKAGE_NAME="wtr-max-fans-it87"
  PACKAGE_VERSION="bd7a566c-1"          # <upstream short sha>-<patch series revision>
  BUILT_MODULE_NAME[0]="it87"
  DEST_MODULE_LOCATION[0]="/updates/dkms"
  PATCH[0]="0001-it87-expose-pwm4-6-auto-start.patch"
  AUTOINSTALL="yes"
  ```

  The upstream `Makefile` is kbuild-compatible, so DKMS's default `MAKE` applies. Source goes to
  `/usr/src/wtr-max-fans-it87-bd7a566c-1/`.
- **Prerequisites** installed by `install.sh` with `apt-get install --no-install-recommends`: `dkms`, `gcc`, `make`,
  and the headers metapackage — `proxmox-default-headers` when `/usr/bin/pveversion` exists, else
  `linux-headers-amd64`; plus `smartmontools`, `whiptail`, `python3`, `mokutil` (only when Secure Boot is on).
- **Load**: `/etc/modules-load.d/it87.conf` with the bare module name, no options (`force_id` and
  `ignore_resource_conflict` are never used: the private deployment proved there is no ACPI conflict on this board).
- **Secure Boot**: `mokutil --sb-state`. If enabled, after the first DKMS build the installer runs
  `mokutil --import /var/lib/dkms/mok.pub` (the user types a one-time password in the terminal), ends with a "reboot
  required" screen that explains the MOK Manager prompt, and the status screen shows "driver: not loaded — Secure Boot
  key pending enrolment" until `modprobe it87` succeeds. Nothing else is installed as active until the module loads.
- **Kernel updates**: `AUTOINSTALL="yes"` rebuilds in `/etc/kernel/postinst.d/dkms`. The status screen compares
  `dkms status wtr-max-fans-it87` against `/lib/modules/*` and warns "the kernel `<ver>` has no module built; run
  `sudo wtr-max-fans check`". At boot, the guard (always running) logs and mails once if the hwmon device is missing:
  "fans are following the firmware curve".
- **Verification after load**: `/sys/devices/platform/it87.*/hwmon/hwmon*/name == it8613` and `pwm5_auto_start`
  present (that attribute only exists with the patch).
- **Bumping the pin** is a documented maintainer procedure (`driver/README.md`): replace `upstream/`, re-apply the
  patch, regenerate `SHA256SUMS`, bump `PACKAGE_VERSION`, let CI compile, release as a minor version.

### 3.3 Identity gate and detection (`wtr-max-fans check`, also the first step of `install`)

| Gate | Check | Source | On failure |
|---|---|---|---|
| G1 board | `/sys/class/dmi/id/sys_vendor` + `product_name` in the known list (`TianBei` / `WTR MAX`, as read on the maintainer's unit; the list is a table in `detect.py`) | DMI | exit 3: "This is not a recognised AOOSTAR WTR MAX (found: `<vendor> <product>`). Nothing was installed. If you believe it is one, open an issue with the output of `wtr-max-fans report`." |
| G2 chip | Super-I/O ID at `0x2E` equals `0x8613` (enter key `87 01 55 55`, read `0x20/0x21`, exit `0x02 ← 0x02`), via `/dev/port`, **only while `it87` is not loaded** (the ACPI mutex protects `0x2E` from the firmware side; we never probe while the driver owns it) | the private `volcar-hwm.py` procedure | exit 3: "Super-I/O is not an IT8613E (found `0x<id>`)." |
| G3 driver | after `modprobe`, hwmon `name == it8613` and `pwm5_auto_start` exists | sysfs | exit 4: "driver loaded but the patch is not effective" (DKMS tree tampered or stale); the installer removes what it added |
| G4 exclusivity | no active unit among `fancontrol.service`, `fan2go.service`, `wtr-ventiladores.service`, `wtr-guardia-discos.service`; `dkms status` has no other package named `it87` | systemd, dkms | exit 5 with the migration text (§3.15) |
| G5 prerequisites | Python ≥ 3.11, apt available, running kernel has headers or they are installable | — | exit 6 with the exact apt command |

`check` never writes. Exit codes are stable (documented) so that the README's troubleshooting table and the tests can
refer to them.

### 3.4 Guided calibration (`wtr-max-fans calibrate`, required once; re-runnable)

Precondition: driver loaded, controller stopped (the wizard stops it and `ExecStopPost` leaves the backup), `flock` on
`/run/wtr-max-fans/lock`, the guard still running (its step 1 would override the wizard; the wizard aborts if a guard
step becomes active).

1. **Snapshot**: for each channel `pwm2..pwm5`, read and store `pwmN_enable`, `pwmN_auto_start`, `pwmN_auto_slope`,
   `pwmN_auto_point{1,2,3}_temp`, `pwmN_auto_channels_temp`. This is the **firmware snapshot** that uninstall restores
   (D12). Stored in `hardware.ini` under `[firmware.pwmN]`.
2. **PWM-response test per channel with a tachometer** (`fanN_input` exists), `pwm2` included but only to
   classify it (it is never managed). Sequence, with the `pwmconfig` numbers: set `enable=2`, flatten the curve,
   `auto_start=255`, wait 5 s, read `rpm_full`; `auto_start=80` (31 %), wait 5 s, read `rpm_low`; restore the snapshot.
   Classification:
   - `rpm_full == 0` → **no tachometer / no fan**: "no fan detected on `<channel>` (or a 2-wire fan): not managed".
   - `rpm_low < 5/6 · rpm_full` → **PWM-controlled**.
   - otherwise → **does not respond to PWM** (DC fan or fixed-speed): "the fan on `<channel>` runs at a fixed speed;
     this software cannot slow it down. Replace it with a 4-pin PWM fan to control it."
   Bounded: ≤ 2 writes of `auto_start` and ≤ 12 s per channel; never below 80 during the probe; the snapshot is
   restored even on `KeyboardInterrupt` (`finally`).
3. **Role assignment**: default from the known WTR MAX map (`pwm5` → `disk-cage`, `pwm3` → `base`, `pwm2` → `cpu`,
   `pwm4` → `unmanaged`), then confirmed by the user per PWM-controlled channel: the wizard runs that channel alone at
   255 for 5 s and asks "Which fan just sped up? rear (disk cage) / bottom (under the case) / other / not sure". "Not
   sure" keeps the default and flags `role_source = assumed`; "other" → `unmanaged`.
4. **Noise test** (only on PWM-controlled, managed channels): plays the night levels of `quiet`, then `balanced`, then
   `cool` for 20 s each, asking after each "Acceptable at night where the NAS lives? yes / no". The proposed profile is
   the **loudest acceptable** one (more air at equal comfort); if none is acceptable, `quiet` with a note that disks
   will run warmer within the safe band. The user can override.
5. **Write `hardware.ini`** atomically with `calibrated = <ISO timestamp>` and start the controller.

Outcome cases: 0 managed channels → guard only, menu says "fan control is not available with the fans detected";
1 managed channel → the stopped-fan rule has no partner, so it becomes alert-only; 2 → full behaviour.

### 3.5 Controller (`wtr-max-fans-controller.service`)

What stays from the private implementation (unchanged semantics, same tests): hybrid mode and the common write
precondition; the control law (`curve(T, role, night)` with floor, silence, night, converge, max points; step 20 up / 4
down; deadband 6 over 3 cycles; 255 applied whole); disk discovery of rotational ATA/SCSI disks by `lsblk` with stable
`/dev/disk/by-id` names; `smartctl -j -n standby -A` with `timeout 20`, only `temperature.current`; plausibility
(5–90 °C, jump > 10 °C discarded, new base only below 50 °C, "suspicious" readings count for air but never for
shutdown); 5 consecutive failures exclude a disk; blind for 3 cycles → 255; all in standby → floor; stopped-fan rule;
`Type=notify` + `WatchdogSec=180`, `Restart=always`, `RestartSec=30`, `ExecStopPost` backup; exit 78 "driver or
attribute missing" with zero writes.

What changes:

| Private | Product |
|---|---|
| channels `pwm3`/`pwm5` hard-coded | channels with role `base` / `disk-cage` from `hardware.ini`; a channel without `pwm_response = yes` is never written |
| constants per channel in `/etc/wtr-ventiladores.conf` | per-role constants from the active profile (§3.7); `profile = custom` exposes them in `config.ini` |
| a city-specific holiday calendar copied from another service | §3.8 schedule |
| `instance` labels, hostname check | none; metrics only if a textfile directory exists |
| Spanish subcommands | `run`, `backup`, `status`, `decide --at`, `calibrate`, `restore-firmware` |
| `ConditionPathExists` absent | `ConditionPathExists=/etc/wtr-max-fans/hardware.ini` on the unit: no calibration, no controller |
| hwmon path `it87.2608` | `glob('/sys/devices/platform/it87.*/hwmon/hwmon*')` filtered by `name == it8613` |

### 3.6 Guard (`wtr-max-fans-guard.service`)

Kept: a separate process that never dies because an action failed; 30 s cycle; steps 0 (watching), 1 (full air at
≥ 54 °C, step file in `/run/wtr-max-fans/step` refreshed each cycle and honoured by the controller only with
mtime < 90 s, released below 50 °C), 2 (shutdown), 3 (isolated disk ≥ 58 °C without corroboration: air + critical
alert, no shutdown), 4 (blind 5 min: air), 5 (caution 30 min after a thermal shutdown); last resort `pwmN_enable=0`
when `auto_start` is missing, sticky until `wtr-max-fans acknowledge`; persistent shutdown marker in
`/var/lib/wtr-max-fans/`; shutdown chain `shutdown -h +1` → `systemctl poweroff` → sysrq.

Changed:

- **`action = alert` by default.** Step 2 then performs step 1 plus a critical alert ("disk `<id>` at `<T>` °C with
  `<id2>` at `<T2>` °C: fans at 100 %; the NAS is NOT shutting down because overheat protection is set to *alert*") —
  no power action. `action = shutdown` is set only from the menu screen that shows the rule in full.
- **Single managed disk**: `shutdown` is accepted in the config but degrades to `alert` at runtime, with a once-per-boot
  log and a menu notice: the corroboration rule needs a second disk by design.
- **Alert delivery** (`alerts.py`, shared with the controller): journal at `crit`/`warning`; if `/usr/sbin/sendmail`
  exists, a short mail to `root` (Proxmox forwards it to the configured administrator address; Debian/OMV may need a
  local MTA, which the README says); optional `command =` hook executed with `WTR_FANS_EVENT`, `WTR_FANS_SEVERITY`,
  `WTR_FANS_MESSAGE` in its environment, `timeout 30`, failures logged and never fatal. Alerts are rate-limited to one
  per state change plus one reminder per 6 h while the state persists.
- The guard also owns the **boot-time health check** (driver missing, DKMS without a module for the running kernel,
  calibration missing): one alert per boot, so that an owner learns that the fans are on the firmware curve.

### 3.7 Profiles and night mode

All profiles share the temperature points `T_floor = 38`, `T_silence = 46`, `T_converge = 50`, `T_max = 52` (°C),
the guard thresholds `54 / 50 / 58 / 50` and the step/deadband parameters. Values are PWM 0–255 (percent):

| Role | Parameter | `quiet` | `balanced` | `cool` |
|---|---|---|---|---|
| `disk-cage` | floor | 102 (40 %) | 115 (45 %) | 128 (50 %) |
| `disk-cage` | day at 46 °C | 128 (50 %) | 153 (60 %) | 191 (75 %) |
| `disk-cage` | night at 46 °C | 115 (45 %) | 153 (60 %) | 153 (60 %) |
| `disk-cage` | backup (ExecStopPost) | 128 | 153 | 153 |
| `base` | floor | 77 (30 %) | 77 (30 %) | 100 (39 %) |
| `base` | day at 46 °C | 77 (30 %) | 100 (39 %) | 128 (50 %) |
| `base` | night at 46 °C | 77 (30 %) | 77 (30 %) | 100 (39 %) |
| `base` | backup | 77 | 77 | 100 |

`balanced` is the maintainer's measured comfort point (rear 60 %, bottom 30 % at night; disks 33–42 °C). `quiet`
lowers only the ≤ 46 °C band (above it the curve converges to the day curve at 50 °C and reaches 255 at 52 °C, so the
safety ceiling is identical); its rear floor of 40 % is one step above the 39 % the firmware applied on the
maintainer's unit, where disks stayed at 38–46 °C. `cool` is for warm rooms or disks that sit above 46 °C on
`balanced`; the status screen suggests it when the 24 h maximum exceeds 46 °C. No profile goes below 77 on `base`
until a tachometer-verified lower floor exists (the private audit's N-I3 finding); `custom` lets an owner set every
value in `config.ini` under `[curve.disk-cage]` / `[curve.base]`, validated against the invariants (floor ≥ 51,
monotonic, night ≤ day ≤ 255).

Night mode (`schedule.py`): `night = on|off`, `night_start = 21:00`, `night_end = 09:00`, optional
`night_end_weekend = 10:00`, optional `holidays_file` (ISO dates, one per line, `#` comments; treated like weekends);
evaluated every cycle in the system's local time zone (`/etc/localtime`), DST-safe by comparing wall-clock times.
`mode = auto|day|night` forces. Transitions ramp with the normal step limits (night → day in ≤ 2 min, day → night in
≤ 6 min on `balanced`).

### 3.8 Menu and CLI

`sudo wtr-max-fans` (and `sudo ./install.sh` at the end of a successful install) opens:

```
wtr-max-fans 1.0.0 — AOOSTAR WTR MAX fan control
  1  Status            fans, disk temperatures, profile, day/night, protection, driver
  2  Calibrate fans    detect which fan is which and whether it responds (required once)
  3  Profile           quiet / balanced / cool
  4  Night mode        on/off, hours, weekends, holidays
  5  Overheat protection   alert (default) / shutdown
  6  Noise test        hear the profiles again
  7  Report            bundle for an issue (no secrets)
  8  Uninstall
  0  Quit
```

- `ui.py` wraps `whiptail --menu/--yesno/--inputbox/--msgbox/--gauge`; sizes computed from `stty size`; when stdin or
  stdout is not a TTY (`os.isatty`) the menu refuses with the CLI equivalents listed, so that scripts and tests use
  `wtr-max-fans profile balanced`, `night on --start 21:00 --end 09:00`, `protection alert|shutdown`, `status --json`,
  `calibrate --yes` (non-interactive, keeps defaults, skips the noise test), `report`, `uninstall --yes [--purge]`.
- "Overheat protection → shutdown" shows the rule verbatim ("…only when one disk reads ≥ 58 °C three times in a row
  and a second disk reads ≥ 50 °C; never in the 30 minutes after a thermal shutdown; the machine powers off cleanly
  after a 1-minute warning") and requires typing `shutdown` to confirm.
- "Report" writes `/var/lib/wtr-max-fans/report-<timestamp>.tar.gz` with DMI, `dkms status`, the hwmon listing,
  `smartctl --scan` and a temperature summary (no serial numbers), both INI files, the last 500 journal lines of both
  units, `VERSION`; it never includes hostnames or addresses beyond what the journal lines carry, and the README says
  what is inside.

### 3.9 Configuration, state and install surface

| Path | Content | Notes |
|---|---|---|
| `/etc/wtr-max-fans/config.ini` | `[profile] name`; `[schedule]`; `[guard] action, full_air_c, release_c, shutdown_c, corroborate_c, caution_min`; `[alerts] mail, command`; `[metrics] textfile_dir = auto|off|<path>`; `[disks] devices = auto|<list>`; `[curve.*]` only for `custom` | written by the menu with `configparser`; re-read every cycle; invalid value → factory value + metric/alert, never a crash |
| `/etc/wtr-max-fans/hardware.ini` | calibration output and firmware snapshot (§3.4) | written only by `calibrate`; the controller's start condition |
| `/etc/wtr-max-fans/holidays.txt` | optional | not shipped; example in `config/` |
| `/etc/systemd/system/wtr-max-fans-{controller,guard}.service` | units | hardening as in the private spec (`ProtectSystem=strict`, `PrivateNetwork=yes`, `DevicePolicy=closed` + `DeviceAllow=block-sd rw`, capability bounding set, `SystemCallFilter=@system-service`); the guard adds `CAP_SYS_BOOT` only when `action = shutdown` (a drop-in written by the menu) |
| `/usr/local/sbin/wtr-max-fans`, `/usr/local/lib/wtr-max-fans/` | program | FHS `/usr/local` |
| `/usr/src/wtr-max-fans-it87-<ver>/`, `/etc/modules-load.d/it87.conf` | driver | |
| `/var/lib/wtr-max-fans/` | shutdown markers, reports, calibration log | `StateDirectory` |
| `/run/wtr-max-fans/` | lock, guard step file | `RuntimeDirectory` |
| `<textfile_dir>/wtr_max_fans.prom` | metrics | only if the directory exists (`/var/lib/prometheus/node-exporter` by default) |

### 3.10 Installation, upgrade, uninstallation

**Install** (README, verbatim for the user):

```
apt install git            # only if missing
git clone --branch v1.0.0 --depth 1 https://github.com/phyrexia/wtr-max-fans.git
cd wtr-max-fans
sudo ./install.sh
```

`install.sh` → root check → `apt-get install` prerequisites → `python3 -I bin/wtr-max-fans install`, which runs the
gates G1–G5 (§3.3), copies the program, verifies `driver/SHA256SUMS`, `dkms add/build/install`, handles Secure Boot,
`modprobe it87`, runs G3, installs and starts the **guard** in `alert` mode, installs the controller unit (enabled,
waiting for `hardware.ini`), then opens the menu on **Calibrate**. Idempotent: running it again upgrades in place,
keeps `/etc/wtr-max-fans/`, rebuilds DKMS only when `PACKAGE_VERSION` changed, restarts the services, and prints what
changed. `--check` runs the gates and reports without writing. Total time on a stock PVE 9 with internet: ≤ 10 min.

**Why `git clone` + `sudo ./install.sh` and not `curl | bash`**: the script runs as root and builds a kernel module;
a local copy can be read before running, is re-run for upgrade and uninstall, carries its version (`git describe`,
`VERSION`), and cannot execute half a script when the connection drops (bash executes a piped script line by line as
it arrives; a `set -e` pipeline also masks a failed download). A tagged clone pins the bytes; signed tags
(`git tag -s`, SSH signature) let the careful user verify them with `git verify-tag`. Release tarballs with
`SHA256SUMS` are offered for users without git. The UGREEN precedent uses the same entry point.

**Uninstall** (`wtr-max-fans uninstall`, menu item 8): stop the controller (backup values applied), restore the
firmware snapshot from `hardware.ini` on every channel it touched (`auto_point*_temp`, `auto_slope`, `auto_start`,
`enable`), stop and disable both units, remove units, program, DKMS package (`dkms remove --all`),
`modules-load.d` entry, metrics file, `/var/lib/wtr-max-fans/` (keeps shutdown markers unless `--purge`); keep
`/etc/wtr-max-fans/` unless `--purge`; leave apt packages installed (listed on screen for manual removal); remove
`/var/lib/dkms/mok.*` **only** with `--purge` and only when `dkms status` is empty. A reboot is not required: the
chip is back on the firmware's own numbers immediately; without the module the firmware curve applies after the next
boot regardless.

### 3.11 Failure modes

| Situation | Behaviour | Carried from the private spec? |
|---|---|---|
| Driver not loaded (kernel without module, Secure Boot pending, `modprobe -r`) | controller exits 78 with zero writes, retries every 30 s; guard runs, cannot write, alerts once per boot; fans on the firmware curve | yes |
| `pwmN_auto_start` missing on a managed channel (stale DKMS) | controller writes nothing on any channel; guard's last resort `enable=0` + sticky `manual_emergency` + alert until `acknowledge` after a rebuild | yes |
| Controller dies / hangs | `ExecStopPost` backup ≤ 5 s; watchdog 180 s; restart 30 s | yes |
| Guard dies | restart 10 s; the controller's curve still reaches 255 at 52 °C | yes |
| Disks asleep | never woken; floor | yes |
| No readable disk for 3 cycles | 255, state `blind`; guard: 5 min → air | yes |
| Stopped fan on a managed channel | other managed channel to 255; alert | yes (needs 2 managed channels; with 1 → alert only) |
| Guard step active | controller does not write | yes |
| Firmware with different Off/Start temperatures or slope | `prepare` flattens the curve the same way; uninstall restores the recorded values, not constants | **new** |
| Firmware re-arms its curve at runtime (not observed) | `prepare`/cycle corrects `enable` and `auto_point2_temp` drift (counter `corrections_total`); if drift persists > 10 corrections/h → alert "firmware fights the controller" and the status screen says so | **new** |
| More or fewer fans, swapped headers | roles and PWM response from calibration; nothing assumed | **new** |
| Stock DC fans | classified "does not respond"; guard only; the menu says control is unavailable and why | **new** |
| 1 disk, or disks that report no `temperature.current` | shutdown degrades to alert; disks without the attribute are excluded; none left → blind | **new** |
| Other disk models with different limits | thresholds are below every consulted specification (60 °C hard limit); `custom` allows lower, never higher than 58 for shutdown | **new** |
| Another controller installed later (`fancontrol`) | drift corrections + alert as above; install refused up front | **new** |
| Unreadable `holidays.txt` | day curve, one log line | changed (private failed towards night) |
| Second WTR MAX revision with another DMI string | gate G1 refuses; the `report` bundle feeds the known list | **new** |
| Secure Boot with the key rejected | `modprobe` error surfaced verbatim on the status screen with the enrolment steps | **new** |
| `sendmail` missing (plain Debian) | journal + hook only; the README says how to add an MTA | **new** |

### 3.12 Observability

Metrics (`wtr_max_fans_*`, same shape as the private `wtr_*` series: disk temperatures by stable id, max, PWM
target/applied/read by channel and role, mode, state, guard state, corrections, last cycle timestamp) written
atomically only when a textfile directory exists. `contrib/prometheus/rules.yml` + `rules.test.yml` carry the seven
alerts of the private deployment with no `instance` label pre-filled; `promtool check rules` and `promtool test
rules` run in CI. `wtr-max-fans status --json` gives the same data without Prometheus.

### 3.13 Security

Root is required (sysfs writes, `SG_IO`, DKMS). Both services carry the private spec's hardening set; no network
(`PrivateNetwork=yes`, `IPAddressDeny=any`); no telemetry. Inputs treated as untrusted: `smartctl` JSON (types
checked), INI files (validated, never interpolated into a shell), the hook command (executed via `subprocess` without
a shell, with a timeout). `install.sh` installs only from the distribution's apt sources; the driver bytes are hash-
verified against the repository's `SHA256SUMS` before `dkms add`. The `report` bundle lists its contents in the README.
`SECURITY.md` names the reporting channel (GitHub private vulnerability reporting).

### 3.14 Tests

**Unit (pytest, no hardware).** One fixture builds a fake system root (`WTR_FANS_SYSROOT`): an `it87.2608/hwmon/hwmon0`
tree with every attribute (variants without `pwm5_auto_start`, without `fan4_input`, with `name != it8613`), fake
`/sys/class/dmi/id`, fake `/etc`, `/run`, `/var`; fake `smartctl`, `lsblk`, `systemctl`, `shutdown`, `sendmail`,
`dkms`, `mokutil`, `whiptail` on `PATH` driven by scenario files that record every call. Cases: the 17 of the private
spec (law, step/deadband, calendar transitions, readings, backup, stopped fan, orphan step file, calibrate lock,
guard steps 1–5, actions chain, metrics format) plus: G1–G5 gates with each failure; calibration classification
(PWM / DC / no tachometer) with the 5/6 rule and the snapshot restored on exception; role assignment defaults and
overrides; the profile table (exact values at 38/42/46/48/50/52 for each role × profile × day/night, continuity and
monotonicity); `custom` validation; shutdown degrade with one disk; `alert` default performs no power action;
firmware snapshot restore on uninstall; unreadable holidays → day; INI round-trip through the menu's writers;
non-TTY refusal; `report` bundle contents. Run on the remote executor, never on the Mac:
`qf-test up --generic --auto --allow-qf-infra` → `qf-test exec -- python3 -m pytest -q tests` → `qf-test artifacts`
→ `qf-test down`.

**CI (GitHub Actions, public).** Jobs on push and pull request: `shellcheck install.sh`; `pytest` on Python 3.11 and
3.13 (Debian 12 and 13); `promtool check rules` + `promtool test rules` on `contrib/prometheus/`; **driver compile** in
containers `debian:12` and `debian:13` with `linux-headers-amd64` and `debian:13` with the PVE no-subscription
repository and `proxmox-default-headers`, applying the patch and running `make -C /usr/src/linux-headers-<ver>
M=<tree> modules` (no module load); `install.sh --check` inside `debian:13` must exit 3 with the "not a WTR MAX"
message; `reuse lint`. A CI failure of the driver job on a new PVE kernel is the early warning the maintainer needs
before owners update.

**Hardware (the maintainer's unit, during `v1.0.0-rc.1`).** This *is* the migration (D15), executed in one session:

1. `wtr-max-fans check` from the clone: expected exit 5 (legacy units active) — proves G4.
2. Private `instalar.sh revertir --tambien-guardia` is **not** run first; instead: install the product's **guard
   only** (`install --guard-only`, alert mode) while the private guard still runs — two guards never conflict (both
   only write 255 above 54 °C) — then stop and disable the private guard. No minute without a guard.
3. Stop the private controller (`ExecStopPost` leaves 153/77), remove the private DKMS package (`it87/…wtr1`), run
   the product install (G4 now passes), calibrate (expected: `pwm5` PWM-controlled, role `disk-cage`; `pwm3`
   PWM-controlled, role `base`; `pwm4` no tachometer; `pwm2` classified and skipped), choose `balanced`.
4. Verify the private spec's H4–H7 and H10 equivalents (prepare values, kill -9, `modprobe -r`, reboot) and 24 h of
   disk temperatures ≤ 46 °C with the night ramp observed.
5. Switch the home alert rules to `wtr_max_fans_*` (a change in the maintainer's private repository, outside this
   product); the private files stay in that repository as history until the product is tagged `v1.0.0`.
6. **Rollback** (≤ 2 min): `wtr-max-fans uninstall`, then the private `instalar.sh instalar` and DKMS reinstall as
   documented there.

### 3.15 Versioning, releases, documentation

- SemVer in `VERSION`; `CHANGELOG.md` (Keep a Changelog); tags `vX.Y.Z` signed; GitHub Release with the source tarball
  and `SHA256SUMS`. Driver pin bump or patch change → minor; config format change → major; `v1.0.0` is the first
  public tag and the moment the repository goes public.
- `README.md` (English) and `README.es.md` (Spanish mirror, same headings): what it does; requirements (hardware,
  OS, PWM fans); **Safety** (what it never does, what happens when it dies, protection default and the shutdown rule,
  the firmware fallback); install; calibration with screenshots (`docs/img/`); profiles and night mode; uninstall;
  troubleshooting with the exit codes; metrics; development and tests; licence and trademark notice.
- Licences: `driver/` GPL-2.0-only; everything else MIT; `LICENSES/` + SPDX headers per file (REUSE); the README's
  licence section explains the split in two sentences.
- Migration text for the maintainer's legacy units lives in `docs/` and is printed by gate G4.
- Issue template requires the `report` bundle; `SECURITY.md`; `CONTRIBUTING.md` (tests must pass remotely and in CI;
  one functional change per commit).

## 4. Alternatives rejected (each with a measurable reason)

| Alternative | Rejected because |
|---|---|
| **Web interface** | Adds a network listener on the NAS host plus authentication for a configuration done once; ≥ 2 000 lines and a framework against 0 extra packages for whiptail (`Priority: important` on Debian). Proxmox already offers a shell in its web UI. |
| **lm-sensors `fancontrol` + `pwmconfig`** | Manual mode only: the chip freezes at the last duty when the process is killed (its restore runs on trap, not on SIGKILL); needs an hwmon temperature input, so no SMART/standby awareness; no night mode; cannot express "automatic mode + Start PWM"; `pwmconfig` is interactive and not disk-aware. |
| **fan2go** | Manual PWM mode (same freeze risk); AGPL-3.0 would bind the whole product; no thermal guard; distributed as a binary/AUR, not apt. Its calibration idea is adopted. |
| **hddfancontrol** | Manual mode; no night mode; Rust toolchain or binary; its control law is adopted. |
| **Upstream submission of the patch** | Owner decision. Measurable context: mainline IT8613E support was accepted and then withdrawn in 2026; the out-of-tree project's response time is unknown; a vendored pin plus CI compile gives a deterministic answer per kernel instead. |
| **`.deb` package + apt repository** | Needs repository hosting, a signing key lifecycle and debconf for a calibration that must run interactively; `dpkg -i` of a downloaded `.deb` is no safer than a tagged clone. Revisit at ≥ 50 installs or with a volunteer packager. |
| **`curl \| bash`** | Partial execution on connection loss; pipeline exit masking; server-side detection of piped requests; no local copy for `--check`, upgrade and uninstall. |
| **`git clone` of the upstream driver at install time** | Needs network and git at install, exposes installs to upstream history changes, and CI could not compile the exact bytes that users build. |
| **TOML/YAML configuration** | `tomllib` cannot write, `yaml` is not in the standard library; `configparser` reads and writes with zero dependencies. |
| **`drivetemp` instead of `smartctl`** | Its behaviour on standby disks was not verified in this research; `smartctl -n standby` has a documented guard and JSON output. Revisit when verified (would drop the `smartmontools` dependency). |
| **One combined daemon** | The guard must survive controller bugs; two processes with separate restart policies and capability sets, as audited. |
| **Go or Rust rewrite** | Toolchain or binary distribution for a ≤ 3 000-line program; Python ≥ 3.11 is on every target. |
| **Allowing non-WTR-MAX boards with the same chip in v1** | No calibration evidence from any such board; the gate stays closed and `report` collects the evidence (grill Q2). |

## 5. Consequences

- Owners get: an installer that refuses on the wrong hardware, says whether their fans can be controlled, picks a
  profile from a noise test, keeps the firmware in charge after uninstall, and never powers off unless they opt in.
- The maintainer takes on: a vendored driver pin to bump, a CI matrix to keep green on PVE kernel releases, two
  READMEs, and support through `report` bundles within the ≤ 2 h/week budget.
- The private deployment is retired in favour of the product after the migration (D15); its alert rules move to the
  product's metric names in the maintainer's repository.
- Safety invariants 1–7 of the grill are testable statements in `tests/`; any change to them is a major version.
- The `custom` profile and the `command` hook are the two escape hatches; everything else is a menu choice.

## 6. What would revisit this decision

- Mainline or PVE kernel gains native IT8613E support with correct `pwm4`/`pwm5` and visible `auto_start`: drop
  `driver/`, keep the controller (minor release with a migration step).
- A second board with the same chip is confirmed by a `report` + calibration: extend G1's list (WTR PRO).
- A WTR MAX unit where calibration cannot classify the fans or where the firmware re-arms its curve at runtime:
  redesign of the hybrid mode, not a patch.
- Verified `drivetemp` standby semantics: drop `smartmontools`.
- ≥ 50 installs, or TrueNAS/Unraid demand: packaging (`.deb`, plugin) and possibly a web view read-only from the
  metrics.
- Secure Boot enrolment failing for owners in practice (issues): a signed-module distribution would need a different
  trust model than a MOK per machine.
- `frankcrawford/it87` stops building on a PVE kernel and no fix lands upstream: the vendored tree becomes a fork in
  fact; decide then whether to maintain it or to drop PVE versions.

## 7. Status

**proposed** — awaiting the Owner's direction approval (phase 2 exit) and the adversarial audit (phase 3).
