# This fork's differences from upstream Klipper

We used to say this fork had zero core differences from upstream Klipper outside
`klippy/extras/`. That wasn't quite true, so this page exists to set the record straight. Diffed
against real upstream `Klipper3d/klipper` at the actual merge-base (`14cbb8dd`, 2025-06-02), 34
files outside `klippy/extras/` and `klippy/chelper/` do differ from stock Klipper. Here's the
honest breakdown, split into what NebulaOS is actually responsible for and what it isn't.

## Inherited from the base fork (not NebulaOS's doing)

These are real differences, but they predate this project entirely — inherited from whichever
pellcorp/SimpleAF-lineage base fork this repo started from, not written here:

- `src/sensor_ldc1612_ng.c` (935 new lines) — a BTT Eddy current-sensor driver
- `klippy/mcu.py` (~124 lines) — reconnect-handling additions that go with the BTT Eddy integration
  (`handle_non_critical_disconnect`, `non_critical_recon_event`, `recon_mcu`,
  `reset_to_initial_state`, `_check_serial_exists`, and a few related event-name accessors)
- `klippy/clocksync.py`, `klippy/queuelogger.py`, `klippy/serialhdl.py`, `klippy/stepper.py`,
  `klippy/klippy.py` — small supporting changes for the same reconnect-handling work
- `.config.btteddy`, `.config.host` — build config fragments
- `fw/*` — prebuilt firmware binaries for various boards
- `src/basecmd.c`, `src/linux/main.c`, `src/Makefile` — small supporting changes
- `.github/workflows/*` removed, `docs/prtouch_timer_incident_forensics.md` added (that one's ours, not upstream's)
- `build.sh`, `_build.sh` — build-script additions

All of this dates to a 2025-07-18 commit ("updated btt eddy config") — before this project's own
history even starts (our first commit is from 2026-07-something onward). This is inherited fork
lineage. PRTouch or any other NebulaOS work didn't introduce it and isn't responsible for
maintaining it.

## What NebulaOS actually added

Nothing, outside `klippy/extras/`. Every commit in this fork's own history — PRTouch,
Z-compensation, load-cell safety hardening, physical qualification, all of it — that touches files
outside `klippy/extras/`/`klippy/chelper/` turned out, on inspection, to be a mirror-sync or
test-support change, not an actual new core Klipper behavior. Everything NebulaOS is really
responsible for lives in `klippy/extras/` (`prtouch_*.py`, `z_compensate.py`, `tmcstatus.py`, and
their tests).

## Why this matters

If this fork ever rebases onto newer upstream Klipper, the inherited BTT Eddy work above (and its
`mcu.py` dependencies) needs to be accounted for — those are real source-level changes upstream
doesn't have, not just merge noise. Nothing NebulaOS itself added needs the same treatment, since
that list is empty — our own work stays fully isolated to `klippy/extras/`.
