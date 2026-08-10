# PRTouch MCU timer incident — forensics, source-correspondence audit, and next diagnostic plan

Audit date: 2026-08-10. Scope: a live no-trigger `Z_OFFSET_CALIBRATION` test (configured for a
1mm descent that could not physically reach the bed) ended in a real MCU shutdown
(`sentinel timer called`, preceded by five `Timer too close` warnings) and an audibly abnormal
motor sound. This document is the autonomous zero-motion follow-up: it establishes what can and
cannot be proven about the firmware actually running on the device, reconstructs the incident
timeline with explicit confidence labels, resolves the second-JSON-RPC-request question as far as
evidence allows, records the non-reentrancy guard added as a result, and specifies (but does not
execute) the smallest safe next motion experiment.

No gcode was sent to the real printer during this session. Every finding below came from
SSH-based read-only inspection (files, logs, Moonraker/Klipper status queries, `READ_PRES`),
static source inspection, and offline unit tests. The printer was left idle, unhomed, heaters
off, motors de-energized throughout.

---

## 1. Source ↔ running-firmware correspondence

**Question: is `vendor/klipper/src/sched.c` (or any source in this repo) provably the code that
built the MCU firmware currently running on this printer's `F005` mainboard?**

**No — and there is now positive evidence it is not.**

- Live MCU identify string: `Loaded MCU 'mcu' 116 commands
  (38d96adc-dirty-20231016_135251-longer-virtual-machine / gcc 9.2.1 [ARM/arm-9-branch] ...)`,
  `MCU=gd32f303xe`, `CLOCK_FREQ=120000000`, connected via real UART (`serial: /dev/ttyS1` in
  `[mcu]` — not a Linux-software-MCU, a genuine separate microcontroller).
- `38d96adc` does not exist as a commit anywhere in `vendor/klipper`'s git history
  (`git cat-file -t 38d96adc` → "Not a valid object name"). `vendor/klipper` HEAD is an unrelated
  commit (`0e5785da`, 2026-08-07). The firmware build date (Oct 2023) predates this whole project.
- `vendor/klipper/src/sched.c` was vendored wholesale in one commit ("add ender 3 v3 firmware
  blobs", `386fde4`, pellcorp/creality import) alongside **precompiled** firmware blobs at
  `fw/*/mcu0_*.bin` — i.e. this repo's own history already treats the mainboard MCU firmware as a
  separate, binary-only artifact from the buildable `src/` tree.
- This device's board code is `F005` (independently confirmed elsewhere in this repo's
  `FIRMWARE.md` via `/etc/ota_info` and U-Boot's own `nvram model: F005` output), matching
  `fw/F005/mcu0_001_G32-mcu0_007_000.bin`. That file is present both in this repo and on the live
  device (`/usr/data/nebulaos/apps/klipper/fw/F005/...`, byte-identical path/name). `file` reports
  it as raw opaque "data" (no ELF header); `strings -n 4` finds **zero** readable text anywhere in
  it — no "klipper", "prtouch", "timer", "shutdown", nothing. This is almost certainly an
  encrypted/obfuscated Creality OTA package, not a directly-inspectable compiled image.
- **No consumer of `fw/*.bin` exists anywhere on the live custom OS** (`grep -rl "fw/F005\|mcu0_"`
  across the whole `klipper` app tree matches only `.git/index`). **No `gd32` build target exists**
  in this Klipper source tree's `src/` at all (only `atsam, stm32, lpc176x, hc32f460, simulator,
  ar100, generic, linux, atsamd, rp2040, pru, avr`). **No file anywhere in the reachable custom-OS
  filesystem mentions "gd32"/"GD32" in any form.** The mainboard MCU update mechanism, if one
  exists, is not part of NebulaOS/SimpleAF at all — it almost certainly lives in Creality's
  separate stock firmware/OS partition, which was not booted into (a real OS-switch/reboot,
  explicitly out of scope for this zero-motion session).
- `reference/prtouch_v2.c` defines `PR_VERSION (307)`, which exactly matches `'version': 307`
  echoed in every live `debug_prtouch` MCU response — real corroboration that *the prtouch command
  layer specifically* is genuine. But that same file's header comment (`// Report on user
  interface buttons ... Copyright (C) 2018 Kevin O'Connor`) is verbatim upstream Klipper's
  unrelated `buttons.c` header — this file was made by editing an existing template, not a
  pristine Creality drop, and its provenance for anything beyond the prtouch command layer is
  unconfirmed.
- **Direct behavioral contradiction, not just a version mismatch**: `vendor/klipper/src/sched.c`
  implements `try_shutdown("Timer too close")` →
  `sched_try_shutdown()` (only guard: "not already shutting down") → `sched_shutdown()` →
  `irq_disable(); longjmp(shutdown_jmp, reason);` — an **immediate, unconditional hard shutdown on
  the very first call**. The real device instead printed five `Timer too close` `#output` lines
  across three full attempt cycles, with completely normal sensor reads, no-trigger detection, and
  recovery lifts in between each one, before a distinct `sentinel timer called` shutdown much
  later. If this exact `sched.c` governed the real firmware, the first `Timer too close` should
  have hard-stopped the MCU immediately. It did not. **This is concrete evidence that whatever
  scheduler code is actually running differs from this repo's `vendor/klipper/src/sched.c`.**
- A targeted web search found a Creality Wiki "Firmware Open Source" page for this exact model,
  but it is JS-rendered and yielded no extractable links via automated fetch. No further public
  Creality GPL source release was located from this session.

**Conclusion (superseded — see §7)**: this section originally stopped here, concluding that
static analysis had been exhausted pending either a genuine Creality GPL source release or live
MCU extraction. §7 below found that release. Kept for the historical record of what was
provable from the live device and this repo's own vendored source alone.

---

## 2. Incident timeline, with confidence labels

All times are Klipper `eventtime` (reactor-monotonic), read directly from
`klippy.log` (`Received`/`debug_prtouch`/`#output` lines are all in this same clock domain; the
one `Requested toolhead position at shutdown time 1748.877217` line is in the *separate*
`print_time` domain and must not be compared directly against `eventtime` — an error made and
corrected during this investigation).

| eventtime | Event | Confidence |
|---|---|---|
| 1743.594545 | `Received` `gcode/script` `Z_OFFSET_CALIBRATION` (id `1945934128`) — the one HTTP call made | FACT |
| 1744.366–367 | Attempt 1 armed: pres config echo, step-down echo `oid=5 dir=0 send_ms=10 step_cnt=200 step_us=1000 acc_ctl_cnt=50` | FACT |
| ~1744.4–1747.1 | `repairing pres samples, got 0/32` → `no pressure channel reported a trigger ... attempt 1/10` | FACT |
| 1747.152 | `Timer too close` (1st) | FACT |
| 1747.152–154 | disarm step/pres, recovery lift armed: `oid=5 dir=1 step_cnt=200 step_us=2500` | FACT |
| — | `repairing step samples, got 4/32` | FACT |
| ~1747.15 | `Timer too close` (2nd) | FACT |
| 1749.887 | Attempt 2 armed (down): identical params to attempt 1 | FACT |
| — | `no pressure channel reported a trigger ... attempt 2/10` | FACT |
| 1754.383 | `Timer too close` (3rd) | FACT |
| 1754.385 | recovery lift for attempt 2 armed | FACT |
| — | `Timer too close` (4th) | FACT |
| 1757.082 | Attempt 3 armed (down): identical params | FACT |
| — | `no pressure channel reported a trigger ... attempt 3/10` | FACT |
| 1761.591 | `Timer too close` (5th) | FACT |
| 1761.593 | recovery lift for attempt 3 armed — its own disarm/completion is never logged | FACT |
| 1748.808983 | `Received` `gcode/script` `Z_OFFSET_CALIBRATION` (id `1956010320`) — second, distinct request | FACT |
| shortly after 1761.593 | `Transition to shutdown state: MCU shutdown` → `MCU 'mcu' shutdown: sentinel timer called` | FACT |
| — | Motors reported by the operator as sounding abnormal during the test | FACT (direct observation) |
| — | `sched_shutdown`'s `longjmp` is immediate on first call, in the *source read*; real device tolerated 5 warnings before the real shutdown | FACT (of the source) / STRONG_INFERENCE (that the real firmware therefore differs — see §1) |
| — | The raw step-generation timing/cadence itself (not concurrency) is implicated, since attempt 1 alone produced 2 of the 5 warnings before the second RPC ever existed | STRONG_INFERENCE |
| — | Something specific to the third attempt's recovery-lift dispatch is where the firmware actually stalled long enough to trip the sentinel (~18s of the MCU's own 100ms heartbeat not running, per `sentinel_timer.waketime = periodic_timer.waketime + 0x80000000` at 120MHz — again only provably true of the *source read*, not confirmed for the real firmware) | HYPOTHESIS |
| — | Exact firmware-level mechanism that stalls the scheduler for that long | UNKNOWN — needs source correspondence (§1) or live instrumentation to resolve |
| — | Origin of the second RPC request | UNKNOWN as to *source*, but see §3 for what has been ruled out with evidence |

### On "0/32" and "4/32" sample-repair counts
`repairing pres samples, got 0/32` appears on every attempt's down-phase (no pressure trigger
ever arrived, consistent with a load cell that genuinely never triggered against open air — matches
the intentional design of this test). `repairing step samples, got 4/32` appears once, on attempt
1's *recovery lift* specifically (not its down-phase, and not on attempts 2 or 3's lifts, which show
`0/32`) — i.e. the MCU did report 4 real step samples for that one lift before the buffer needed
repair, while every other repair event reports zero. This is the single asymmetric data point in
the whole sequence. It temporally lines up with the first `Timer too close` overall (which also
occurs during attempt 1's transition into that same recovery lift). **STRONG_INFERENCE**: this
one partially-populated buffer marks the first point at which MCU response timing genuinely
degraded — consistent with, but not proof of, the recovery-lift path being where things start
going wrong. Not proven as causal; recorded as the most concrete anomaly available for any future
firmware-level investigation to explain first.

---

## 3. The second JSON-RPC request

Two distinct `Received gcode/script Z_OFFSET_CALIBRATION` lines exist in `klippy.log`'s
connection dump, with different ids, 5.2s apart (1743.59 / 1748.81) — this is a real, structural
fact, not a parsing artifact (Klippy's own retrospective 20-request dump lists both by their
genuine original timestamps).

**Ruled out, with evidence, as the source:**
- **Moonraker's own request layer duplicating the call.** Read directly from
  `/opt/moonraker/moonraker/components/klippy_connection.py`: `_request_standard()` creates
  exactly one `KlippyRequest` object (keyed by Python `id()`) and schedules exactly one write to
  Klippy per call (`self.event_loop.register_callback(self._write_request, base_request)`, once).
  `KlippyRequest.wait()`'s "pending" retry-logging path (the mechanism behind the unrelated
  `Request 'gcode/script' pending: 60.00 seconds` lines seen earlier and 20 minutes before this
  incident) re-awaits the *same* future via `asyncio.shield(self._fut)` — it structurally cannot
  create a second request. **This is proof, not inference: a single HTTP POST cannot produce two
  `Received` lines through this code path.**
- **Moonraker's own HTTP access log** (`application.py:log_request()`) shows exactly one relevant
  POST: `17:50:12,500 400 POST /printer/gcode/script?script=Z_OFFSET_CALIBRATION (127.0.0.1)
  20864.56ms` — the one call made during this investigation.
- **GuppyScreen's UI/wizard.** Its own application log
  (`/usr/data/nebulaos/printer_data/logs/guppyscreen.log`) shows a completely unrelated,
  already-finished `RecalibrationWizardPanel` session (stock BLTouch `TESTZ`-based calibration,
  *not* our custom `Z_OFFSET_CALIBRATION` command at all) ending at `17:30:32`, then **total
  silence** until a fresh process restart at `18:16:04` (consistent with GuppyScreen's connection
  being killed by the MCU shutdown and auto-restarting afterward). Zero log activity of any kind
  during the 17:43–17:50 incident window.
- **Mainsail.** Its websocket (`ID 1956252112`) closed at `17:47:22`, over a minute before this
  session's own curl call was even issued.
- **Full websocket enumeration** across the entire `moonraker.log`: only three connections ever
  opened — GuppyScreen's local one (open throughout, but proven silent above), Mainsail (closed
  before the test), and GuppyScreen's post-crash restart. No other client is visible.

**Not fully resolvable**: Moonraker does not access-log individual websocket JSON-RPC method
calls (only HTTP requests go through `log_request()`), so a raw, un-instrumented websocket call
cannot be 100% excluded on log evidence alone. However every specific, checkable candidate has
been checked and ruled out with positive evidence (not merely left untested), and GuppyScreen —
the only client with a connection open throughout — is independently proven silent by its own
application log.

**Causally irrelevant regardless of origin**: the first `Timer too close` (1747.15) predates the
second request (1748.81) by 1.6s, and the real shutdown occurs roughly 13s *after* the second
request, following a full additional clean attempt cycle (attempt 3) with no visible interference.
Whatever sent the second request, it did not cause this incident.

---

## 4. Reference material confidence reassessment

| Item | Classification | Basis |
|---|---|---|
| `reference/prtouch_v2.c` — prtouch command/protocol layer (`PR_VERSION`, field encoding) | CONSISTENT_BUT_UNPROVEN, with one strong corroborating data point | `PR_VERSION=307` matches the live device's own echoed protocol version exactly |
| `reference/prtouch_v2.c` — file provenance/header | RECONSTRUCTED_ONLY | Header is verbatim unrelated upstream Klipper `buttons.c` copyright text |
| `reference/prtouch_v2_wrapper.py` — command cadence (wait-then-disarm, timeout formula) | CONSISTENT_BUT_UNPROVEN | This port's `probe_timeout_seconds()` (`distance/speed + 2.0s`) matches the wrapper's own `down_min_z/use_tri_z_down_spd + 2` formula exactly; structurally equivalent wait/disarm sequencing |
| `vendor/klipper/src/sched.c` — scheduler/timer/shutdown semantics | CONTRADICTED | See §1's `try_shutdown` behavioral contradiction |
| This port's own `prtouch_probe.py`/`prtouch_mcu.py` host-side orchestration | CONFIRMED_BY_REAL_FIRMWARE (structurally) | The live incident's own log output (attempt counters, repair-sample counts, disarm/rearm sequencing) matches exactly what this port's source predicts it would send/log — the port's *host-side* behavior is doing what it was written to do; the *firmware's* response to that behavior is what remains unproven |

---

## 5. Non-reentrancy guard (added this session)

Independent of the unresolved timer root cause: `klippy_extras/z_compensate.py`'s
`cmd_z_offset_calibration()` now rejects a second invocation immediately (`command_error:
"Z_OFFSET_CALIBRATION: a calibration is already in progress"`) if one is already running, checked
and set with no yield in between — safe without a lock given Klipper's single-threaded/cooperative
reactor (whichever invocation's handler runs first always sets `"running"` before it can yield via
`reactor.pause()`/`wait_moves()`, so any second invocation is guaranteed to observe the busy state
already set, however it was triggered). The busy state clears on every exit path (success →
`"complete"`, any exception → `"error"`), matching the existing status-contract behavior; four new
regression tests in `test_z_compensate.py::ReentrancyGuardTest` cover rejection-while-running, that
a rejected call doesn't bump `calibration_id` or disturb the in-progress status, and that the guard
correctly clears after both success and failure to allow legitimate sequential reuse.

This is explicitly **not** presented as a fix for the timer incident — the second request was
shown in §3 to be causally irrelevant to it. It closes a real, independently-justified gap (no
motion-capable command should ever be able to overlap another instance of itself on this MCU's raw
step channel) regardless of what actually caused the shutdown.

Scope note: `CRTENSE_NOZZLE_CLEAR`/`NOZZLE_CLEAR` (`cmd_nozzle_clear`) also call `touch_probe()` on
the same shared `PrtouchProbe` instance and carry the same theoretical class of risk, but were not
part of what was asked for here and were left unguarded — flagged for a future, explicitly scoped
pass if wanted.

`UPSTREAM_KLIPPER_CORE_DIFFS: NONE` — this change is entirely within
`klippy_extras/z_compensate.py` (a NebulaOS-owned extra) and its own test file.

---

## 6. Next motion diagnostic — prepared, NOT executed

Do not run any of the following without a fresh, explicit go-ahead.

Given §1–§4: the retry/recovery *cadence itself* is not yet cleared as a factor (attempt 1 alone,
before any retry loop had run twice, already produced two `Timer too close` warnings), so the
right next step is not another `Z_OFFSET_CALIBRATION` run — it's the smallest possible **isolated,
single, non-probing raw step dispatch**, to learn whether even one lone `start_step_prtouch` call
produces `Timer too close` outside of any retry/disarm/rearm cadence. `SAFE_MOVE_Z` already exists,
is genuinely non-probing (no pressure arm, no trigger check, no retry loop — confirmed by direct
code reading earlier this investigation), and is exactly this shape. No new code is needed; this is
a usage plan, not an implementation task.

**Step A — single isolated UP move** (away from the bed; the strictly safer direction):
```
SAFE_MOVE_Z DIR=1 DIS=1 SPD=1
```
- 1mm, at a deliberately slow 1mm/s (well under the ~5mm/s used in the incident) — the smallest,
  slowest raw move this command supports.
- Capture `klippy.log` immediately before and after for any `#output: Timer too close` or shutdown
  transition, and confirm `webhooks.state` stays `"ready"` throughout.
- Confirm via `objects/query` that `toolhead` position and MCU stats look sane afterward.
- **Do not send Step B in the same session/back-to-back** — a deliberate pause and explicit
  human confirmation between the two, specifically because the incident's own cadence (rapid
  disarm-then-immediately-rearm) is one of the still-open hypotheses.

**Step B — single isolated DOWN move**, only after Step A is confirmed clean and only with fresh
authorization:
```
SAFE_MOVE_Z DIR=0 DIS=1 SPD=1
```
- Same 1mm/1mm/s parameters. Since `SAFE_MOVE_Z` has no pressure arm or trigger detection at all,
  this cannot be mistaken for contact detection — it is purely a raw-step-timing probe.

**Explicitly not part of this plan**: no `Z_OFFSET_CALIBRATION`, no `NOZZLE_CLEAR`, no retries, no
`G28`/homing, no persistence, no chaining the two steps together. If Step A alone reproduces
`Timer too close`, that would be strong evidence the issue is inherent to any raw step dispatch on
this real MCU, independent of cadence — a materially different conclusion than if only the
chained/retried case (as in the actual incident) reproduces it.

**NEXT_MOVEMENT_DIAGNOSTIC_EXECUTED: NO** (by design — this document only specifies the plan).

### 6a. Exact static proof (2026-08-10, second session, after the fix)

Ran this device's real, live `[stepper_z]` values (`microsteps: 16`, `rotation_distance: 8`,
200 full steps/rotation → `mm_per_step = 8/(200*16) = 0.0025`) through the real, unmodified
`prtouch_units.py` functions (no fake/test values) to compute exactly what
`SAFE_MOVE_Z DIR=1 DIS=1 SPD=1` will send, now that the guard/settle fix is in place:

```
start_step_prtouch oid=<step_oid> dir=1 send_ms=10 step_cnt=400 step_us=2500 acc_ctl_cnt=200 \
    low_spd_nul=5 send_step_duty=16 auto_rtn=0
collect_step_samples timeout = 6.0s (1.0s expected physical move + 5.0s margin)
settle after disarm = 0.01s (tri_send_ms/1000, the new fix's own yield)
```

Notably, `step_cnt=400, step_us=2500` for this 1mm/1mm/s move is the same order of magnitude as
the incident's own recovery-lift arms (`step_cnt=200, step_us=2500` for its 0.5mm lifts) —
this diagnostic exercises genuinely comparable timing to what actually happened, not an
artificially different regime. With the fix in place, this single call now also exercises
`_own_raw_operation` (rejects any overlapping call) and `_settle_after_disarm` (yields 10ms
after the disarm before returning) — both proven correct offline in
`test_prtouch_raw_op_guard.py`.

**NEXT_MOVEMENT_DIAGNOSTIC_EXECUTED: NO.**

---

## 7. Official Creality source located — the root cause is now proven, not inferred

2026-08-10, second session. `gh repo view` confirmed two real, accessible, official Creality
repositories: `CrealityOfficial/Ender-3_V3_KE_Klipper` (`git clone from
https://github.com/Klipper3d/klipper/`, tag `V1.1.0.12` — the exact firmware version this repo's
own `FIRMWARE.md` already cites for this device via `/etc/ota_info`) and
`CrealityOfficial/Ender-3_V3_KE_Annex`. Cloned both. History is only 4 commits total
(`9bdde73 Initial commit`, `a63fb1a update for open source`, plus two dependabot bumps) — a
one-time GPL-compliance snapshot, not a live mirror of Creality's internal build system, so
`38d96adc` (the running firmware's own embedded hash) still does not appear here either. That
specific commit remains unrecoverable — but this is now a materially different, far stronger
kind of evidence than §1's `vendor/klipper`: **Creality's own official source**, not a
third-party fork, containing the *exact same custom PRTouch subsystem* (confirmed below), for
the same board family.

### What's actually there
- `src/prtouch_v2_cm23.o`, `src/prtouch_v2_cm3.o`, `src/prtouch_v2_cm4.o` — real, precompiled,
  **not stripped** ARM ELF relocatable objects (`file`: "ELF 32-bit LSB relocatable, ARM, EABI5
  ... with debug_info, not stripped"), one per Cortex-M variant (M23/M3/M4).
- `src/gd32/` — a real, buildable GD32 target (`Kconfig`, `Makefile`), with
  `config MACH_GD32F303XE` present and selecting `MACH_GD32F30X_HD`/`MACH_GD32F30X` — this
  device's own live-identified chip (`gd32f303xe`) is a named, first-class target here, and the
  GD32F30x family's actual core is Cortex-M4, making `prtouch_v2_cm4.o` the applicable object
  (the Kconfig confirms the chip target; which of the three `.o` files gets linked for it is
  standard Cortex-core selection, not something this session needed to trace further given what
  was found below applies identically to all three).
- `config/F005/factory_printer.cfg` and `config/F005/printer.cfg` — this exact board, with the
  same `[mcu] serial:/dev/ttyS1 baud:230400` as the live device.
- `src/prtouch_v2_compile.c` — just `DECL_COMMAND` declarations + a thin `sendf_info()`; the
  real command implementations live in the precompiled `.o` files, linked via
  `src-$(CONFIG_HAVE_GPIO) += ... prtouch_v2_compile.c` in `src/Makefile`.

### The proof: Creality's own `sched_add_timer()` is NOT stock Klipper's

Diffing this official `src/sched.c` against `vendor/klipper/src/sched.c` (this repo's own
vendored pellcorp/klipper copy, the one §1 already showed is unproven for this device):

```c
 sched_add_timer(struct timer *add)
 {
     uint32_t waketime = add->waketime;
+	uint8_t flags = 0;              // (Creality's own real code, not upstream's)
     irqstatus_t flag = irq_save();
     struct timer *tl = SchedStatus.timer_list;
     if (unlikely(timer_is_before(waketime, tl->waketime))) {
         if (timer_is_before(waketime, timer_read_time()))
+		{
+            //try_shutdown("Timer too close");     <-- COMMENTED OUT in Creality's real source
+			flags = 1;
+			waketime = timer_read_time() + timer_from_us(2);   <-- silently clamped instead
+			add->waketime = waketime;
+		}
         ...
     }
     irq_restore(flag);
+    if(flags)
+	{			
+		output("Timer too close");      <-- plain debug print, NOT a shutdown notification
+		flags = 0;
+	}
 }
```

(diff direction: `+` lines are what upstream/pellcorp's `vendor/klipper/src/sched.c` has; the
lines actually present in Creality's own file are what's left when those are removed — i.e.
Creality's real `sched_add_timer()` has the commented-out `try_shutdown` and the clamp/print
logic, not the stock immediate-`longjmp` behavior.)

**This exactly and completely explains the incident's observed behavior.** §1 found a real
behavioral contradiction: stock `sched.c`'s `try_shutdown` → `sched_shutdown` → `irq_disable();
longjmp(...)` is unconditional and immediate on the very first call, yet the live device
tolerated five separate `Timer too close` warnings with fully normal operation in between each
one before a distinct, later shutdown. Creality's own real firmware source shows exactly why:
**they deliberately commented out the hard-shutdown call and replaced it with a silent
now+2μs clamp and a plain `output()` debug print** — which is precisely what
`mcu 'mcu': #output: Timer too close` in the live log is (a plain debug line, never a shutdown
notification at all, confirmed by the wire-format difference: shutdown reports use `is_shutdown
static_string_id=%hu`, not `#output:`).

A second, independent Creality customization was found in the same diff: the core idle loop
(`sched_main`'s inner `while` in `run_tasks`) has upstream's `irq_wait()` (sleep until an
interrupt) replaced with:
```c
do {
    asm volatile("cpsie i" ::: "memory");
    extern void prtouch_task(void);
    prtouch_task();
} while (SchedStatus.tasks_status != TS_REQUESTED);
```
i.e. whenever the MCU has no pending Klipper task, it **busy-polls a PRTouch-specific function
instead of sleeping**. Disassembling `prtouch_task` in `prtouch_v2_cm4.o`
(`arm-none-eabi-objdump -dr -j .text.prtouch_task`) shows it is a two-instruction dispatcher:
```
00000000 <prtouch_task>:
   0:	b508      	push	{r3, lr}
   2:	f7ff fffe 	bl	0 <prtouch_task>
			2: R_ARM_THM_CALL	prtouch_pres_task
   6:	e8bd 4008 	ldmia.w	sp!, {r3, lr}
   a:	f7ff bffe 	b.w	0 <prtouch_task>
			a: R_ARM_THM_JUMP24	prtouch_step_task
```
— it calls `prtouch_pres_task` then tail-calls `prtouch_step_task`, **the exact two function
names already documented in `reference/prtouch_v2.c`** (its own `prtouch_step_task()`, gated by
`check_delay(&send_dly, send_ms/1000)`, is what paces buffered-sample sends). This is real,
symbol-level, disassembly-confirmed proof that `reference/prtouch_v2.c`'s documented structure
matches Creality's actual compiled firmware, upgrading its confidence rating (see §4) from
"consistent but unproven" to **CONFIRMED_BY_REAL_FIRMWARE at the function/call-graph level**
(not byte-exact instruction correspondence — this session did not attempt full decompilation of
the timer-scheduling internals themselves, judged disproportionate once the `sched.c`-level
proof above was in hand).

### Updated §1 answer

`RUNNING_MCU_SOURCE_FOUND`: a strong, official, function-level match — not the exact `38d96adc`
build (unrecoverable without Creality's internal build history), but Creality's own published
source for this exact board family and firmware line, showing the precise customization that
explains the observed behavior. This is the strongest evidence obtainable without live MCU
extraction, and it was sufficient to reach a real conclusion.

---

## 8. Could the raw-step architecture be replaced instead of patched?

Evaluated per the mission brief's explicit instruction not to preserve Creality's raw-step
primitive "merely for compatibility." Conclusion: **no, not without upstream Klipper
modifications, which are out of scope.** The pressure (load-cell) sampling and the raw step
pulse generation are fused together inside the MCU firmware itself — `prtouch_event()` (the
per-pulse timer callback disassembled/read via `reference/prtouch_v2.c` and confirmed present
by symbol in the official `.o` files) is the single function that both toggles the step GPIO
*and* checks `read_swap_sta()` (the pressure-trigger latch) on every pulse, recording
`send_tri_time` the instant a trigger is detected. Upstream Klipper's own stepper/homing/probe
abstractions (`trsync`, `mcu_endstop`, the trapq-based move queue) have no path to observe this
MCU's pressure channel at all — there is no endstop pin, ADC reading, or any other primitive
upstream Klipper knows how to homing-query that carries this signal. Reimplementing the touch
detection on top of upstream's motion APIs would mean either (a) inventing a new MCU-side
protocol from scratch (modifying the firmware — explicitly excluded) or (b) polling the
pressure sensor from the host during a normal queued move, which cannot achieve trigger-time
resolution anywhere close to the MCU's own per-pulse check and would reintroduce exactly the
kind of unbounded-blind-travel risk the 2026-08-06/09 fixes already closed. The raw-step
primitive is the *only* foundation available for detecting a load-cell trigger without
firmware changes. This session's fix therefore works within that architecture (bounding and
pacing its use) rather than replacing it.

---

## 9. Root cause — final synthesis

| Finding | Confidence |
|---|---|
| The live device's `Timer too close` warnings are plain debug prints, not shutdown attempts, because Creality's own real firmware has `try_shutdown("Timer too close")` commented out | **PROVEN** (official Creality source, `CrealityOfficial/Ender-3_V3_KE_Klipper` tag `V1.1.0.12`, `src/sched.c`) |
| A "too close" timer gets silently clamped to `now + 2µs` and rescheduled, rather than rejected | **PROVEN** (same source) |
| The real, unmodified `sentinel_timer`/`sentinel_event` mechanism (§2) is still present and still capable of independently firing after ~17.9s of the periodic 100ms heartbeat not running | **PROVEN** (same source — this part of `sched.c` is unmodified from stock) |
| Repeated back-to-back disarm-then-immediate-rearm cycling (the incident's own cadence — zero host-side yield between a disarm and the next arm) is what drove the clamp-and-warn path 5 times, and plausibly congested the MCU's timer dispatch enough to eventually starve the periodic timer and trip the still-present sentinel | **STRONG_EVIDENCE** — directly explains every observed timestamp and warning count; the exact congestion mechanism inside `sched_add_timer`'s repeated near-immediate rescheduling was not separately disassembled/simulated, so this final causal link (clamp cascade → sentinel starvation) is strong inference from proven mechanics, not a byte-level proof |
| `reference/prtouch_v2.c`'s documented `prtouch_step_task`/`prtouch_pres_task` structure matches Creality's real compiled firmware | **PROVEN** (disassembly + symbol match, `prtouch_v2_cm4.o`) |
| The second, unexplained `Z_OFFSET_CALIBRATION` JSON-RPC request caused or contributed to the incident | **DISPROVEN** (§2/§3 — the first warning predates it by 1.6s, and the shutdown follows a full additional clean attempt cycle after it) |
| A full replacement of the raw-step architecture with upstream Klipper motion APIs is possible without firmware changes | **DISPROVEN** (§8 — the pressure/step coupling is inside the MCU firmware itself) |

**Likely root cause**: the live incident's own command cadence — disarming a raw step operation
and immediately rearming the next one with no host-side yield — repeatedly triggers Creality's
own real (not stock-Klipper) timer-clamping path in rapid succession, and this repeated
near-immediate rescheduling pressure is what eventually stalled the MCU's timer dispatch loop
long enough to trip the always-present, unmodified sentinel watchdog. This is now grounded in
Creality's own official source for the exact behavior that diverged from what stock
`vendor/klipper/src/sched.c` would predict, not merely inferred from host-side log timing.

**Fix implemented, directly targeting this mechanism**: `_settle_after_disarm()` (§10) inserts
a real host-side yield after every disarm, before the next arm — breaking the exact back-to-back
cadence that drives repeated clamp events on Creality's real firmware.

---

## 10. Fix implemented (2026-08-10, second session)

Real deployment target correction first: this document's own §1–§6 were written against
`ke-mainline-klipper`'s `klippy_extras/`, but the live device's actual running
`prtouch_probe.py`/`z_compensate.py` (verified by line count and distinctive symbol/config-key
match over SSH) matches `/home/tim/Documents/NebulaOS-klipper-loadcell`
(`coreflake1/NebulaOS-klipper`, `master`, commit `4510ee65`) — a separately-cloned, more advanced
checkout with an already-shipped safety-hardening layer (`PrtouchProbeSafetyError`,
`max_probe_travel_mm`, `max_probe_duration_s`, baseline sanity guards) that `ke-mainline-klipper`
does not yet have. The core `_touch_probe()` MCU dispatch sequence is identical between the two
trees (confirmed by direct diff), so this document's incident timeline (built from live log
data, not from source reading) remains valid regardless. The fix below was implemented in
**both** trees — primarily in `NebulaOS-klipper-loadcell` as the real ship target, mirrored into
`ke-mainline-klipper`'s simpler (pre-hardening) file structure.

### Shared raw-operation ownership guard
`PrtouchProbe._own_raw_operation()` (a context manager) wraps the two PUBLIC raw-motion entry
points, `touch_probe()` and `safe_move_z()` — only one may be active at a time, across both,
however a second call is triggered. `_fail()`'s own internal safety lift was refactored to call
a new private `_raw_move()` helper directly (bypassing the public `safe_move_z()` guard
entirely), since it is legitimately nested inside whichever public operation already holds the
guard — without this refactor, `_fail()`'s own cleanup would incorrectly raise
`PrtouchProbeSafetyError` against itself. Checked-and-set with no yield in between, so this is
race-free under Klipper's single-threaded/cooperative reactor without needing a lock.
`z_compensate.py`'s own `cmd_z_offset_calibration()` guard (from the first session, already
committed) is a second, higher-level, complementary guard protecting the whole multi-step
calibration sequence as one logical unit, on top of this lower-level one.

### Evidence-grounded settle after every disarm
`PrtouchProbe._settle_after_disarm()` yields (`reactor.pause()`) for at least one
`tri_send_ms` tick — the protocol's own declared pacing granularity for this exact channel,
not an invented constant — after every disarm, before the next arm. Wired into all four
disarm sites: `_raw_move` (covers `safe_move_z` and `_fail`'s own lift), `_touch_probe`'s own
down-arm disarm, and `_raw_lift` (covers both `_lift_after_down` and
`_recover_after_no_trigger`'s no-trigger recovery). Overridable via `raw_op_settle_s` once real
hardware timing margins are measured; `None` (default) derives from `tri_send_ms`.

### Instrumentation
Every arm/disarm now logs an operation id (incrementing per `touch_probe`/`safe_move_z` call),
direction, `step_cnt`/`step_us`/`acc_ctl_cnt`/`send_ms`, and start/end markers — host-side only,
no additional MCU traffic, low overhead (plain `logging.info` calls matching this codebase's
existing conventions).

### Tests
`test_prtouch_raw_op_guard.py` (new, both repos, 10 tests): shared-ownership rejection across
both entry points in both directions, guard release after success/exception,
`_fail()`'s internal lift not blocked by its own guard (regression proof for the refactor),
settle called exactly once per disarm across a full no-trigger retry sequence (directly
reproduces the incident's own attempt/disarm/rearm pattern against a fake MCU and asserts the
fix is wired into every transition), settle duration default/override, and settle actually
advancing the (virtual) reactor clock. `test_z_compensate_reentrancy_guard.py` (new, in
`NebulaOS-klipper-loadcell` only — `ke-mainline-klipper`'s equivalent guard/tests were added in
the first session as `ReentrancyGuardTest` inside its existing `test_z_compensate.py`): 4 tests
for the calibration-level guard. All pre-existing tests continue to pass in both repos with zero
regressions (162 total in `NebulaOS-klipper-loadcell`, 160 in `ke-mainline-klipper`).

`UPSTREAM_KLIPPER_CORE_DIFFS: NONE` in both repos — every change is confined to
`klippy_extras/`/`klippy/extras/` (NebulaOS-owned) and its own tests.
