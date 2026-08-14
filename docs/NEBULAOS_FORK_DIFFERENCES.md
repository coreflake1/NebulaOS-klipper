# This fork's differences from upstream Klipper

This document exists because an earlier audit asserted `UPSTREAM_KLIPPER_CORE_DIFFS=NONE`
for this fork, and that claim does not hold literally. Diffed against genuine upstream
`Klipper3d/klipper` at the real merge-base (`14cbb8dd`, 2025-06-02), 34 files outside
`klippy/extras/` and `klippy/chelper/` differ from stock Klipper. This document splits
those diffs into two categories so "no core diffs" is never asserted incorrectly again.

## INHERITED_BASE_FORK_CORE_DIFFS

Real, but predates this project's own history and was inherited from whichever
pellcorp/SimpleAF-lineage base fork this repo started from, not authored here:

- `src/sensor_ldc1612_ng.c` (935 new lines) — a BTT Eddy current-sensor driver
- `klippy/mcu.py` (~124 lines) — reconnect-handling additions
  (`handle_non_critical_disconnect`, `non_critical_recon_event`, `recon_mcu`,
  `reset_to_initial_state`, `_check_serial_exists`, related event-name accessors)
  that go with the BTT Eddy integration
- `klippy/clocksync.py`, `klippy/queuelogger.py`, `klippy/serialhdl.py`,
  `klippy/stepper.py`, `klippy/klippy.py` — small supporting changes for the same
  reconnect-handling work
- `.config.btteddy`, `.config.host` — build config fragments
- `fw/*` — prebuilt firmware binaries for various boards
- `src/basecmd.c`, `src/linux/main.c`, `src/Makefile` — small supporting changes
- `.github/workflows/*` removed, `docs/prtouch_timer_incident_forensics.md` (this
  project's own, not upstream's)
- `build.sh`, `_build.sh` — build-script additions

Dated to a 2025-07-18 commit ("updated btt eddy config") — before this project's own
history begins (its own first commit is 2026-07-something forward). This is
inherited fork lineage, not something PRTouch or any other NebulaOS mission
introduced or is responsible for maintaining the correctness of.

## NEBULAOS_ADDED_CORE_DIFFS

**None found.** Every commit in this fork's own history (the PRTouch/z_compensate
missions, load-cell safety hardening, physical qualification, etc.) that touches
files outside `klippy/extras/` and `klippy/chelper/` was, on inspection, a
mirror-sync or test-support change — not a new core Klipper behavior change. This
project's own accepted work is fully contained in `klippy/extras/` (the
`prtouch_*.py`, `z_compensate.py`, `tmcstatus.py` family and their tests).

## Practical implication

A future rebase onto newer upstream Klipper needs to account for the
`INHERITED_BASE_FORK_CORE_DIFFS` list above (the BTT Eddy work and its `mcu.py`
dependencies) — those are real source-level changes upstream doesn't have, not
mechanical merge noise. It does **not** need to account for anything from
`NEBULAOS_ADDED_CORE_DIFFS`, because that list is empty: this project's own work
stays isolated to `klippy/extras/`.

_Added 2026-08-14 as part of the NebulaOS repository canonicalization mission._
