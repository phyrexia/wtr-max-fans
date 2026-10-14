<!--
SPDX-FileCopyrightText: 2026 Gabriel González Pineda <https://github.com/phyrexia>
SPDX-License-Identifier: Apache-2.0
-->

# Driver: `it87` for the ITE IT8613E (DKMS, pinned commit + one local patch)

The services in `v0/` need the out-of-tree `it87` driver from
[frankcrawford/it87](https://github.com/frankcrawford/it87), built with DKMS from a pinned commit plus the patch in
this directory. This page lists the exact steps used on the maintainer's machine (Proxmox VE, kernel
`7.0.14-23-pve`).

## Why this driver and why the patch

- The mainline `it87` (kernel 7.0 and master) does not know the IT8613E: `modprobe it87` returns "No such device".
  Forcing it with `force_id=0x8622` reads the fans but hides `fan5` (SATA) and reads `pwm4`/`pwm5` from the
  current-output registers instead of the control registers. **Never write any `pwm*` with that driver.**
- `frankcrawford/it87` supports the IT8613E natively.
- On "newer autopwm" chips the pinned driver hides `pwm4..6_auto_start` because of an attribute-index mapping
  error in `it87_auto_pwm_is_visible()` (mainline has the same line). The patch adds three lines so that
  `pwm4_auto_start` and `pwm5_auto_start` appear. The SATA channel is `pwm5`, and `v0/` only writes
  `pwmN_auto_start`, so **without the patch the controller refuses to run** (exit code 78).

On the maintainer's machine, after the patch, only `pwm4_auto_start` and `pwm5_auto_start` appeared in the hwmon
directory, their values matched the Start PWM registers, and writing `pwm5_auto_start` in automatic mode changed
the fan speed as expected.

## Files

| File | What it is |
|---|---|
| `0001-it87-expose-pwm4-6-auto-start.patch` | The patch, in `git format-patch` form, against commit `bd7a566c8a60fe0308d67e26761e16260368191a`. sha256 `b145523cad39dc70ca2016f9750fbfdb06bb364382534d05f2e4bba6d85ebf54`. GPL-2.0-only. |
| `0001-it87-expose-pwm4-6-auto-start.patch.license` | REUSE licence sidecar for the patch. |
| `COPYING` | GPL-2.0 text. |

## Install (as root)

```bash
# 1) Compiler, DKMS and the headers of the running kernel. On Proxmox VE the metapackage
#    proxmox-default-headers also brings the headers of every future kernel, so DKMS can rebuild.
apt-get install -y --no-install-recommends dkms gcc proxmox-default-headers

# 2) Pinned driver source.
mkdir -p /root/src && cd /root/src
git clone https://github.com/frankcrawford/it87.git it87
cd it87 && git checkout bd7a566c8a60fe0308d67e26761e16260368191a
git rev-parse HEAD     # bd7a566c8a60fe0308d67e26761e16260368191a
sha256sum it87.c       # b11c5dd669b6a2a38ad39952784bfeff320e539843d22639c97cf6a11d25d703 (unpatched)

# 3) Local patch. Copy driver/0001-it87-expose-pwm4-6-auto-start.patch from this repository to /root/src/.
sha256sum /root/src/0001-it87-expose-pwm4-6-auto-start.patch
#                      b145523cad39dc70ca2016f9750fbfdb06bb364382534d05f2e4bba6d85ebf54
git checkout -b wtrmax/auto-start-pwm4-6
git -c user.name="wtr-max-fans" -c user.email="root@wtrmax.invalid" am /root/src/0001-it87-expose-pwm4-6-auto-start.patch
sha256sum it87.c       # 183d4a80d286dbee70b23178b146ee9f24ecd622c54ca7301c7944b4207df7e5 (patched)

# 4) DKMS with its own version string, so it cannot be mistaken for upstream. The Makefile's dkms target
#    runs dkms add, build and install, then modprobe it87. (Not dkms-install.sh: it derives the version
#    from git describe.)
V=v2.0-6-gbd7a566.20261002.wtr1
make -f Makefile DRIVER=it87 DRIVER_VERSION=$V DKMS_ROOT_PATH=/usr/src/it87-$V dkms

# 5) Load at every boot, with no options (neither force_id nor ignore_resource_conflict).
install -m 0644 /path/to/wtr-max-fans/v0/it87.conf /etc/modules-load.d/it87.conf
```

If an unpatched build of the same commit is already installed, remove it first:
`modprobe -r it87 && dkms remove it87/v2.0-6-gbd7a566.20261002 --all`.

Expected result on the maintainer's machine:

- `dkms status` → `it87/v2.0-6-gbd7a566.20261002.wtr1, 7.0.14-23-pve, x86_64: installed (Original modules exist)`
- `modinfo it87 | grep ^version` → `version: v2.0-6-gbd7a566.20261002.wtr1`
- `modinfo -n it87` → `/lib/modules/7.0.14-23-pve/updates/dkms/it87.ko` (DKMS does not delete the in-kernel
  `it87.ko`; the one in `updates/dkms` takes precedence).

## Check

```bash
H=$(ls -d /sys/devices/platform/it87.2608/hwmon/hwmon*)   # it87.2608 = I/O base 0xA30; the hwmonN number changes
cat $H/name                                              # it8613
ls $H/pwm3_auto_start $H/pwm5_auto_start                 # both must exist (pwm5_auto_start only with the patch)
grep . $H/fan[2-5]_input $H/pwm[2-5]_enable $H/temp[1-3]_input
```

Channel map on the maintainer's WTR MAX (names as in the BIOS "Smart Fan" screen):

| BIOS | Linux | Notes |
|---|---|---|
| CPU FAN | `fan2` / `pwm2` | Follows `temp3` (CPU). Not touched by `v0/`. |
| SYS FAN | `fan3` / `pwm3` | On this unit, the fan in the base. Controlled by `v0/`. |
| MEM FAN | `fan4` / `pwm4` | On this unit, a 2-wire fan: no PWM and no tachometer, so `fan4` reads 0. Not touched by `v0/`. |
| SATA FAN | `fan5` / `pwm5` | The rear fans that cool the disk cage. Controlled by `v0/`. |

There is no `fan1`/`pwm1` on the IT8613E. The board thermistors (`temp1`, `temp2`) do not follow the disk
temperatures, which is why `v0/` reads SMART instead.

## Kernel updates

- `dkms.conf` has `AUTOINSTALL="yes"`; the `dkms` package installs the kernel `postinst.d` hooks, and
  `proxmox-default-headers` brings the new headers in the same upgrade.
- **After every kernel upgrade, and before rebooting into it**, check that DKMS built the module for the new
  kernel:

  ```bash
  dkms status it87     # must list <new kernel>: installed
  ```

  If it is missing, do not reboot until it is fixed, or accept that the new kernel boots without the module: the
  fans then stay on the firmware (BIOS) curve, `wtr-ventiladores` exits with code 78 and stays idle, and the disk
  guard cannot write to the fans.
- After the reboot, `cat $H/name` must print `it8613`.

## Warnings

- **Never set `pwmN_enable=1` (manual mode).** If the program that set it dies, the chip keeps the last PWM until
  the next reboot; if that value was low, the fan stays slow or stopped. `v0/` keeps every channel in automatic
  (`pwmN_enable=2`) and only writes the Start PWM (`pwmN_auto_start`).
- `pwmN_enable=0` is full speed (manual, duty 255) and is the emergency action. It also writes 255 to the Start PWM
  register, so after returning to automatic (`pwmN_enable=2`) the Start PWM stays at 255 until you write
  `pwmN_auto_start` again.
- **Never access the chip through `/dev/port` while the driver is loaded**: both use the index port `0xA35`. Unload
  the driver first (`modprobe -r it87`, which does not change the fans).
- The module is out of tree and unsigned for the kernel unless its key is enrolled: it taints the kernel (`O`,
  `E`). On the maintainer's machine Secure Boot is off; DKMS generated its own MOK key
  (`/var/lib/dkms/mok.key`, `/var/lib/dkms/mok.pub`) and signed the module, but the key is not enrolled. **With
  Secure Boot on, the module will not load until that key is enrolled** (for example with
  `mokutil --import /var/lib/dkms/mok.pub` and the confirmation in MOK Manager at the next boot). This path was not
  tested on the WTR MAX.
- If you update the driver to another upstream commit, re-apply the patch or check that upstream has merged an
  equivalent fix.

## Revert

Back to the unpatched build (`pwm4`/`pwm5` lose `auto_start`; the `v0/` controller then stops with exit code 78):

```bash
modprobe -r it87
dkms remove it87/v2.0-6-gbd7a566.20261002.wtr1 --all
cd /root/src/it87 && git checkout bd7a566c8a60fe0308d67e26761e16260368191a
V=v2.0-6-gbd7a566.20261002
make -f Makefile DRIVER=it87 DRIVER_VERSION=$V DKMS_ROOT_PATH=/usr/src/it87-$V dkms
```

Remove the driver completely (uninstall `v0/` first, see the main README):

```bash
rm /etc/modules-load.d/it87.conf
modprobe -r it87
dkms remove it87/v2.0-6-gbd7a566.20261002.wtr1 --all     # the in-kernel it87.ko comes back; it does not load by itself
rm -rf /usr/src/it87-v2.0-6-gbd7a566.20261002.wtr1 /root/src/it87 /root/src/0001-it87-expose-pwm4-6-auto-start.patch
# The MOK key was generated by DKMS. Delete it only if no other DKMS module remains (dkms status is empty):
rm -f /var/lib/dkms/mok.key /var/lib/dkms/mok.pub
```

A reboot always gives the fans back to the firmware curve.

## Licence

The patch is GPL-2.0-only (it modifies a GPL-2.0 driver); see `COPYING`. This README is Apache-2.0, like the rest
of the repository.
