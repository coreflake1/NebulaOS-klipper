# NebulaOS Klipper

This is the Klipper runtime fork used by [NebulaOS](https://github.com/coreflake1/NebulaOS), a
from-scratch custom OS/firmware for the Creality Ender-3 V3 KE.

- **Canonical branch:** `master`.
- **The complete OS is built via [`NebulaOS-firmware`](https://github.com/coreflake1/NebulaOS-firmware)**,
  which pins an exact commit of this repo (`KLIPPER_PIN` in `manifests/dependencies.conf`) and fetches
  it as part of the full build — you don't need to clone or build this repo directly to build NebulaOS.
- **NebulaOS-specific functionality** — PRTouch (the load-cell probe), Z-compensation, and related
  printer-specific behavior — lives in `klippy/extras/` (`prtouch_*.py`, `z_compensate.py`, `tmcstatus.py`
  and their tests).
- **Inherited vs. added core differences:** this fork differs from real upstream Klipper outside
  `klippy/extras/`, but those differences predate NebulaOS's own history (inherited BTT Eddy
  current-sensor support from this fork's base lineage) — NebulaOS's own work is fully contained in
  `klippy/extras/`. See [`docs/NEBULAOS_FORK_DIFFERENCES.md`](docs/NEBULAOS_FORK_DIFFERENCES.md) for
  the full breakdown.

## Developer documentation

You don't need to build this repo standalone — the canonical build/install/update/recovery
procedures live in `NebulaOS-firmware`:

- [`NebulaOS-firmware` wiki](https://github.com/coreflake1/NebulaOS-firmware/wiki)
- [Build From Source](https://github.com/coreflake1/NebulaOS-firmware/blob/main/docs/BUILD_FROM_SOURCE.md) — how this repo's pinned commit gets fetched and cross-compiled
- [Developer Update](https://github.com/coreflake1/NebulaOS-firmware/blob/main/docs/DEVELOPER_UPDATE.md) — how a Klipper change actually reaches a device (component update path)

For this repo's own PRTouch/Z-compensation work specifically, see
[`docs/NEBULAOS_FORK_DIFFERENCES.md`](docs/NEBULAOS_FORK_DIFFERENCES.md) and
[`docs/prtouch_timer_incident_forensics.md`](docs/prtouch_timer_incident_forensics.md).

---

Welcome to the Klipper project!

[![Klipper](docs/img/klipper-logo-small.png)](https://www.klipper3d.org/)

https://www.klipper3d.org/

The Klipper firmware controls 3d-Printers. It combines the power of a
general purpose computer with one or more micro-controllers. See the
[features document](https://www.klipper3d.org/Features.html) for more
information on why you should use the Klipper software.

Start by [installing Klipper software](https://www.klipper3d.org/Installation.html).

Klipper software is Free Software. See the [license](COPYING) or read
the [documentation](https://www.klipper3d.org/Overview.html). We
depend on the generous support from our
[sponsors](https://www.klipper3d.org/Sponsors.html).
