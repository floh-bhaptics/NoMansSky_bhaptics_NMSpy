# /// script
# dependencies = ["nmspy>=169922.0", "bhaptics_python"]
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
# default_mod_save_dir = "."
# internal_mod_dir = "."
#
# [tool.pymhf.logging]
# shown = false
# log_dir = "."
# log_level = "info"
# window_name_override = "No Mans Sky bhaptics mod (NMS.py)"
# ///

"""
No Man's Sky — bHaptics haptic suit mod  (NMS.py, no-polling edition)

Strategy for every hook:
  - No hooks on cGcPlayer.Update or cGcLocalPlayerCharacterInterface.IsJetpacking.
  - cGcLaserBeam.Fire is still per-frame, but the hook body is three lines;
    a cGcPlayerWeapon.Update hook (narrower scope than cGcPlayer.Update)
    handles the off-edge only when the laser is believed active.
  - All spaceship hooks (Update, GetVelocity, GetHeatFactor, UpdatePulseDrive)
    are acceptable because they only run while the player is piloting a ship.

Haptic patterns used:
  heartbeat / PlayerDeath / FallDamage
  DamageFront / DamageBack / DamageLeft / DamageRight / DefaultDamage
  RightHandPistolShoot / LeftHandPistolShoot
  RightHandPistolLaserShoot / LeftHandPistolLaserShoot  (looping)
  Scanning  (looping)
  CollectItem
  GetOnSpaceship / GetOffSpaceship
  SpaceshipTakeOff / SpaceshipOnGround
  SpaceshipSpeedUp / SpaceshipPulse (looping) / SpaceshipWeaponShoot (looping)
"""

import ctypes
import logging
import math
import time
from ctypes import _Pointer

from pymhf import Mod
from pymhf.core.hooking import function_hook, Structure

import nmspy.data.types as nms
import nmspy.data.enums as nmse

from bhaptics_library import bhaptics_suit, TimerController

logger = logging.getLogger("NMS_bhaptics")

# ---------------------------------------------------------------------------
# bHaptics credentials
# ---------------------------------------------------------------------------
BHAPTICS_APP_ID   = "693ac4ffa277918a719a1bd8"
BHAPTICS_API_KEY  = "uSEDPxsVOpRefEGM7FAc"
BHAPTICS_DEFAULT_PATTERNS = ""

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
SHIP_ACCEL_THRESHOLD_SQ    = 2500   # speed-sq delta to trigger SpaceshipSpeedUp
SHIP_WEAPON_HEAT_THRESHOLD = 0.02   # heat-factor rise per frame = "firing"
SHIP_WEAPON_COOLDOWN       = 0.35   # seconds of no heat rise before loop stops
SHIP_FIRE_DEBOUNCE         = 0.10   # min seconds between separate fire-burst haptics
LASER_BEAM_COOLDOWN        = 0.25   # seconds of no Fire call before beam is "off"
PLAYER_WEAPON_HEAT_THRESHOLD = 0.05  # mfHeatTime rise per Update tick = "shot fired" 

# ---------------------------------------------------------------------------
# cGcNetworkWeapon — not yet in NMS.py, define locally with raw byte pattern
# ---------------------------------------------------------------------------

class cGcNetworkWeapon(Structure):
    @function_hook("40 53 41 56 41 57 48 81 EC E0 00 00 00 8D 41 ?? 4D 8B D1")
    def FireRemote(self, this: ctypes.c_uint64): ...


# ---------------------------------------------------------------------------
# Mod
# ---------------------------------------------------------------------------

class NMSBhapticsMod(Mod):
    __author__      = "floh-bhaptics"
    __description__ = "bHaptics haptic suit integration — no-polling edition"
    __version__     = "2.1.0"

    def __init__(self):
        super().__init__()

        # --- player state ---
        self.player_hand: int = 0
        self.is_in_spaceship: bool = False

        # laser
        self._laser_active: bool = False
        self._laser_last_fire: float = 0.0

        # ship
        self._last_land_state: int = -1
        self._prev_velocity_sq: float = 0.0
        self._prev_heat: float = 0.0
        self._heat_last_rise: float = 0.0
        self._ship_fire_last_trigger: float = 0.0
        self._pulse_fired: bool = False

        # multitool projectile fire detection
        self._prev_weapon_heat: float = 0.0
        self._player_weapon_fired: bool = False

        # scanner
        self._scan_active: bool = False

        # world interactions
        self._last_interaction_event: int = -1

        # --- connect bHaptics ---
        # bhaptics_suit.__init__ starts a background thread and returns immediately
        # — no sleep, no blocking the game thread.
        logger.debug("Initialising bHaptics suit…")
        self.suit = bhaptics_suit(
            app_id=BHAPTICS_APP_ID,
            api_key=BHAPTICS_API_KEY,
            default_pattern=BHAPTICS_DEFAULT_PATTERNS,
        )
        self.timers = TimerController(self)
        logger.debug("bHaptics suit initialised (connecting in background…)")

    # ===================================================================
    # PLAYER — death
    # ===================================================================

    @nms.cGcPlayerCharacterComponent.SetDeathState.after
    def on_player_death(self, this, *args):
        logger.debug("PlayerDeath")
        self.suit.play_pattern("PlayerDeath")

    # ===================================================================
    # PLAYER — damage (directional)
    # ===================================================================

    @nms.cGcPlayer.TakeDamage.after
    def on_take_damage(self, this, lfDamageAmount, leDamageType, lDamageId,
                       lDir, lpOwner, laEffects):
        # lDamageId is a pointer to a TkID[0x10] struct; str() decodes its
        # char array to a plain string like "LANDING", "PROJECTILE", etc.
        try:
            damage_id = str(lDamageId.contents)
        except Exception:
            damage_id = ""

        if damage_id == "LANDING":
            logger.debug("FallDamage")
            self.suit.play_pattern("FallDamage")
            return

        try:
            d = lDir.contents
            rotation = _dir_to_rotation(d.x, d.z)
        except Exception:
            rotation = 0.0
        logger.debug(f"Damage id={damage_id} type={leDamageType} rotation={rotation:.0f}deg")
        self.suit.play_damage("DefaultDamage", rotation)

    # ===================================================================
    # PLAYER — dominant hand
    #
    # The debug log showed this firing well over 100 times/second — not
    # "rarely" as originally assumed. True hook removal at runtime would
    # need direct access to pymhf's internal hook-disable API, which isn't
    # part of the public NMS.py Mod interface and isn't something we can
    # safely guess without testing against a live process — worth asking
    # monkeyman192 about if this turns out to matter for performance.
    #
    # For now we cut the cost down as much as possible without that: only
    # update state and log when the value actually changes (which should
    # be never, or extremely rarely if a player changes hand preference
    # mid-session), turning nearly every call into a single int comparison.
    # ===================================================================

    @nms.cGcPlayer.GetDominantHand.after
    def on_dominant_hand(self, this, *args, _result_):
        hand = int(_result_)
        if hand != self.player_hand:
            self.player_hand = hand
            logger.debug(f"DominantHand changed to {self.player_hand}")

    # ===================================================================
    # PLAYER — mining laser
    #
    # cGcLaserBeam.Fire is per-frame while the beam is active.
    # Hook body is minimal: just update a timestamp and start the loop once.
    # cGcPlayerWeapon.Update (narrower than cGcPlayer.Update) detects the
    # off-edge, but ONLY when we believe the laser is on — so it is
    # essentially a no-op 99% of the time.
    # ===================================================================

    @nms.cGcLaserBeam.Fire.after
    def on_laser_fire(self, this, lbHitOnFirstFrame):
        if self.is_in_spaceship:
            return
        self._laser_last_fire = time.perf_counter()
        if not self._laser_active:
            self._laser_active = True
            logger.debug("Laser ON")
            self.timers.start_pistol_laser()

    @nms.cGcPlayerWeapon.Update.after
    def on_weapon_update(self, this, lfTimeStep):
        # --- laser off-edge detection ---
        if self._laser_active:
            if time.perf_counter() - self._laser_last_fire > LASER_BEAM_COOLDOWN:
                self._laser_active = False
                logger.debug("Laser OFF")
                self.timers.stop_pistol_laser()

        # --- projectile / scatter / pulse weapon fire detection ---
        # Skip while mining laser is active or when in spaceship.
        if self._laser_active or self.is_in_spaceship or not this:
            return
        weapon = this.contents
        heat = float(weapon.mfHeatTime)
        delta = heat - self._prev_weapon_heat
        self._prev_weapon_heat = heat
        if delta > PLAYER_WEAPON_HEAT_THRESHOLD and not self._player_weapon_fired:
            self._player_weapon_fired = True
            if self.player_hand == 0:
                logger.debug("RightHandPistolShoot")
                self.suit.play_pattern("RightHandPistolShoot")
            else:
                logger.debug("LeftHandPistolShoot")
                self.suit.play_pattern("LeftHandPistolShoot")
        elif heat < 0.01:
            self._player_weapon_fired = False

    # ===================================================================
    # PLAYER — scanning
    #
    # UpdateScanBarProgress is only called while the scan bar is moving,
    # not on every global frame.  UpdateRayCasts fires when scan ends.
    # ===================================================================

    @nms.cGcBinoculars.UpdateScanBarProgress.after
    def on_scan_progress(self, this, lfScanProgress):
        # Play once when scan starts, not on every frame tick.
        # UpdateScanBarProgress is called each frame while scanning, so we
        # guard with a flag that resets when the scan ends.
        if not self._scan_active:
            self._scan_active = True
            logger.debug("Scan started")
            self.suit.play_pattern("Scanning")

    @nms.cGcBinoculars.UpdateRayCasts.after
    def on_scan_end(self, this, *args):
        self._scan_active = False
        logger.debug("Scan ended")

    # ===================================================================
    # PLAYER — multitool projectile weapons (all modes)
    #
    # cGcNetworkWeapon.FireRemote only matches boltcaster/laser.
    # Instead we read mfHeatTime from cGcPlayerWeapon in its Update hook
    # (which already runs for laser-off detection) — a heat spike above
    # a threshold means a shot was just fired, regardless of weapon mode.
    # We ignore this while the mining laser is active (handled separately).
    # ===================================================================
    # (projectile fire detection is integrated into on_weapon_update below)

    # ===================================================================
    # PLAYER — item collection
    # ===================================================================

    @nms.cGcRewardManager.GiveGenericReward.after
    def on_collect(self, this, lRewardID, lMissionID, lSeed, lbPeek,
                   lbForceShowMessage, liOutMultiProductCount, lbForceSilent,
                   lInventoryChoiceOverride, lbUseMiningModifier, _result_):
        if lbForceSilent or lbUseMiningModifier:
            logger.debug("CollectItem")
            self.suit.play_pattern("CollectItem")


    # ===================================================================
    # WORLD INTERACTIONS — DoInteractionEvent
    # ===================================================================

    _INTERACTION_PATTERNS: dict = {
        0x01: "InteractionShop",
        0x02: "InteractionNPC",
        0x03: "InteractionNPC",
        0x04: "InteractionNPC",
        0x05: "InteractionNPC",
        0x06: "InteractionShip",
        0x07: "InteractionTerminal",
        0x08: "InteractionTerminal",
        0x09: "InteractionTerminal",
        0x0A: "InteractionMonolith",
        0x0B: "InteractionTerminal",
        0x0C: "InteractionTerminal",
        0x0D: "InteractionTerminal",
        0x0E: "InteractionTerminal",
        0x0F: "InteractionTerminal",
        0x10: "InteractionTerminal",
        0x11: "InteractionPortal",
        0x12: "InteractionMonolith",
        0x13: "InteractionTerminal",
        0x14: "InteractionTerminal",
        0x15: "InteractionTerminal",
        0x16: "InteractionTerminal",
        0x17: "InteractionTerminal",
        0x18: "InteractionTeleporter",
        0x19: "InteractionTeleporter",
        0x1A: "InteractionTerminal",
        0x1B: "InteractionNPC",
        0x1C: "InteractionNPC",
        0x1D: "InteractionNPC",
        0x1E: "InteractionNPC",
        0x1F: "InteractionNPC",
        0x20: "InteractionNPC",
        0x21: "InteractionNPC",
        0x22: "InteractionNPC",
        0x23: "InteractionNPC",
        0x24: "InteractionTerminal",
        0x25: "InteractionNPC",
        0x26: "InteractionNPC",
        0x27: "InteractionNPC",
        0x28: "InteractionNPC",
        0x29: "InteractionNPC",
        0x2A: "InteractionShip",
        0x2B: "InteractionTerminal",
        0x2C: "InteractionShop",
        0x2D: "InteractionTerminal",
        0x2E: "InteractionShop",
        0x2F: "InteractionMission",
        0x30: "InteractionNPC",
        0x31: "InteractionNPC",
        0x32: "InteractionNPC",
        0x33: "InteractionNPC",
        0x34: "InteractionPortal",
        0x35: "InteractionPortal",
        0x36: "InteractionTerminal",
        0x37: "InteractionTerminal",
        0x38: "InteractionTerminal",
        0x39: "InteractionNPC",
        0x3A: "InteractionTerminal",
        0x3B: "InteractionTerminal",
        0x3C: "InteractionMonolith",
        0x3D: "InteractionTerminal",
        0x3E: "InteractionNPC",
        0x3F: "InteractionTerminal",
        0x40: "InteractionTerminal",
        0x41: "InteractionNPC",
        0x42: "InteractionTerminal",
        0x43: "InteractionTerminal",
        0x44: "InteractionTerminal",
        0x45: "InteractionTerminal",
        0x46: "InteractionTerminal",
        0x47: "InteractionTerminal",
        0x48: "InteractionTerminal",
        0x49: "InteractionTerminal",
        0x4A: "InteractionMission",
        0x4B: "InteractionShop",
        0x4C: "InteractionTerminal",
        0x4D: "InteractionTerminal",
        0x4E: "InteractionTerminal",
        0x4F: "InteractionNPC",
        0x50: "InteractionNPC",
        0x51: "InteractionTerminal",
        0x52: "InteractionNPC",
        0x53: "InteractionNPC",
        0x54: "InteractionTerminal",
        0x55: "InteractionNPC",
        0x56: "InteractionMission",
        0x57: "InteractionMission",
        0x58: "InteractionTerminal",
    }

    # NOTE: this currently fires while the player is merely gazing at an
    # interactable, not specifically when they press the interact button.
    # Our working theory (consistent with the values mapped above) is that
    # leEvent reports the TYPE of whatever is under the crosshair, and the
    # function gets called repeatedly while it stays in focus — not a
    # distinct "gaze" vs "confirm" event code.
    #
    # The debounce below stops the repeated buzz while continuously
    # looking at the same object, firing once per newly-focused target.
    # It does NOT yet distinguish "looking at" from "actually interacting".
    #
    # To look for a genuine "confirm pressed" signal: with debug logging
    # on, gaze at a few different objects without pressing anything (note
    # the ids logged), then walk up and actually press the interact
    # button — if a NEW id appears that wasn't already tied to a specific
    # object type, that may be the real confirm signal to filter for.
    @nms.cGcInteractionComponent.DoInteractionEvent.after
    def on_interaction_event(self, this, leEvent):
        event_id = int(leEvent)
        if event_id == self._last_interaction_event:
            return  # still gazing at the same target — suppress repeat
        self._last_interaction_event = event_id
        pattern = self._INTERACTION_PATTERNS.get(event_id, "InteractionEvent")
        logger.debug(f"InteractionEvent id={event_id} -> {pattern}")
        self.suit.play_pattern(pattern)

    # ===================================================================
    # SPACESHIP — cockpit entry / exit
    # ===================================================================

    @nms.cGcPlayer.OnEnteredCockpit.after
    def on_entered_cockpit(self, this):
        logger.debug("GetOnSpaceship")
        self.is_in_spaceship = True
        self.suit.play_pattern("GetOnSpaceship")

    @nms.cGcSpaceshipComponent.Eject.after
    def on_eject(self, this, *args):
        logger.debug("GetOffSpaceship")
        self.is_in_spaceship = False
        self.timers.stop_spacejump()
        self.suit.play_pattern("GetOffSpaceship")

    # ===================================================================
    # SPACESHIP — land state transitions (runs only while piloting)
    # ===================================================================

    _LANDED     = 3
    _TAKING_OFF = 10

    # cGcMissionConditionShipEngineStatus values we care about:
    _SHIP_BOOSTING  = 4
    _SHIP_PULSING   = 5

    @nms.cGcSpaceshipComponent.Update.after
    def on_ship_update(self, this: _Pointer[nms.cGcSpaceshipComponent], lfTimeStep):
        if not this:
            return
        ship  = this.contents
        state = int(ship.meLandState)

        if state == self._last_land_state:
            return
        prev, self._last_land_state = self._last_land_state, state

        if state == self._LANDED:
            logger.debug("SpaceshipOnGround")
            self.suit.play_pattern("SpaceshipOnGround")
        elif state == self._TAKING_OFF and prev == self._LANDED:
            logger.debug("SpaceshipTakeOff")
            self.suit.play_pattern("SpaceshipTakeOff")
        elif state == self._SHIP_BOOSTING and prev != self._SHIP_BOOSTING:
            logger.debug("SpaceshipBoost")
            self.suit.play_pattern("SpaceshipBoost")
        elif state == self._SHIP_PULSING and prev != self._SHIP_PULSING:
            # This transition (Update.after, the same safe hook used for
            # land state — NOT UpdatePulseDrive) appears to fire right as
            # the pulse drive engages. Earlier we assumed "pulse jump" was
            # a distinct mechanic from "pulse drive", but in NMS these are
            # colloquially the same thing — reusing the SpaceshipPulse
            # pattern here means it doubles as our "start" trigger,
            # complementing the existing GetPulseDriveFuelFactor-based
            # "stop" trigger below, with no risk of the movement-breaking
            # issue UpdatePulseDrive caused.
            logger.debug("PulseDrive start (via engine status)")
            self.suit.play_pattern("SpaceshipPulse")

    # ===================================================================
    # SPACESHIP — acceleration (ship-scoped, runs only while piloting)
    # ===================================================================

    @nms.cGcSpaceshipComponent.GetVelocity.after
    def on_get_velocity(self, this: _Pointer[nms.cGcSpaceshipComponent], result, *args):
        if not self.is_in_spaceship or not result:
            return
        v     = result.contents
        sq    = v.x * v.x + v.y * v.y + v.z * v.z
        delta = sq - self._prev_velocity_sq
        self._prev_velocity_sq = sq
        if delta > SHIP_ACCEL_THRESHOLD_SQ:
            logger.debug("SpaceshipSpeedUp")
            self.suit.play_pattern("SpaceshipSpeedUp")

    # ===================================================================
    # SPACESHIP - pulse drive (stop trigger)
    #
    # UpdatePulseDrive breaks player movement even on foot (confirmed by
    # direct testing), so it is never hooked. GetPulseDriveFuelFactor is
    # safe, but testing showed it fires once near the END of a pulse
    # jump rather than the start (likely only called while the HUD fuel
    # bar is being shown during recovery) — so this is kept as the STOP
    # trigger. The START trigger lives in on_ship_update above, using the
    # _SHIP_PULSING engine-status transition instead.
    #
    # Fuel sits at 1.0 at rest. The instant it drops below 0.99 we fire
    # one haptic burst. The flag resets when fuel recovers to 1.0 so the
    # next jump triggers again.
    # ===================================================================

    @nms.cGcSpaceshipWarp.GetPulseDriveFuelFactor.after
    def on_pulse_fuel(self, this, _result_):
        if not self.is_in_spaceship:
            return
        fuel = float(_result_)
        if fuel < 0.99 and not self._pulse_fired:
            self._pulse_fired = True
            logger.debug("PulseDrive stop (via fuel factor)")
            self.suit.play_pattern("SpaceshipPulse")
        elif fuel >= 1.0:
            self._pulse_fired = False

    # ===================================================================
    # SPACESHIP — weapons
    #
    # GetCurrentShootPoints was tried as an additional trigger but ruled
    # out by direct log evidence: it returned a non-null/truthy result on
    # essentially every frame for the entire flight, regardless of
    # whether the trigger was held — not tied to actual firing at all.
    # Reverted to GetHeatFactor only, even though that one is known to be
    # unreliable when shooting without hitting anything (the original
    # complaint). Worth raising with monkeyman192: a dedicated "weapon
    # fired" hook would be much more reliable than either of these.
    # ===================================================================

    def _trigger_ship_weapon_burst(self, source: str):
        now = time.perf_counter()
        if now - self._ship_fire_last_trigger < SHIP_FIRE_DEBOUNCE:
            logger.debug(f"ShipWeapons burst from {source} suppressed (debounce)")
            return
        self._ship_fire_last_trigger = now
        logger.debug(f"ShipWeapons fired (source={source})")
        self.suit.play_pattern("SpaceshipWeaponShoot")

    @nms.cGcSpaceshipWeapons.GetHeatFactor.after
    def on_ship_heat(self, this, _result_):
        if not self.is_in_spaceship:
            return
        heat  = float(_result_)
        delta = heat - self._prev_heat
        self._prev_heat = heat
        if delta > SHIP_WEAPON_HEAT_THRESHOLD:
            self._trigger_ship_weapon_burst("heat")


# ---------------------------------------------------------------------------
# Directional damage helper
# ---------------------------------------------------------------------------

def _dir_to_rotation(dx: float, dz: float) -> float:
    """
    Convert an XZ damage-direction vector to a bhaptics rotation in degrees
    (counterclockwise, 0-360, used as x_offset in play_param).

    lDir points TOWARD the attacker (i.e. away from the player), so:
      +Z = attacker is in front  -> front of vest (0 deg)
      -Z = attacker is behind    -> back of vest  (180 deg)
      +X = attacker is to right  -> right side    (270 deg)
      -X = attacker is to left   -> left side     (90 deg)

    NMS axes: +X = right, +Z = forward.
    bhaptics x_offset: counterclockwise, 0 = front, 90 = left,
                       180 = back, 270 = right.

    Formula: atan2(-nx, nz) gives a CCW angle from front in [-180, 180],
    mod 360 maps it to [0, 360).
    """
    mag = (dx * dx + dz * dz) ** 0.5
    if mag < 0.1:
        return 0.0
    nx, nz = dx / mag, dz / mag
    rotation = math.degrees(math.atan2(-nx, nz)) % 360.0
    return rotation


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from pymhf import load_mod_file
    load_mod_file(__file__)