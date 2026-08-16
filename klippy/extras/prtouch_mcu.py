# prtouch_v2 MCU protocol - oid/config setup, raw command send, response buffering
#
# This is the one file that actually speaks the PRTouch wire protocol. Everything else in
# this module set (prtouch_probe.py's state machine, prtouch_calibration.py's math) only ever
# sees the plain-Python results this file hands back - no other file constructs a raw MCU
# command or parses a raw MCU response. See docs/PRTOUCH_INTERNALS.md for how this fits
# into the wider command -> MCU -> Z-result pipeline.
#
# Clean-room rewrite of the MCU-facing half of Creality's prtouch_v2_wrapper.py (GPLv3-
# licensed Creality source, not included in this tree - see
# docs/prtouch_timer_incident_forensics.md sec 7 for how its protocol shapes were
# independently confirmed against Creality's own officially published source for this board)
# against the *existing, unreflashed* toolhead firmware - same wire protocol, same standard
# Klipper host APIs (create_oid/add_config_cmd/lookup_command/register_response) already
# proven on this device by hx711s.py. Nothing on the MCU itself was changed; this file only
# needs to send the same commands in the same shapes the stock wrapper always did.
#
# Field-name note: the wire format itself (confirmed against Creality's own official source
# and against captured live traffic) uses several inconsistent/misspelled names verbatim -
# e.g. "resault_write_swap_prtouch" and "resault_manual_get_pres" (missing/extra letters,
# not this file's typo) - these are preserved exactly as Creality's firmware expects them,
# since the MCU only recognizes its own literal command/response strings.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

from . import prtouch_units as units

#: Fixed size of both the step-sample and pressure-sample response ring buffers the MCU
#: fills during one armed operation. Confirmed from the wire protocol (both
#: result_run_step_prtouch's 4-samples-per-message chunking and
#: result_run_pres_prtouch's 2-samples-per-message chunking divide evenly into this), not
#: independently configurable - a probe/move that finishes before the buffer fills simply
#: returns fewer than MAX_BUF_LEN samples (a real trigger stops the MCU early), which is a
#: normal, expected outcome, not a protocol error.
MAX_BUF_LEN = 32

#: Maximum number of independent pressure-sensor channels this protocol supports (one per
#: bed corner on Creality's original 4-corner load-cell layout). This printer only wires
#: pres_cnt=1 in practice, but the wire format's ch0..ch3 fields and the 4-bit tri_chs
#: trigger bitmask are always this wide regardless of how many channels are physically
#: connected.
MAX_PRES_CNT = 4

#: How often (seconds) collect_step_samples()/collect_pres_samples() re-check whether the
#: response buffer has filled, while waiting. This is a host-side polling granularity, not a
#: protocol value - it only trades a little host CPU/responsiveness for a lot of syscall
#: overhead if set too small, and adds up to this much extra latency in the worst case if set
#: too large. Not performance-critical at 10ms; not derived from any MCU timing.
POLL_INTERVAL = 0.010


class PrtouchProtocolError(Exception):
    pass


class PrtouchMCU:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        ppins = self.printer.lookup_object('pins')

        # use_adc: which physical sensor family is wired up. False (default, this printer's
        # real config) means a strain-gauge/HX711-style load cell read via a clk/sdo bit-bang
        # pair (pres%d_clk_pins/pres%d_sdo_pins below); True means an ADC/piezo sensor read
        # via a single analog pin (pres%d_adc_pins). This selects both which config keys are
        # required AND which calibration path runs (prtouch_calibration.filter_pressure_series
        # skips the high-pass stage entirely for ADC sensors) - it is not just a wiring detail.
        self.use_adc = config.getboolean('use_adc', default=False)
        # pres_cnt: how many pressure channels are actually wired (this printer uses 1, even
        # though the protocol supports up to MAX_PRES_CNT=4 - Creality's original hardware
        # supported up to 4 load cells, one per bed corner).
        self.pres_cnt = config.getint('pres_cnt', 1, minval=1, maxval=MAX_PRES_CNT)
        # sys_time_duty: sent to the MCU (config_step_prtouch/config_pres_prtouch's own
        # sys_time_duty field, scaled x100000 by duty_fraction_to_scaled_units) as a small
        # fraction, default 0.001 (0.1%). Field name and wire encoding are confirmed; its
        # precise firmware-internal effect is not - name suggests it governs how much of the
        # MCU's own time budget this subsystem's background polling is allowed to consume,
        # but treat this as reverse-engineered/uncertain, not confirmed. Left at the stock
        # default everywhere in this codebase.
        self.sys_time_duty = config.getfloat('sys_time_duty', default=0.001,
                                              minval=0.00001, maxval=0.010)

        step_swap_pin = config.get('step_swap_pin')
        pres_swap_pin = config.get('pres_swap_pin')
        step_swap = ppins.parse_pin(step_swap_pin, True, True)
        pres_swap = ppins.parse_pin(pres_swap_pin, True, True)
        self.step_mcu = step_swap['chip']
        self.pres_mcu = pres_swap['chip']
        self._step_swap_pin_name = step_swap['pin']
        self._pres_swap_pin_name = pres_swap['pin']

        self.is_corexz = config.getsection('printer').get('kinematics', '') == 'corexz'
        self._z_step_pins = []
        self._z_dir_pins = []
        for name in ('stepper_z', 'stepper_x' if self.is_corexz else 'stepper_z1',
                     'stepper_z2', 'stepper_z3'):
            if config.has_section(name):
                sec = config.getsection(name)
                self._z_step_pins.append(sec.get('step_pin'))
                self._z_dir_pins.append(sec.get('dir_pin'))
        if not self._z_step_pins:
            raise config.error("prtouch_mcu: no stepper_z section found")

        self._pres_clk_pins = []
        self._pres_sdo_pins = []
        self._pres_adc_pins = []
        for i in range(self.pres_cnt):
            if self.use_adc:
                self._pres_adc_pins.append(config.get('pres%d_adc_pins' % i))
            else:
                self._pres_clk_pins.append(config.get('pres%d_clk_pins' % i))
                self._pres_sdo_pins.append(config.get('pres%d_sdo_pins' % i))

        self.step_oid = self.step_mcu.create_oid()
        self.pres_oid = self.pres_mcu.create_oid()
        self.step_mcu.register_config_callback(self._build_step_config)
        self.pres_mcu.register_config_callback(self._build_pres_config)

        self.step_res = []
        self.pres_res = []
        self.step_tri_time = 0.
        self.pres_tri_time = 0.
        self.pres_tri_chs = 0
        self.pres_buf_cnt = 0

        self.read_swap_prtouch_cmd = None
        self.start_step_prtouch_cmd = None
        self.manual_get_steps_cmd = None
        self.write_swap_prtouch_cmd = None
        self.read_pres_prtouch_cmd = None
        self.start_pres_prtouch_cmd = None
        self.deal_avgs_prtouch_cmd = None
        self.manual_get_pres_cmd = None

        self.step_mcu.register_response(self._handle_result_run_step_prtouch,
                                         "result_run_step_prtouch", self.step_oid)
        self.pres_mcu.register_response(self._handle_result_run_pres_prtouch,
                                         "result_run_pres_prtouch", self.pres_oid)
        self.pres_mcu.register_response(self._handle_result_read_pres_prtouch,
                                         "result_read_pres_prtouch", self.pres_oid)

    def _build_step_config(self):
        ppins = self.printer.lookup_object('pins')
        self.step_mcu.add_config_cmd(
            'config_step_prtouch oid=%d step_cnt=%d swap_pin=%s sys_time_duty=%u' % (
                self.step_oid, len(self._z_step_pins), self._step_swap_pin_name,
                units.duty_fraction_to_scaled_units(self.sys_time_duty)))
        for i in range(len(self._z_step_pins)):
            step_par = ppins.parse_pin(self._z_step_pins[i], True, True)
            dir_par = ppins.parse_pin(self._z_dir_pins[i], True, True)
            dir_invert = dir_par['invert']
            if self.is_corexz and i == 0:
                dir_invert = not dir_invert
            self.step_mcu.add_config_cmd(
                'add_step_prtouch oid=%d index=%d dir_pin=%s step_pin=%s '
                'dir_invert=%d step_invert=%d' % (
                    self.step_oid, i, dir_par['pin'], step_par['pin'],
                    dir_invert, step_par['invert']))
        self.read_swap_prtouch_cmd = self.step_mcu.lookup_query_command(
            'read_swap_prtouch oid=%c', 'result_read_swap_prtouch oid=%c sta=%c',
            oid=self.step_oid)
        self.start_step_prtouch_cmd = self.step_mcu.lookup_command(
            'start_step_prtouch oid=%c dir=%c send_ms=%c step_cnt=%u step_us=%u '
            'acc_ctl_cnt=%u low_spd_nul=%c send_step_duty=%c auto_rtn=%c', cq=None)
        self.manual_get_steps_cmd = self.step_mcu.lookup_query_command(
            'manual_get_steps oid=%c index=%c',
            'result_manual_get_steps oid=%c index=%c tri_time=%u '
            'tick0=%u tick1=%u tick2=%u tick3=%u step0=%u step1=%u step2=%u step3=%u',
            oid=self.step_oid)

    def _build_pres_config(self):
        ppins = self.printer.lookup_object('pins')
        self.pres_mcu.add_config_cmd(
            'config_pres_prtouch oid=%d use_adc=%d pres_cnt=%d swap_pin=%s sys_time_duty=%u' % (
                self.pres_oid, self.use_adc, self.pres_cnt, self._pres_swap_pin_name,
                units.duty_fraction_to_scaled_units(self.sys_time_duty)))
        for i in range(self.pres_cnt):
            if self.use_adc:
                adc_par = ppins.parse_pin(self._pres_adc_pins[i], True, True)
                clk_pin = sdo_pin = adc_par['pin']
            else:
                clk_par = ppins.parse_pin(self._pres_clk_pins[i], True, True)
                sdo_par = ppins.parse_pin(self._pres_sdo_pins[i], True, True)
                clk_pin, sdo_pin = clk_par['pin'], sdo_par['pin']
            self.pres_mcu.add_config_cmd(
                'add_pres_prtouch oid=%d index=%d clk_pin=%s sda_pin=%s' % (
                    self.pres_oid, i, clk_pin, sdo_pin))
        self.write_swap_prtouch_cmd = self.pres_mcu.lookup_query_command(
            'write_swap_prtouch oid=%c sta=%c', 'resault_write_swap_prtouch oid=%c',
            oid=self.pres_oid)
        self.read_pres_prtouch_cmd = self.pres_mcu.lookup_command(
            'read_pres_prtouch oid=%c acq_ms=%u cnt=%u', cq=None)
        self.start_pres_prtouch_cmd = self.pres_mcu.lookup_command(
            'start_pres_prtouch oid=%c tri_dir=%c acq_ms=%c send_ms=%c need_cnt=%c '
            'tri_hftr_cut=%u tri_lftr_k1=%u min_hold=%u max_hold=%u', cq=None)
        self.deal_avgs_prtouch_cmd = self.pres_mcu.lookup_query_command(
            'deal_avgs_prtouch oid=%c base_cnt=%c',
            'result_deal_avgs_prtouch oid=%c ch0=%i ch1=%i ch2=%i ch3=%i', oid=self.pres_oid)
        self.manual_get_pres_cmd = self.pres_mcu.lookup_query_command(
            'manual_get_pres oid=%c index=%c',
            'resault_manual_get_pres oid=%c index=%c tri_time=%u tri_chs=%c buf_cnt=%u '
            'tick_0=%u ch0_0=%i ch1_0=%i ch2_0=%i ch3_0=%i '
            'tick_1=%u ch0_1=%i ch1_1=%i ch2_1=%i ch3_1=%i', oid=self.pres_oid)

    # -- async response handlers --------------------------------------------------

    def _handle_result_run_step_prtouch(self, params):
        self.step_tri_time = units.mcu_ticks_to_seconds(params['tri_time'])
        for i in range(4):
            self.step_res.append({
                'tick': units.mcu_ticks_to_seconds(params['tick%d' % i]),
                'step': params['step%d' % i],
                'index': params['index'],
            })

    def _handle_result_run_pres_prtouch(self, params):
        self.pres_tri_time = units.mcu_ticks_to_seconds(params['tri_time'])
        self.pres_tri_chs = params['tri_chs']
        self.pres_buf_cnt = params['buf_cnt']
        for i in range(2):
            self.pres_res.append({
                'tick': units.mcu_ticks_to_seconds(params['tick_%d' % i]),
                'ch0': params['ch0_%d' % i], 'ch1': params['ch1_%d' % i],
                'ch2': params['ch2_%d' % i], 'ch3': params['ch3_%d' % i],
                'index': params['index'],
            })

    def _handle_result_read_pres_prtouch(self, params):
        self.pres_res.append(params)

    # -- public API -----------------------------------------------------------

    def reset_buffers(self):
        self.step_res = []
        self.pres_res = []

    def start_step(self, direction, step_cnt, step_us, acc_ctl_cnt, send_ms=10,
                   low_spd_nul=5, send_step_duty=16, auto_rtn=0):
        """ARM the raw step-pulse channel: the MCU generates step_cnt pulses on the Z stepper
        directly (bypassing Klipper's normal trapq/step-compression pipeline entirely), one
        step_us microseconds apart, ramping speed over the first/last acc_ctl_cnt pulses.
        This is the primitive every real motion in this module set (touch_probe's descent,
        safe_move_z, the recovery/settle lifts) is built from.

        start_step_prtouch wire fields, in order (confirmed from the wire format and cross-
        checked against Creality's own officially published source for this board - see
        docs/prtouch_timer_incident_forensics.md sec 7):
          oid           - this channel's object id (create_oid(), fixed per printer).
          dir            - 1 = up (away from bed), 0 = down (toward bed). Confirmed.
          send_ms        - how often (ms) the MCU sends a buffered-sample chunk back to the
                            host while this operation runs; ALSO doubles as the dedicated
                            stop sentinel when set to exactly 0 (see stop_step()). Also the
                            value this module reuses as its own settle-after-disarm pacing
                            (PrtouchProbe._raw_op_settle_s) since it's the one MCU-declared
                            timing constant on this exact channel.
          step_cnt        - total pulse count for this move. distance_mm / mm_per_step,
                            truncated (prtouch_units.distance_mm_to_step_count). Must never be
                            0 through this method - see the guard below.
          step_us         - per-pulse period in microseconds - i.e. speed, expressed as time
                            between pulses rather than mm/s (prtouch_units.
                            step_count_to_step_us derives this from distance/speed/step_cnt).
                            Smaller = faster.
          acc_ctl_cnt     - how many of the leading/trailing pulses ramp speed up/down rather
                            than running at the full step_us rate immediately - an
                            acceleration window expressed in step counts, not mm or time
                            (prtouch_units.distance_mm_to_acc_ctl_cnt converts acc_ctl_mm
                            into this). Confirmed as an acceleration-shaping field from the
                            wire format; the exact ramp curve is internal MCU firmware
                            behavior, not independently confirmed.
          low_spd_nul     - name and wire position confirmed from the protocol; likely governs
                            some low-speed/near-zero pulse handling ("null" zone) given its
                            name, but the exact firmware-internal effect is NOT confirmed -
                            treat as reverse-engineered/uncertain. Left at its stock default
                            (5) everywhere in this codebase; nothing here has ever needed to
                            change it.
          send_step_duty  - name and wire position confirmed; plausibly a PWM-style duty
                            value for the step pulse itself, but not independently confirmed -
                            same uncertain-meaning caveat as low_spd_nul. Left at its stock
                            default (16) everywhere in this codebase.
          auto_rtn        - name confirmed from the wire format; this host never sets it to
                            anything but 0 (auto-return/auto-retract behavior is not used by
                            this rewrite - every recovery/lift move is issued explicitly by
                            prtouch_probe.py instead of relying on firmware auto-behavior).

        2026-08-14 (disarm-protocol mission): step_cnt=0 is rejected here, not silently
        accepted - see stop_step()'s own docstring for why a step_cnt=0 call through THIS
        method (which defaults send_ms=10) is not the same thing as a real disarm on the
        actual MCU wire protocol, and would not cleanly stop the step timer."""
        if step_cnt == 0:
            raise ValueError(
                "prtouch_mcu: start_step() called with step_cnt=0 (send_ms=%d) - this is not "
                "a valid disarm on the real MCU protocol (see stop_step()'s own docstring); "
                "call stop_step() instead" % send_ms)
        self.start_step_prtouch_cmd.send([
            self.step_oid, direction, send_ms, step_cnt, step_us, acc_ctl_cnt,
            low_spd_nul, send_step_duty, auto_rtn])

    def stop_step(self):
        """The one real step-disarm packet. 2026-08-14 (disarm-protocol mission - see
        docs/prtouch_timer_incident_forensics.md's incident timeline for the live fault
        this fixed): Creality's own real firmware's command_start_step_prtouch checks
        send_ms==0 (its 3rd wire field, `args[2]`) as the dedicated stop sentinel - on a match
        it sets need_stop=1, calls stop_sys_time(), and returns immediately, WITHOUT ever
        reaching sched_add_timer(). Every real stock disarm call sends send_ms=0 for exactly
        this reason. This host's own disarm calls previously went through start_step()'s own
        send_ms=10 default instead (step_cnt=0 but send_ms=10) - on the real protocol that
        does NOT hit the send_ms==0 early-return, and instead falls through to the normal arm
        path with step_cnt=0/step_us=0/acc_ctl_cnt=0 - a degenerate re-arm, not a clean stop.
        This method exists so that mistake is structurally impossible to make again: it always
        sends the exact stock disarm shape (all fields zero but oid), and start_step() itself
        now refuses a step_cnt=0 call rather than silently accepting one."""
        self.start_step_prtouch_cmd.send([self.step_oid, 0, 0, 0, 0, 0, 0, 0, 0])

    def start_pres(self, direction, acq_ms, send_ms, need_cnt, hftr_cut, lftr_k1,
                   min_hold, max_hold):
        """ARM the load-cell sampling + MCU-side trigger detection, concurrently with a
        start_step() descent (touch_probe's whole down-phase arms both together). This is
        where the physical "did the nozzle touch the bed" decision actually gets made - on
        the MCU, in real time, against every incoming pressure sample - NOT in
        prtouch_calibration.py (that file only re-derives WHERE within an already-triggered
        buffer the trigger tick falls; see module docstring / docs/PRTOUCH_INTERNALS.md's
        "two independent filtering passes" section for why these are deliberately separate).

        start_pres_prtouch wire fields, in order:
          oid       - this channel's object id.
          tri_dir   - direction of the concurrent step move (same encoding as start_step's
                      dir); lets the MCU's own trigger logic know which way is "into the bed".
          acq_ms    - pressure-sample acquisition interval in milliseconds (how often the MCU
                      reads the load cell). Config default 12ms for strain-gauge (HX711-style)
                      sensors, 1ms for use_adc=True (ADC/piezo) sensors - ADC channels can be
                      sampled far faster than a strain-gauge bridge, hence the much smaller
                      default.
          send_ms   - how often (ms) the MCU flushes a buffered pressure-sample chunk back to
                      the host. Same field/purpose as start_step's own send_ms, on this
                      channel instead.
          need_cnt  - number of consecutive filtered samples that must fall inside
                      [min_hold, max_hold] before the MCU latches a real trigger (a debounce/
                      confirmation count). Default 1 - a single qualifying sample is enough on
                      this printer's current tuning; raising it would make triggering slower
                      but more resistant to a single noisy sample.
          hftr_cut  - MCU-side high-pass filter cutoff, sent as a fixed-point integer
                      (prtouch_units.to_fixed_point, x1000) - this is the config's
                      tri_hftr_cut, distinct from prtouch_calibration.py's own cal_hftr_cut
                      (host-side re-filter, see module docstring). Only used for non-ADC
                      (strain-gauge) sensors - matches filter_pressure_series's own use_adc
                      branching.
          lftr_k1   - MCU-side low-pass filter coefficient (0-1, higher = less smoothing/
                      faster response to a fresh sample), sent fixed-point x1000 - this is
                      tri_lftr_k1, distinct from cal_lftr_k1.
          min_hold  - lower bound of the filtered-signal magnitude band that counts as a real
                      trigger, in the sensor's own raw/filtered units (not Newtons - the
                      absolute force transfer function of this load cell has not been
                      characterized). Sent as a plain int, NOT fixed-point scaled. Too low and
                      ordinary vibration/noise can register as contact; too high and a real,
                      light touch can be missed.
          max_hold  - upper bound of that same band. Also acts as an implausibility ceiling -
                      a reading beyond this is not treated as "harder contact", it is outside
                      the range this trigger logic was tuned for.
        """
        self.start_pres_prtouch_cmd.send([
            self.pres_oid, direction, acq_ms, send_ms, need_cnt,
            units.to_fixed_point(hftr_cut), units.to_fixed_point(lftr_k1),
            int(min_hold), int(max_hold)])

    def deal_avgs(self, base_cnt=8):
        return self.deal_avgs_prtouch_cmd.send([self.pres_oid, base_cnt])

    def read_swap(self):
        params = self.read_swap_prtouch_cmd.send([self.step_oid])
        return bool(params['sta'])

    def write_swap(self, state):
        self.write_swap_prtouch_cmd.send([self.pres_oid, int(state)])

    def collect_step_samples(self, timeout_s):
        """Wait for the step-sample response buffer to fill (MAX_BUF_LEN entries) or
        timeout_s to elapse, whichever comes first, then return whatever was collected -
        repairing the buffer via manual_get_steps if it's short (see _repair_step_samples).
        A short/empty buffer on a genuine no-trigger response is normal, not corrupted: the
        MCU still runs the full commanded step_cnt when nothing trips, it just never latches
        a trigger to short-circuit the async sample stream - see prtouch_probe.py's own
        comment at its no-trigger recovery call site for why that matters for toolhead
        position tracking."""
        end_time = self.reactor.monotonic() + timeout_s
        eventtime = self.reactor.monotonic()
        while len(self.step_res) != MAX_BUF_LEN and eventtime < end_time:
            eventtime = self.reactor.pause(eventtime + POLL_INTERVAL)
        if len(self.step_res) != MAX_BUF_LEN:
            self._repair_step_samples()
        return list(self.step_res)

    def collect_pres_samples(self, timeout_s):
        end_time = self.reactor.monotonic() + timeout_s
        eventtime = self.reactor.monotonic()
        while len(self.pres_res) != MAX_BUF_LEN and eventtime < end_time:
            eventtime = self.reactor.pause(eventtime + POLL_INTERVAL)
        if len(self.pres_res) != MAX_BUF_LEN:
            self._repair_pres_samples()
        return list(self.pres_res)

    def _repair_step_samples(self):
        logging.info("prtouch_mcu: repairing step samples, got %d/%d",
                      len(self.step_res), MAX_BUF_LEN)
        for i in range(0, MAX_BUF_LEN, 4):
            if len(self.step_res) > i and self.step_res[i]['index'] == i:
                continue
            params = self.manual_get_steps_cmd.send([self.step_oid, i])
            self.step_tri_time = units.mcu_ticks_to_seconds(params['tri_time'])
            for j in range(4):
                self.step_res.insert(i + j, {
                    'tick': units.mcu_ticks_to_seconds(params['tick%d' % j]),
                    'step': params['step%d' % j],
                    'index': params['index'],
                })
        if len(self.step_res) != MAX_BUF_LEN:
            raise PrtouchProtocolError(
                "step sample repair failed: got %d/%d" % (len(self.step_res), MAX_BUF_LEN))

    def _repair_pres_samples(self):
        logging.info("prtouch_mcu: repairing pres samples, got %d/%d",
                      len(self.pres_res), MAX_BUF_LEN)
        for i in range(0, MAX_BUF_LEN, 2):
            if len(self.pres_res) > i and self.pres_res[i]['index'] == i:
                continue
            # NOTE: Creality's original prtouch_v2_wrapper.py sends self.step_oid here, which
            # looks like a copy-paste bug from ck_and_manual_get_step - manual_get_pres is
            # registered under pres_oid (config_pres_prtouch/add_pres_prtouch), so this uses
            # pres_oid instead. This is a clean rewrite, not a verbatim port, so this was
            # corrected rather than preserved; flagged in case real-hardware testing ever
            # shows the original's behavior was intentional for some reason not visible in
            # the source.
            params = self.manual_get_pres_cmd.send([self.pres_oid, i])
            self.pres_tri_time = units.mcu_ticks_to_seconds(params['tri_time'])
            self.pres_tri_chs = params['tri_chs']
            self.pres_buf_cnt = params['buf_cnt']
            for j in range(2):
                self.pres_res.insert(i + j, {
                    'tick': units.mcu_ticks_to_seconds(params['tick_%d' % j]),
                    'ch0': params['ch0_%d' % j], 'ch1': params['ch1_%d' % j],
                    'ch2': params['ch2_%d' % j], 'ch3': params['ch3_%d' % j],
                    'index': params['index'],
                })
        if len(self.pres_res) != MAX_BUF_LEN:
            raise PrtouchProtocolError(
                "pres sample repair failed: got %d/%d" % (len(self.pres_res), MAX_BUF_LEN))
