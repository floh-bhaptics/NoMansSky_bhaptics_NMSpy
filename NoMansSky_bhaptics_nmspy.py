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
  - Weapon fire (multitool, all modes, AND spaceship) is caught by a single
    cGcNetworkWeapon.FireRemote hook — a real fire event, not a heuristic.
  - Scanning, pulse-drive start/stop, ship takeoff/landing are all driven
    by discrete cTkAudioManager.Play audio cues rather than polling UI
    update functions or struct fields — proven reliable in the original
    pyMHF version of this mod.
  - UpdatePulseDrive is never hooked — confirmed by direct testing to
    break player movement, cause unknown.
  - Remaining spaceship hooks (Update for boost, GetVelocity) are
    acceptable because they only run while piloting a ship.

Haptic patterns used:
  heartbeat / PlayerDeath / FallDamage
  DamageFront / DamageBack / DamageLeft / DamageRight / DefaultDamage
  RightHandPistolShoot / LeftHandPistolShoot
  RightHandPistolLaserShoot / LeftHandPistolLaserShoot  (looping)
  Scanning (held-button scan, one-shot per hold) / ScanWave (audio pulse)
  CollectItem
  GetOnSpaceship / GetOffSpaceship
  SpaceshipTakeOff / SpaceshipOnGround / SpaceshipBoost
  SpaceshipSpeedUp / SpaceshipPulse (looping) / SpaceshipWeaponShoot
"""

import ctypes
import logging
import math
import time
from ctypes import _Pointer

from pymhf import Mod
from pymhf.core.hooking import function_hook, Structure

import nmspy.data.types as nms

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
LASER_BEAM_COOLDOWN        = 0.25   # seconds of no Fire call before beam is "off"
SCAN_HOLD_COOLDOWN         = 0.30   # seconds of no progress call before a new scan-hold counts as fresh

# ---------------------------------------------------------------------------
# Audio event IDs (cTkAudioManager.Play / TkAudioID.muID)
#
# These came from the original, proven-working pyMHF version of this mod —
# discrete one-shot audio cues turn out to be far more reliable signals
# than polling heat factors, fuel factors, or UI update functions.
# ---------------------------------------------------------------------------
AUDIO_ID_SCAN_WAVE        = 2149772978
AUDIO_ID_SHIP_ON_GROUND   = 3903008093
AUDIO_ID_SHIP_TAKEOFF     = 514090887
AUDIO_ID_START_SPACEJUMP  = 1261594536
AUDIO_ID_STOP_SPACEJUMP_1 = 1511168854
AUDIO_ID_STOP_SPACEJUMP_2 = 2852869421

# ---------------------------------------------------------------------------
# cGcNetworkWeapon — not yet in NMS.py, define locally with raw byte pattern.
# This single hook reliably catches weapon fire for ALL multitool modes
# AND spaceship weapons — confirmed working in the original pyMHF version
# of this mod, unlike the heat/shootpoint-based approaches tried earlier
# today which both turned out to be unreliable proxies.
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

        # scan (held button, distinct from the ScanWave audio pulse)
        self._scan_last_progress: float = 0.0

        # ship
        self._last_land_state: int = -1
        self._prev_velocity_sq: float = 0.0

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
        # --- laser off-edge detection only; projectile fire is handled
        # by cGcNetworkWeapon.FireRemote below ---
        if self._laser_active:
            if time.perf_counter() - self._laser_last_fire > LASER_BEAM_COOLDOWN:
                self._laser_active = False
                logger.debug("Laser OFF")
                self.timers.stop_pistol_laser()

    # ===================================================================
    # PLAYER — scan (held button)
    #
    # This is a DIFFERENT mechanic from the ScanWave audio pulse below.
    # UpdateScanBarProgress fires every frame for as long as the scan
    # button is held — not a loop bug, that's genuinely how often it's
    # called. The old _scan_active/UpdateRayCasts approach reset
    # unreliably (UpdateRayCasts isn't a clean "scan ended" signal), so
    # this uses the same time-based cooldown trick that works well for
    # the laser: only fire once per "hold", determined by a gap since
    # the last progress call rather than relying on an explicit end event.
    # ===================================================================

    @nms.cGcBinoculars.UpdateScanBarProgress.after
    def on_scan_progress(self, this, lfScanProgress):
        now = time.perf_counter()
        if now - self._scan_last_progress > SCAN_HOLD_COOLDOWN:
            logger.debug("Scan hold started")
            self.suit.play_pattern("Scanning")
        self._scan_last_progress = now

    # ===================================================================
    # WEAPON FIRE — cGcNetworkWeapon.FireRemote
    #
    # Catches the actual fire RPC for ALL multitool modes (boltcaster,
    # scatter blaster, pulse splitter, etc.) and spaceship weapons in one
    # place. Mining laser is excluded here since it's handled by the
    # separate continuous cGcLaserBeam.Fire loop above.
    # ===================================================================

    @cGcNetworkWeapon.FireRemote.after
    def on_fire_remote(self, this):
        if self._laser_active:
            return
        if self.is_in_spaceship:
            logger.debug("SpaceshipWeaponShoot (FireRemote)")
            self.suit.play_pattern("SpaceshipWeaponShoot")
        elif self.player_hand == 0:
            logger.debug("RightHandPistolShoot (FireRemote)")
            self.suit.play_pattern("RightHandPistolShoot")
        else:
            logger.debug("LeftHandPistolShoot (FireRemote)")
            self.suit.play_pattern("LeftHandPistolShoot")

    # ===================================================================
    # PLAYER — scanning
    #
    # Previously hooked UpdateScanBarProgress/UpdateRayCasts, which fired
    # repeatedly throughout a scan (not just once), effectively behaving
    # like an unwanted loop. Replaced with the ScanWave audio cue — a
    # genuine one-shot signal for "scan activated", handled in the
    # cTkAudioManager.Play dispatcher below.
    # ===================================================================

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
    # SPACESHIP — boost transition (runs only while piloting)
    #
    # Takeoff/landing and pulse-drive start/stop used to be detected here
    # via meLandState polling, but have been replaced by the more
    # reliable audio cues (cTkAudioManager.Play dispatcher below). Only
    # the boost transition remains on this mechanism since no audio ID
    # for it has been identified yet.
    # ===================================================================

    # cGcMissionConditionShipEngineStatus value for "Boosting":
    _SHIP_BOOSTING  = 4

    @nms.cGcSpaceshipComponent.Update.after
    def on_ship_update(self, this: _Pointer[nms.cGcSpaceshipComponent], lfTimeStep):
        if not this:
            return
        ship  = this.contents
        state = int(ship.meLandState)

        if state == self._last_land_state:
            return
        prev, self._last_land_state = self._last_land_state, state

        if state == self._SHIP_BOOSTING and prev != self._SHIP_BOOSTING:
            logger.debug("SpaceshipBoost")
            self.suit.play_pattern("SpaceshipBoost")

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
    # AUDIO EVENTS — cTkAudioManager.Play
    #
    # Discrete one-shot audio cues, proven reliable in the original
    # pyMHF version of this mod. This replaces three separate heuristic
    # approaches tried earlier today (UpdatePulseDrive, which broke
    # player movement; GetPulseDriveFuelFactor and meLandState-based
    # engine-status polling, which both had start/stop timing issues).
    #
    # Pulse drive now gets a PROPER continuous loop again: the SDK loop
    # itself is fine, the problem before was always how we detected
    # start/stop, never the loop mechanism. These two clean audio events
    # give us an exact start/stop boundary to drive it correctly.
    # ===================================================================

    @nms.cTkAudioManager.Play.after
    def on_audio_play(self, this, event, object, _result_=None):
        try:
            audio_id = event.contents.muID
        except Exception:
            return

        if audio_id == AUDIO_ID_SCAN_WAVE:
            logger.debug("ScanWave (audio)")
            self.suit.play_pattern("ScanWave")

        elif audio_id == AUDIO_ID_START_SPACEJUMP:
            logger.debug("PulseDrive start (audio)")
            self.timers.start_spacejump()

        elif audio_id in (AUDIO_ID_STOP_SPACEJUMP_1, AUDIO_ID_STOP_SPACEJUMP_2):
            logger.debug("PulseDrive stop (audio)")
            self.timers.stop_spacejump()

        elif audio_id == AUDIO_ID_SHIP_TAKEOFF:
            logger.debug("SpaceshipTakeOff (audio)")
            self.suit.play_pattern("SpaceshipTakeOff")

        elif audio_id == AUDIO_ID_SHIP_ON_GROUND:
            logger.debug("SpaceshipOnGround (audio)")
            self.suit.play_pattern("SpaceshipOnGround")


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