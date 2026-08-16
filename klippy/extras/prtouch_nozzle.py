# prtouch_v2 nozzle-wipe routine
#
# This drives the physical wipe sequence: heat, probe two points on a fixed wipe-pad area to
# find its local Z height (via PrtouchProbe.touch_probe(), the same probing state machine
# prtouch_probe.py implements), drag the hot nozzle across the pad between those two points,
# then cool. All of the coordinates below are relative to clr_noz_start_x/y - the wipe pad's
# own origin on this printer's bed, NOT bed (0, 0) - see ClearNozzleConfig's own comments for
# where that pad physically sits and which direction it runs.
#
# Clean-room rewrite of Creality's clear_nozzle() (prtouch_v2_wrapper.py, GPLv3-licensed
# Creality source, not included in this tree), read completely and traced during the original
# reverse-engineering work. Not a verbatim port: drops the per-run velocity/accel override
# (set_step_par - a wipe-speed optimization, not a correctness requirement as long as
# clr_xy_spd/rdy_xy_spd stay under the printer's own configured max_velocity) and the
# out-of-range Z-reference-reset retry path (nozzle_clear_z_out_of_range - only matters if the
# wipe pad sits implausibly close to position_min, which would be a config error worth
# surfacing directly rather than silently working around).
#
# Wipe-drag geometry generalized to a 2D vector (pa_clr_dis_mm_x/y) rather than the single
# X-only pa_clr_dis_mm the reference wrapper reads - this printer's own real [z_compensate]
# section (pulled live via SSH 2026-08-05) has pa_clr_dis_mm_x: 0 / pa_clr_dis_mm_y: 30 against
# clr_noz_len_x: 3 / clr_noz_len_y: 50, i.e. its wipe pad is a narrow strip running along Y, the
# opposite orientation from the generic reference defaults (wide-X/narrow-Y) this file originally
# assumed. Setting pa_clr_dis_mm_y=0 exactly recovers the old X-only behavior, so this is a
# strict generalization, not a behavior change for any config that only sets the X component.
#
# Config reads live in ClearNozzleConfig, built once at __init__/load_config time by whichever
# module owns the config section - NOT inside clear_nozzle() itself. Confirmed live 2026-08-05:
# Klipper's configfile checks that every option present in a section was read at least once
# during the whole startup config-load pass, before any gcode ever runs - reading options lazily
# inside a gcode-command handler is too late and hard-errors at startup ("Option '...' is not
# valid in section '...'") the instant that section has any real value the __init__ path didn't
# already touch. This is why clr_noz_start_x etc. previously lived inside clear_nozzle()'s own
# body: worked fine offline (nothing ever calls it there), broke immediately on a real restart.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import random


class ClearNozzleConfig:
    """All config.get*() reads clear_nozzle() needs, resolved once by the owning module's own
    __init__ (PRTouchV2 for [prtouch_v2], ZCompensate for [z_compensate]) - see module docstring
    for why this can't happen lazily inside clear_nozzle() itself. Defaults are wide enough that
    [prtouch_v2]'s own real section (which has none of these keys at all) doesn't error - its own
    NOZZLE_CLEAR command stays effectively dead code (nothing in real production calls it) but no
    longer crashes the whole printer if it ever is."""

    def __init__(self, config):
        # clr_noz_start_x/clr_noz_start_y: the wipe pad's own bottom-left corner, in absolute
        # bed XY mm - everything else in this class is relative to this point, not to bed
        # (0, 0). clr_noz_start_x allows negative (this printer's real value is -3 - the wipe
        # pad sits partly off the near edge of the bed's own X origin, not a typo; the pad
        # itself is a physical brush/silicone strip mounted at the bed edge, common on this
        # printer family).
        self.clr_noz_start_x = config.getfloat('clr_noz_start_x', default=0.,
                                                 minval=-50, maxval=1000)
        self.clr_noz_start_y = config.getfloat('clr_noz_start_y', default=0.,
                                                 minval=0, maxval=1000)
        # clr_noz_len_x/clr_noz_len_y: the wipe pad's own physical footprint (mm) from its
        # start corner - how much room clear_nozzle() has to randomize where on the pad it
        # probes/wipes each time (see the avail_x/avail_y margin math below). This printer's
        # real pad is a narrow Y-running strip (clr_noz_len_x=3, clr_noz_len_y=50) - most of
        # its usable travel is in Y, almost none in X.
        self.clr_noz_len_x = config.getfloat('clr_noz_len_x', default=1., minval=1)
        self.clr_noz_len_y = config.getfloat('clr_noz_len_y', default=1., minval=1)
        # pa_clr_dis_mm_x/pa_clr_dis_mm_y: the wipe-drag vector (mm) - how far, and in which
        # direction, the nozzle drags across the pad between the two probed points. Together
        # they generalize what Creality's original wrapper only expressed as a single X-only
        # distance (pa_clr_dis_mm) to a full 2D vector, since this printer's real pad runs
        # along Y (pa_clr_dis_mm_x=0, pa_clr_dis_mm_y=30) - the opposite orientation from the
        # wide-X/narrow-Y layout the original code assumed. Setting the Y component to 0
        # exactly recovers the original X-only behavior for any printer whose pad runs the
        # other way.
        self.pa_clr_dis_mm_x = config.getfloat('pa_clr_dis_mm_x', default=30,
                                                minval=-100, maxval=100)
        self.pa_clr_dis_mm_y = config.getfloat('pa_clr_dis_mm_y', default=0,
                                                minval=-100, maxval=100)
        # pa_clr_down_mm: a small Z offset (mm) from the probed pad-surface height, used in
        # BOTH directions by clear_nozzle() (see its own comments at each use site) - as a
        # small positive clearance above the surface for the pre-heat "rest" position (heating
        # up while not yet touching the pad), and as a small negative push into the surface
        # for the actual wipe-drag move (real contact is the point of a wipe). Negative
        # default (-0.15mm); a value this small is intentional, not a rounding artifact - too
        # deep risks jamming the nozzle into a firm pad or the metal bed beneath a worn one.
        self.pa_clr_down_mm = config.getfloat('pa_clr_down_mm', default=-0.15,
                                               minval=-1, maxval=1)
        # clr_xy_spd: XY travel speed (mm/s) while actually dragging the hot nozzle across the
        # pad - deliberately slow (default 2.0) since this is the working part of the wipe,
        # not just repositioning.
        self.clr_xy_spd = config.getfloat('clr_xy_spd', default=2.0, minval=0.1)
        # rdy_xy_spd: XY travel speed (mm/s) for plain repositioning moves between wipe-cycle
        # stages (moving to the start point, moving to hover height, etc.) - fast (default
        # 200) since no contact happens during these moves.
        self.rdy_xy_spd = config.getfloat('rdy_xy_spd', default=200, minval=1)
        # bed_max_err: dual-purpose - (1) the extra Z clearance added above the second probed
        # point's height before the final retreat move at the end of clear_nozzle() (see its
        # own last _move() call), and (2) the fallback default for hover_z below when
        # vs_start_z_pos isn't set. Name suggests a bed-flatness tolerance; kept as-is since
        # that's the real config key's name, not renamed to something more specific.
        self.bed_max_err = config.getfloat('bed_max_err', default=5, minval=1)
        # g29_down_min_z: the down_min_z passed to touch_probe() for both wipe-pad probes -
        # how far the nozzle is allowed to descend from hover_z while searching for the pad
        # surface. Distinct from z_compensate.py's own z_offset_down_min_z (a different probe,
        # at a different XY location, with its own separately-configured travel bound).
        self.g29_down_min_z = config.getfloat('g29_down_min_z', default=25, minval=1)
        # vs_start_z_pos (real key): hover height (mm above the bed) the nozzle moves to
        # before each wipe-pad touch probe begins its descent. Falls back to bed_max_err (the
        # pre-existing dual-use default) when unset.
        self.hover_z = config.getfloat('vs_start_z_pos', default=self.bed_max_err)
        # pr_clear_probe_cnt (real key): probe-agreement count (touch_probe()'s own pro_cnt)
        # for these two wipe-pad touches, distinct from Z_OFFSET_CALIBRATION's own
        # pr_probe_cnt (read in z_compensate.py) - the wipe-pad probes don't need to be as
        # precise as a real Z-offset calibration measurement, but still default to the same
        # value (3) for consistency.
        self.pr_clear_probe_cnt = config.getint('pr_clear_probe_cnt', default=3, minval=1)


class NozzleHeaters:
    """Thin wrapper around Klipper's own heater objects for wait-for-temp semantics
    (set_hot_temps/set_bed_temps-equivalent). Built once at connect time and
    shared across every clear_nozzle() call - no global mutable state of its own."""

    def __init__(self, printer):
        self.reactor = printer.get_reactor()
        self.pheaters = printer.lookup_object('heaters')
        self.extruder_heater = printer.lookup_object('extruder').heater
        self.bed_heater = printer.lookup_object('heater_bed').heater

    def set_hot_temp(self, temp, wait=False, tolerance=5.0):
        self.pheaters.set_temperature(self.extruder_heater, temp, False)
        if not wait:
            return
        eventtime = self.reactor.monotonic()
        while (self.extruder_heater.target_temp > 0
               and abs(self.extruder_heater.target_temp
                       - self.extruder_heater.smoothed_temp) > tolerance):
            eventtime = self.reactor.pause(eventtime + 0.1)

    def set_bed_temp(self, temp, wait=False, tolerance=5.0):
        self.pheaters.set_temperature(self.bed_heater, temp, False)
        if not wait:
            return
        eventtime = self.reactor.monotonic()
        while (self.bed_heater.target_temp > 0
               and abs(self.bed_heater.target_temp - self.bed_heater.smoothed_temp) > tolerance):
            eventtime = self.reactor.pause(eventtime + 0.1)


def _move(gcode, toolhead, pos, speed):
    gcode.run_script_from_command(
        'G1 F%d X%.3f Y%.3f Z%.3f' % (speed * 60, pos[0], pos[1], pos[2]))
    toolhead.wait_moves()


def clear_nozzle(probe, toolhead, gcode, heaters, params,
                  hot_min_temp, hot_max_temp, bed_max_temp, hot_end_temp=None):
    """clear_nozzle()-equivalent: heat bed/nozzle, probe two randomized XY
    points on the wipe pad via probe.touch_probe() to find local Z at each, drag the nozzle
    between them at wipe temp, then cool. `probe` is a PrtouchProbe (prtouch_probe.py); its own
    touch_probe() already suspends the active bed mesh for the duration of each probe. `params`
    is a ClearNozzleConfig, already resolved from config at __init__ time by the caller (see
    module docstring for why this can't be read lazily here).

    `hot_end_temp` (real config key, [z_compensate]-only - not read here as a bare default
    because [prtouch_v2]'s own real section never sets it): final nozzle temp to settle at once
    the wipe finishes, defaulting to hot_min_temp (the pre-existing behavior) when omitted.

    Physical sequence, roughly:
      1. Start bed/nozzle heating (non-blocking) so they're warming up during the moves below.
      2. Pick two random points (src, end) on the wipe pad, pa_clr_dis_mm_x/y apart, within
         the pad's own usable footprint (avail_x/avail_y below, shrunk by `margin` so a probe
         point never lands right at the pad's physical edge).
      3. Touch-probe both points at hover_z to find their real local Z (the pad surface isn't
         perfectly flat/level with the rest of the bed, hence probing rather than assuming a
         fixed height).
      4. Move to just above (+pa_clr_down_mm's magnitude) the src point's measured surface and
         finish heating to hot_max_temp there - the nozzle rests near, but not against, the
         pad while it reaches full wipe temperature.
      5. Drag from src to end at clr_xy_spd, this time pressed slightly INTO the surface
         (-pa_clr_down_mm) - the actual wipe contact - while cooling back down to hot_min_temp,
         so the nozzle isn't still oozing at full temp by the time it lifts off the pad.
      6. Retreat to a bed_max_err clearance above the end point and settle at the final temp
         (hot_end_temp if given, else hot_min_temp) while the bed cools back down.
    """
    clr_noz_start_x = params.clr_noz_start_x
    clr_noz_start_y = params.clr_noz_start_y
    clr_noz_len_x = params.clr_noz_len_x
    clr_noz_len_y = params.clr_noz_len_y
    pa_clr_dis_mm_x = params.pa_clr_dis_mm_x
    pa_clr_dis_mm_y = params.pa_clr_dis_mm_y
    pa_clr_down_mm = params.pa_clr_down_mm
    clr_xy_spd = params.clr_xy_spd
    rdy_xy_spd = params.rdy_xy_spd
    bed_max_err = params.bed_max_err
    g29_down_min_z = params.g29_down_min_z
    hover_z = params.hover_z
    pr_clear_probe_cnt = params.pr_clear_probe_cnt

    heaters.set_bed_temp(bed_max_temp, wait=False)
    heaters.set_hot_temp(hot_min_temp, wait=False)

    # margin: keeps a randomized probe point from landing right at the pad's own physical
    # edge (where a touch reading would be less trustworthy) - not itself a config value,
    # since 5mm of edge clearance isn't something this printer's own pad geometry needs to
    # tune per-printer.
    margin = 5
    avail_x = max(clr_noz_len_x - abs(pa_clr_dis_mm_x) - margin, 0)
    avail_y = max(clr_noz_len_y - abs(pa_clr_dis_mm_y) - margin, 0)
    src_x = clr_noz_start_x + random.uniform(0, avail_x)
    src_y = clr_noz_start_y + random.uniform(0, avail_y)
    src_pos = [src_x, src_y, hover_z]
    end_pos = [src_x + pa_clr_dis_mm_x, src_y + pa_clr_dis_mm_y, hover_z]

    # Get the nozzle into a safe pre-wipe temp band while everything above is still just
    # coordinate math - waits for hot_min_temp (a temp low enough that oozing filament won't
    # foul the pad during the approach moves below), then kicks off a further +40 deg ramp
    # (non-blocking) toward wipe temp so it's already climbing during the two probe touches.
    heaters.set_hot_temp(hot_min_temp, wait=True)
    heaters.set_hot_temp(hot_min_temp + 40, wait=False)

    # Probe both wipe-pad points at hover_z to find their real local Z (the pad is not
    # assumed perfectly flat/level with the rest of the bed).
    _move(gcode, toolhead, src_pos, rdy_xy_spd)
    src_pos[2] = probe.touch_probe(g29_down_min_z, retries=5, pro_cnt=pr_clear_probe_cnt)

    _move(gcode, toolhead, end_pos, rdy_xy_spd)
    end_pos[2] = probe.touch_probe(g29_down_min_z, retries=5, pro_cnt=pr_clear_probe_cnt)

    # Back to hover height, then down to just ABOVE the probed src surface (subtracting
    # pa_clr_down_mm, which is negative, nets a small positive clearance) - a resting position
    # near, but not touching, the pad while the nozzle finishes heating to full wipe temp.
    # Uses tri_z_up_spd (this class's own lift speed) since this is a controlled Z approach,
    # not the wipe-drag move itself.
    _move(gcode, toolhead, [src_pos[0], src_pos[1], hover_z], rdy_xy_spd)
    _move(gcode, toolhead, [src_pos[0], src_pos[1], src_pos[2] - pa_clr_down_mm],
          probe.tri_z_up_spd)
    heaters.set_hot_temp(hot_max_temp, wait=True)

    # The actual wipe: drag from src to end at clr_xy_spd (the slow, deliberate wipe speed),
    # this time ADDING pa_clr_down_mm (negative) to press slightly INTO the pad surface - real
    # contact is the point of a wipe. Cools back to hot_min_temp while still in contact, so
    # the nozzle isn't still oozing at full temp by the time it lifts off.
    _move(gcode, toolhead, [end_pos[0], end_pos[1], end_pos[2] + pa_clr_down_mm], clr_xy_spd)
    heaters.set_hot_temp(hot_min_temp, wait=True)

    # Final retreat: past the end point by another full drag-vector, and up by bed_max_err -
    # clear of the pad in both XY and Z - then settle at the final post-wipe temp (hot_end_temp
    # if the caller gave one, else hot_min_temp) while the bed itself cools back down.
    _move(gcode, toolhead,
          [end_pos[0] + pa_clr_dis_mm_x, end_pos[1] + pa_clr_dis_mm_y, end_pos[2] + bed_max_err],
          clr_xy_spd)
    heaters.set_hot_temp(hot_min_temp if hot_end_temp is None else hot_end_temp, wait=False)
    heaters.set_bed_temp(bed_max_temp, wait=True)
