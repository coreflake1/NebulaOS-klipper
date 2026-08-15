# NebulaOS Klipper

This is the Klipper fork [NebulaOS](https://github.com/coreflake1/NebulaOS) runs on the printer.
The interesting NebulaOS-specific bits are mostly PRTouch (the load-cell probe), Z-compensation,
and a bit of TMC status reporting — all in `klippy/extras/` (`prtouch_*.py`, `z_compensate.py`,
`tmcstatus.py`, plus their tests).

Branch: `master`. You don't need to clone or build this repo on its own —
[`NebulaOS-firmware`](https://github.com/coreflake1/NebulaOS-firmware) pins an exact commit here
and pulls it in as part of the full build.

Outside `klippy/extras/`, this fork does differ from real upstream Klipper in a few places, but
those differences predate NebulaOS entirely — they're inherited BTT Eddy current-sensor support
from whatever this fork was built on top of, not something NebulaOS added. Everything NebulaOS
actually wrote lives in `klippy/extras/`. See
[`docs/NEBULAOS_FORK_DIFFERENCES.md`](docs/NEBULAOS_FORK_DIFFERENCES.md) for the full breakdown if
you want the details.

## Developer documentation

Build/install/update/recovery docs all live in `NebulaOS-firmware`, not here:

- [`NebulaOS-firmware` wiki](https://github.com/coreflake1/NebulaOS-firmware/wiki)
- [Build From Source](https://github.com/coreflake1/NebulaOS-firmware/blob/main/docs/BUILD_FROM_SOURCE.md) — how this repo's pinned commit gets fetched and cross-compiled
- [Developer Update](https://github.com/coreflake1/NebulaOS-firmware/blob/main/docs/DEVELOPER_UPDATE.md) — how a Klipper change actually reaches a device

For the PRTouch/Z-compensation work specifically, see
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
