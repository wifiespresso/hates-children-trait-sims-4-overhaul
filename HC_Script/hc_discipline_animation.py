"""
hc_discipline_animation.py
===========================
HatesChildren Overhaul — Harsh Discipline Animation Script

Pairs with: HatesChildren_Overhaul_Standalone.package
Requires:   Lumpinou's Mood Pack Mod
Requires:   Less Obsessive Parents (Tier 1)

Referenced by XML:
  E882D22F!00000000!0000000000F01035.mixer_HC_Discipline_Harsh_Toddler.InteractionTuning.xml

HOW THE ANIMATION WORKS
-----------------------
The BlackCinema clips were authored with the adult's ROOT bone at Y=0.5061 m —
the exact hip height of an adult seated on a standard Sims 4 chair (seat ≈ 0.43 m).
The toddler clip was authored at a complementary lap position.

Because the clips bake the ROOT position, they rely on the sims being placed at
the right world position BEFORE the clip fires. This script:

  Phase 1: Finds the nearest sittable chair within MAX_CHAIR_DIST units.
  Phase 2: Routes the adult to the chair and sits them using a vanilla affordance
           (the game handles routing + sit animation automatically).
  Phase 3: Moves the toddler to the adult's lap position (no carry posture needed —
           the toddler clip handles the lap pose via ROOT bone override).
  Phase 4: Fires both clips simultaneously via the native ARB system.
  Phase 5: Waits for clips to complete (flush_all_animations).
  Phase 6: Cleans up — releases toddler, adult stands up.
  Phase 7: Weighted 40/60 loot roll (Satisfied / Annoyed outcome).

DEBUGGING
---------
  Enable Script Mods + Script Errors in Options → Other.
  Logs: Documents/Electronic Arts/The Sims 4/Logs/Lastexception.txt
  Prefix: [HC_Overhaul]

  If the adult floats mid-air: the chair wasn't found or sat-in correctly.
  If the clips don't play: check 'native.animation available:' log line.
"""

from __future__ import annotations
import random

import services
import sims4.log
import sims4.resources

logger = sims4.log.Logger('HC_Overhaul', default_owner='HC_Overhaul')

# ── Clip names (must exactly match ClipName field in ClipHeader resources) ─────
ADULT_CLIP_NAME   = 'BlackCinema:PosePack_201710141919474062_set_1'
TODDLER_CLIP_NAME = 'BlackCinema:PosePack_201710141919474062_set_2'
CLIP_ACTOR_SLOT   = 'x'   # ActorName field in both ClipHeaders

# ── Loot IDs (decimal) defined in XML package ─────────────────────────────────
LOOT_SATISFIED_ID = 15732784   # loot_HC_Discipline_Outcome_Satisfied  (40%)
LOOT_ANNOYED_ID   = 15732785   # loot_HC_Discipline_Outcome_Annoyed    (60%)

# ── Chair search radius (Sims 4 world units; ~1 unit ≈ 1 tile) ────────────────
MAX_CHAIR_DIST = 12.0

# ── Time to wait for the adult to route to + sit in the chair (sim-minutes) ───
SETUP_WAIT_MINS   = 4.0

# ── Toddler lap offset — how far in front of adult to place the toddler ───────
# The clip ROOT handles the vertical position; we only offset horizontally.
LAP_FORWARD_OFFSET = 0.25   # metres forward of adult's hip position


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY: sleeping in an interaction generator
# ══════════════════════════════════════════════════════════════════════════════

def _sleep_gen(timeline, sim_minutes):
    """
    Yield control for approximately sim_minutes of game time.
    Tries three approaches in order of reliability, falling back to a
    raw yield loop if none of the game APIs are available.
    """
    try:
        import clock
        duration = clock.interval_in_sim_minutes(sim_minutes)

        # Approach A: timeline.run_child with a TimeSpan (most common)
        try:
            yield from timeline.run_child(duration)
            return
        except (AttributeError, TypeError):
            pass

        # Approach B: element_utils.run_child with duration
        try:
            import element_utils
            yield from element_utils.run_child(timeline, duration)
            return
        except (AttributeError, TypeError):
            pass

    except Exception:
        pass

    # Approach C: raw yield loop (~30 yields per real-second at normal speed)
    # 4 sim-minutes ≈ ~17 real-seconds at normal speed → ~510 yields
    ticks = max(50, int(sim_minutes * 120))
    for _ in range(ticks):
        yield


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — FIND CHAIR
# ══════════════════════════════════════════════════════════════════════════════

def _find_nearest_chair(sim):
    """
    Return the nearest sittable object on the active lot within MAX_CHAIR_DIST,
    or None if none exists.

    Uses Tag.Func_Seating which covers dining chairs, desk chairs, couches,
    sofas, and armchairs — anything the game treats as a valid sit surface.
    """
    try:
        from tag import Tag

        sim_pos      = sim.position
        nearest      = None
        nearest_dist = float('inf')

        for obj in services.object_manager().values():
            try:
                if not obj.is_on_active_lot():
                    continue

                obj_tags = obj.get_tags() if hasattr(obj, 'get_tags') else set()
                if Tag.Func_Seating not in obj_tags:
                    continue

                dist = (obj.position - sim_pos).magnitude_2d()
                if dist < nearest_dist and dist <= MAX_CHAIR_DIST:
                    nearest_dist = dist
                    nearest = obj

            except Exception:
                continue

        if nearest:
            logger.debug('HC: found chair {} at {:.1f} units', nearest, nearest_dist)
        else:
            logger.warn('HC: no chair found within {} units — clips will play standing', MAX_CHAIR_DIST)

        return nearest

    except Exception:
        logger.exception('HC: chair search failed')
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — SIT ADULT IN CHAIR
# ══════════════════════════════════════════════════════════════════════════════

def _push_sit_affordance(actor, chair) -> bool:
    """
    Discover the vanilla sit affordance on the chair object and push it onto
    the adult sim. The game handles routing to the chair automatically.

    Returns True if an affordance was successfully pushed.
    """
    try:
        from interactions.context import InteractionContext, QueueInsertStrategy
        from interactions.priority import Priority

        context = InteractionContext(
            actor,
            InteractionContext.SOURCE_SCRIPT,
            Priority.High,
            insert_strategy=QueueInsertStrategy.NEXT
        )

        # Discover valid sit affordances on the chair.
        # We look for affordances whose class name contains 'sit' but NOT
        # 'getup', 'standup', 'wakeup', etc. (those are the stand-up variants).
        SKIP_WORDS = ('getup', 'get_up', 'standup', 'stand_up', 'wakeup', 'wake')
        WANT_WORDS = ('sit',)

        for affordance in chair.super_affordances(actor, context=context):
            name = type(affordance).__name__.lower()
            if any(w in name for w in WANT_WORDS) and not any(w in name for w in SKIP_WORDS):
                result = actor.push_super_affordance(affordance, chair, context)
                if result:
                    logger.debug('HC: pushed sit affordance {} on {}', name, chair)
                    return True

        # Second pass: try without actor filter (catches some edge cases)
        try:
            for affordance in chair.super_affordances():
                name = type(affordance).__name__.lower()
                if any(w in name for w in WANT_WORDS) and not any(w in name for w in SKIP_WORDS):
                    result = actor.push_super_affordance(affordance, chair, context)
                    if result:
                        logger.debug('HC: pushed sit affordance (pass 2) {} on {}', name, chair)
                        return True
        except Exception:
            pass

        logger.warn('HC: no sit affordance found on chair {}', chair)
        return False

    except Exception:
        logger.exception('HC: _push_sit_affordance failed')
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — POSITION TODDLER AT LAP
# ══════════════════════════════════════════════════════════════════════════════

def _position_toddler_at_lap(toddler, adult):
    """
    Move the toddler to the adult's lap position without a carry posture.

    The toddler clip (set_2) bakes the ROOT bone Y at lap height, so we
    only need to place the toddler at the correct XZ position and orientation.
    The clip takes care of the rest when it fires.

    We place the toddler slightly in front of the adult's hip position,
    facing the same direction as the adult.

    Returns the toddler's original location so we can restore it in cleanup.
    """
    try:
        import sims4.math
        from sims4.math import Vector3

        original_location = toddler.location

        adult_pos = adult.position
        adult_fwd = adult.forward_vector

        # Target: slightly in front of adult's seated hip (X/Z only; Y is ground)
        lap_pos = Vector3(
            adult_pos.x + adult_fwd.x * LAP_FORWARD_OFFSET,
            adult_pos.y,   # keep on the same floor level; clip handles Y via ROOT
            adult_pos.z + adult_fwd.z * LAP_FORWARD_OFFSET
        )

        toddler.move_to(
            translation  = lap_pos,
            orientation  = adult.orientation,
            routing_surface = adult.routing_surface
        )

        logger.debug('HC: toddler positioned at lap ({:.2f}, {:.2f}, {:.2f})',
                     lap_pos.x, lap_pos.y, lap_pos.z)
        return original_location

    except Exception:
        logger.exception('HC: toddler positioning failed')
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — PLAY CLIPS
# ══════════════════════════════════════════════════════════════════════════════

def _request_clip_on_sim(sim, clip_name: str) -> bool:
    """
    Queue a BlackCinema clip on a sim via the native ARB system.
    Tries three common function signatures for cross-version compatibility.
    """
    try:
        import native.animation as _na
        from animation.arb import Arb

        arb    = Arb()
        queued = False

        # Approach A: clip_arb (full parameter list)
        if not queued and hasattr(_na, 'clip_arb'):
            try:
                _na.clip_arb(arb, sim.rig, clip_name, CLIP_ACTOR_SLOT,
                             0.0, 0.0, False, 5, 1.0)
                queued = True
            except TypeError:
                try:
                    _na.clip_arb(arb, sim.rig, clip_name)
                    queued = True
                except Exception:
                    pass

        # Approach B: clip_arb_fnv
        if not queued and hasattr(_na, 'clip_arb_fnv'):
            try:
                _na.clip_arb_fnv(arb, sim.rig, clip_name)
                queued = True
            except Exception:
                pass

        # Approach C: request_clip
        if not queued and hasattr(_na, 'request_clip'):
            try:
                _na.request_clip(sim.id, clip_name, actor_name=CLIP_ACTOR_SLOT)
                queued = True
            except TypeError:
                try:
                    _na.request_clip(sim.id, clip_name)
                    queued = True
                except Exception:
                    pass

        if queued:
            arb.distribute()
            logger.debug('HC: clip queued — {}', clip_name)
        else:
            available = [a for a in dir(_na) if 'clip' in a.lower() or 'arb' in a.lower()]
            logger.error('HC: no compatible clip function. native.animation available: {}', available)

        return queued

    except Exception:
        logger.exception('HC: clip playback error — {}', clip_name)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 6 — CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

def _restore_toddler(toddler, original_location):
    """Move the toddler back to where they were before the interaction."""
    if original_location is None:
        return
    try:
        toddler.move_to(
            translation    = original_location.transform.translation,
            orientation    = original_location.transform.orientation,
            routing_surface = original_location.routing_surface
        )
        logger.debug('HC: toddler restored to original position')
    except Exception:
        logger.exception('HC: toddler restore failed')


def _push_stand_up(actor, chair) -> bool:
    """
    Push a get-up / stand-up affordance on the adult so they naturally
    rise from the chair after the discipline.
    """
    try:
        from interactions.context import InteractionContext, QueueInsertStrategy
        from interactions.priority import Priority

        context = InteractionContext(
            actor,
            InteractionContext.SOURCE_SCRIPT,
            Priority.High,
            insert_strategy=QueueInsertStrategy.NEXT
        )

        STAND_WORDS = ('getup', 'get_up', 'standup', 'stand_up', 'rise', 'leave')

        # Look on actor's current affordances (posture affordances appear here when sitting)
        for affordance in actor.super_affordances(actor, context=context):
            name = type(affordance).__name__.lower()
            if any(w in name for w in STAND_WORDS):
                result = actor.push_super_affordance(affordance, chair, context)
                if result:
                    logger.debug('HC: pushed stand-up affordance {}', name)
                    return True

        # Fallback: look on the chair
        for affordance in chair.super_affordances(actor, context=context):
            name = type(affordance).__name__.lower()
            if any(w in name for w in STAND_WORDS):
                result = actor.push_super_affordance(affordance, chair, context)
                if result:
                    logger.debug('HC: pushed stand-up (from chair) {}', name)
                    return True

        logger.warn('HC: no stand-up affordance found — adult will idle in chair')
        return False

    except Exception:
        logger.exception('HC: _push_stand_up failed')
        return False


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 7 — LOOT
# ══════════════════════════════════════════════════════════════════════════════

def _apply_loot(loot_id: int, actor, target) -> None:
    """Apply a LootActions resource (XML-defined) to two Sims."""
    try:
        from event_testing.resolver import DoubleSimResolver

        loot_manager = services.get_instance_manager(sims4.resources.Types.ACTION)
        loot = loot_manager.get(loot_id)
        if loot is None:
            logger.error('HC: loot {} not found — is the package loaded?', loot_id)
            return

        try:
            resolver = DoubleSimResolver(actor, target)
        except (TypeError, AttributeError):
            resolver = DoubleSimResolver(
                getattr(actor,  'sim_info', actor),
                getattr(target, 'sim_info', target)
            )

        loot.apply_to_resolver(resolver)
        logger.debug('HC: loot {} applied', loot_id)

    except Exception:
        logger.exception('HC: loot {} failed', loot_id)


# ══════════════════════════════════════════════════════════════════════════════
# INTERACTION CLASS
# ══════════════════════════════════════════════════════════════════════════════

try:
    from interactions.base.immediate_interaction import ImmediateSuperInteraction
    from animation.animation_utils import flush_all_animations
    import element_utils

    class HC_HarshDiscipline(ImmediateSuperInteraction):
        """
        Harsh Discipline interaction for sims with trait_HatesChildren.

        Full sequence:
          1. Find nearest chair
          2. Route adult to chair + sit (vanilla affordance)
          3. Position toddler at lap
          4. BlackCinema whooping clips fire (set_1 adult, set_2 toddler)
          5. Flush animations (wait for clips to complete)
          6. Restore toddler position + adult stands up
          7. Weighted loot: 40% Satisfied / 60% Annoyed

        Referenced by XML instance 15728693:
          E882D22F!00000000!0000000000F01035.mixer_HC_Discipline_Harsh_Toddler
        """

        INSTANCE_TUNABLES = {}

        def _run_interaction_gen(self, timeline):
            actor  = self.sim      # HC adult performing the discipline
            target = self.target   # Toddler

            logger.info(
                'HC Harsh Discipline: {} → {}',
                getattr(actor,  'full_name', actor),
                getattr(target, 'full_name', target)
            )

            # ── Phase 1: Find chair ────────────────────────────────────────
            chair = _find_nearest_chair(actor)

            sit_succeeded      = False
            original_toddler_loc = None

            if chair:
                # ── Phase 2: Route adult to chair + sit ───────────────────
                sit_succeeded = _push_sit_affordance(actor, chair)

                if sit_succeeded:
                    # Wait for the adult to finish routing to the chair
                    # and complete the sit animation.
                    logger.debug('HC: waiting {}m for adult to sit...', SETUP_WAIT_MINS)
                    yield from _sleep_gen(timeline, SETUP_WAIT_MINS)

                    # ── Phase 3: Position toddler at lap ──────────────────
                    original_toddler_loc = _position_toddler_at_lap(target, actor)
                else:
                    logger.warn('HC: sit push failed — clips will play standing')
            else:
                logger.warn('HC: no chair found — clips will play at current positions')

            # ── Phase 4: Play clips ────────────────────────────────────────
            # Both clips fire simultaneously. set_1 plays the adult's arm
            # motion; set_2 plays the toddler's reaction. ROOT bones baked
            # in the clips handle each sim's position.
            adult_ok   = _request_clip_on_sim(actor,  ADULT_CLIP_NAME)
            toddler_ok = _request_clip_on_sim(target, TODDLER_CLIP_NAME)

            if not adult_ok and not toddler_ok:
                logger.warn(
                    'HC: neither clip could be queued. '
                    'Check Lastexception for native.animation function names.'
                )

            # ── Phase 5: Wait for clips to finish ─────────────────────────
            # flush_all_animations() yields until all pending ARBs on all
            # sims have resolved — the correct way to wait for clips in Sims 4.
            try:
                yield from element_utils.run_child(timeline, flush_all_animations())
            except Exception:
                logger.warn('HC: flush_all_animations unavailable, using yield fallback')
                for _ in range(15):
                    yield

            # ── Phase 6: Cleanup ──────────────────────────────────────────
            if original_toddler_loc is not None:
                _restore_toddler(target, original_toddler_loc)

            if sit_succeeded and chair is not None:
                _push_stand_up(actor, chair)

            # ── Phase 7: Weighted loot ────────────────────────────────────
            # 40% → Satisfied/Scornful adult + trait-aware Hurt/Defiant toddler
            # 60% → Annoyed/Resentful adult + Scared/Upset toddler
            roll = random.random()

            if roll < 0.40:
                logger.debug('HC: Satisfied outcome (roll {:.2f})', roll)
                _apply_loot(LOOT_SATISFIED_ID, actor, target)
            else:
                logger.debug('HC: Annoyed outcome (roll {:.2f})', roll)
                _apply_loot(LOOT_ANNOYED_ID,   actor, target)

            return True

except ImportError as e:
    logger.exception('HC: import error defining HC_HarshDiscipline — {}', e)
except Exception:
    logger.exception('HC: unexpected error defining HC_HarshDiscipline')
