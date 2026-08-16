# PRTouch internals

This is the one-page map of how a load-cell touch probe on the Ender-3 V3 KE / Nebula Pad
turns into a Z position. The source files themselves remain the detailed reference — this
document exists so a new reader can see how the pieces fit together before diving into any
one of them, and so the many in-source comments that used to point at private working notes
(`ANALYSIS.md`, `DESIGN.md`, `reference/prtouch_v2_wrapper.py`, etc.) now have somewhere real
to point.

Those private notes never existed in this repository or its git history — they were working
notes from the reverse-engineering sessions that produced this code, kept outside the repo.
Where they contained real, load-bearing knowledge, that knowledge has been folded into the
source comments directly (see each file's own header and inline comments). This document adds
the cross-file picture that doesn't belong in any single file.

## What PRTouch actually is

The Ender-3 V3 KE's bed has a load cell mounted under its front-left corner (not on the
toolhead). Creality's stock firmware drives this sensor with a compiled Klipper "extra"
(`prtouch_v2_wrapper.so`) that talks to a small custom command set added to the machine's
own MCU firmware (`prtouch_v2.c`, part of Creality's GD32-based toolhead firmware — see
`docs/prtouch_timer_incident_forensics.md` §7 for how that was confirmed against Creality's
own officially published source). The MCU pulses the Z stepper directly (bypassing Klipper's
normal step-compression/trapq pipeline) while simultaneously sampling the load cell, and
reports back exactly which pulse the load cell tripped on.

`klippy/extras/prtouch_v2.py` and friends are a clean-room, pure-Python **rewrite** of the
*host* side of that system (the compiled `.so`), talking to the **same, unmodified MCU
firmware** over the same wire protocol. Nothing on the MCU was changed or needed to be —
this is a host-side reimplementation using Klipper's standard `create_oid`/`add_config_cmd`/
`lookup_command`/`register_response` primitives (the same pattern `hx711s.py` and
`dirzctl.py` already use on this same board).

`z_compensate.py` is a second, separate module: new code (no Creality source was ever found
for the compensation piece — see its own module docstring), built on top of PRTouch's probing
primitives to do a per-print Z-offset fine-tune against a BLTouch reference.

## Module map

```
                     gcode commands
                          |
                          v
   +----------------------------------------------+
   |  prtouch_v2.py (orchestration / gcode layer)  |
   |  NOZZLE_CLEAR, SAFE_MOVE_Z, READ_PRES,        |
   |  PRTOUCH_CONFIRM_BASELINE, PRTOUCH_TEST_TOUCH |
   +----------------------+-------------------------+
                          |
              +-----------+------------+
              v                        v
   +----------------------+   +--------------------------+
   |  prtouch_probe.py    |   |  prtouch_nozzle.py        |
   |  probing state       |   |  wipe-pad geometry /      |
   |  machine: arm/retry/ |   |  clear_nozzle() sequence  |
   |  recover/settle,     |   |  (calls back into         |
   |  safety guards       |   |  PrtouchProbe.touch_probe)|
   +-----------+-----------+   +--------------------------+
               |
               v
   +----------------------+        +---------------------------+
   |  prtouch_mcu.py       |------->|  prtouch_calibration.py    |
   |  wire protocol:       |  raw   |  pure math: filter samples,|
   |  OIDs, command send,  | sample |  find trigger tick,        |
   |  response buffering   | buffers|  interpolate -> Z          |
   +----------------------+        +---------------------------+
               ^
               |
   +----------------------+
   |  prtouch_units.py     |   (mm/speed <-> MCU step-timing fields,
   |  conversions used by  |    tick <-> seconds, fixed-point scaling)
   +----------------------+

   prtouch_safety_guard.py  - opt-in, test/bring-up-only motion interceptor (NOT wired into
                               production; see its own module docstring)
   prtouch_test_support.py  - fake MCU/config/printer/reactor used by every test_*.py file

   z_compensate.py - separate top-level module. Calls prtouch_v2's touch_probe() at a
                     BLTouch-derived reference point, compares against a live BLTouch-based
                     Z=0, and applies the difference as a per-print SET_GCODE_OFFSET.
```

## Command → physical contact → Z result, step by step

1. A gcode command (`PRTOUCH_TEST_TOUCH`, `NOZZLE_CLEAR`, `Z_OFFSET_CALIBRATION`, ...) calls
   into `PrtouchProbe.touch_probe(down_min_z, ...)` in `prtouch_probe.py`.
2. `touch_probe()` runs a chain of pre-motion safety checks *before arming anything*: only one
   raw MCU operation may be in flight at a time (`_own_raw_operation`), the requested travel
   must be under `max_probe_travel_mm`, and the load-cell baseline must currently look sane
   and self-consistent (`check_sensor_consistency` / the three-state
   `NO_REFERENCE` → `BOOTSTRAP_CANDIDATE` → `TRUSTED_REFERENCE` baseline model — see
   `prtouch_probe.py`'s own `__init__` comments for the full story of why a single reading is
   not trusted). Any of these can raise `PrtouchProbeSafetyError` with zero motion commanded.
3. Once cleared, `_touch_probe()` computes `(step_cnt, step_us, acc_ctl_cnt)` from the
   requested distance/speed via `prtouch_units.py`, arms the MCU's pressure channel
   (`start_pres_prtouch`) and step channel (`start_step_prtouch`) together, and waits for both
   response buffers to fill (`collect_step_samples`/`collect_pres_samples` in
   `prtouch_mcu.py`).
4. The MCU pulses the Z stepper directly, checking the load-cell reading on every pulse. If a
   channel's filtered signal crosses into the `[tri_min_hold, tri_max_hold]` band for
   `tri_need_cnt` consecutive samples, it latches a trigger, records the tick, and reports
   which channel(s) tripped (`tri_chs` bitmask) — this all happens **on the MCU**, using the
   MCU-side filter parameters (`tri_hftr_cut`/`tri_lftr_k1`). If nothing trips, the MCU still
   runs the full commanded pulse count and returns an empty/no-trigger buffer.
5. Back on the host, `prtouch_calibration.compute_trigger_z()` takes the raw step and pressure
   sample buffers and re-derives the trigger position independently, using its **own**
   filter parameters (`cal_hftr_cut`/`cal_lftr_k1`, deliberately distinct config keys from the
   MCU-side ones — see `prtouch_probe.py`'s config parsing). It high-pass + low-pass filters
   the pressure series, finds the trigger sample via a normalize/rotate/minimum trick that
   copes with slow signal drift, linearly interpolates the matching step-buffer position at
   that exact tick, and converts steps → mm via `mm_per_step` to get a Z value.
6. `touch_probe()` repeats this whole attempt up to `retries` times, keeping every accepted
   sample, until two samples agree within `tolerance` (default `probe_min_3err`) or `pro_cnt`
   samples have been collected — then returns the median. Every attempt, triggered or not,
   restores the toolhead to its starting height before the next one (`_lift_after_down` /
   `_recover_after_no_trigger`) because the MCU's raw pulses are invisible to Klipper's own
   toolhead position tracking.
7. For `Z_OFFSET_CALIBRATION` specifically, `z_compensate.py` takes that returned Z, adds
   `tri_expand_mm` (a configurable, unconfirmed compliance correction), and applies it as a
   live `SET_GCODE_OFFSET` — see that module's own docstring for the sign convention and why
   it isn't a permanent `SAVE_CONFIG` by default.

## Two independent filtering passes — a common source of confusion

The MCU-side filter (`tri_hftr_cut`/`tri_lftr_k1`/`tri_min_hold`/`tri_max_hold`/`tri_need_cnt`,
sent via `start_pres_prtouch`) is what actually **decides whether/when a trigger happened** in
real time on the MCU, using whatever raw buffer it has sampled so far. The host-side filter
(`cal_hftr_cut`/`cal_lftr_k1`, used only in `prtouch_calibration.filter_pressure_series`) is a
**separate, offline re-filtering pass** over the full returned sample buffer, used only to
pinpoint the exact interpolated tick within that already-triggered buffer as precisely as
possible. Tuning one does not automatically tune the other, and they are allowed to disagree —
the MCU's tri_* values decide *whether* a probe attempt registers a trigger at all; the host's
cal_* values only affect *where within* an already-triggered buffer the final Z sample lands.

## Confidence, in short

Most of the wire-protocol shapes (field order, fixed-point scaling, tick semantics) are
directly confirmed against Creality's own published source for this board
(`CrealityOfficial/Ender-3_V3_KE_Klipper`, tag `V1.1.0.12`) and cross-checked against live
captured protocol traffic — see `docs/prtouch_timer_incident_forensics.md` for the audit that
established this. The exact physical/force meaning of raw load-cell magnitudes, and a handful
of individual fields whose *name* is confirmed by the wire format but whose precise firmware-
internal behavior isn't (`low_spd_nul`, `send_step_duty`), are not — those are called out
inline, in the relevant source file, as reverse-engineered/uncertain rather than confirmed.

See also: `docs/prtouch_timer_incident_forensics.md` (a real MCU-shutdown incident, its root
cause, and the fixes that came out of it — required reading before changing anything in the
raw-operation arm/disarm/retry sequencing).
