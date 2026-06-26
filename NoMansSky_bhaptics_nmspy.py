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
import time

from pymhf import Mod
from pymhf.core.hooking import function_hook, Structure
from pymhf.core.memutils import map_struct, get_addressof

import nmspy.data.types as nms

from bhaptics_library import bhaptics_suit, TimerController

logger = logging.getLogger("NMS_bhaptics")

# ---------------------------------------------------------------------------
# bHaptics credentials
# ---------------------------------------------------------------------------
BHAPTICS_APP_ID   = "693ac4ffa277918a719a1bd8"
BHAPTICS_API_KEY  = "uSEDPxsVOpRefEGM7FAc"
BHAPTICS_APP_NAME = "No Mans Sky"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
SHIP_ACCEL_THRESHOLD_SQ    = 2500   # speed-sq delta to trigger SpaceshipSpeedUp
SHIP_WEAPON_HEAT_THRESHOLD = 0.02   # heat-factor rise per frame = "firing"
SHIP_WEAPON_COOLDOWN       = 0.35   # seconds of no heat rise before loop stops
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
        self._ship_weapon_active: bool = False
        self._pulse_fired: bool = False

        # multitool projectile fire detection
        self._prev_weapon_heat: float = 0.0
        self._player_weapon_fired: bool = False

        # scanner
        self._scan_active: bool = False

        # --- connect bHaptics ---
        # bhaptics_suit.__init__ starts a background thread and returns immediately
        # — no sleep, no blocking the game thread.
        logger.info("Initialising bHaptics suit…")
        self.suit = bhaptics_suit(
            app_id=BHAPTICS_APP_ID,
            api_key=BHAPTICS_API_KEY,
            app_name=BHAPTICS_APP_NAME,
        )
        self.timers = TimerController(self)
        logger.info("bHaptics suit initialised (connecting in background…)")

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
            logger.info("FallDamage")
            self.suit.play_pattern("FallDamage")
            return

        try:
            d = lDir.contents
            rotation = _dir_to_rotation(d.x, d.z)
        except Exception:
            rotation = 0.0
        logger.info(f"Damage id={damage_id} type={leDamageType} rotation={rotation:.0f}deg")
        self.suit.play_damage("DefaultDamage", rotation)

    # ===================================================================
    # PLAYER — dominant hand (called rarely)
    # ===================================================================

    @nms.cGcPlayer.GetDominantHand.after
    def on_dominant_hand(self, this, *args, _result_):
        self.player_hand = int(_result_)
        logger.debug(f"DominantHand={self.player_hand}")

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
        if self._laser_active or self.is_in_spaceship:
            return
        try:
            weapon = map_struct(get_addressof(this), nms.cGcPlayerWeapon)
            heat = float(weapon.mfHeatTime)
        except Exception:
            return
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
    #
    # Fires whenever the player completes a world interaction (NPC
    # dialogue, terminal, shop, etc.).  leEvent is a raw uint32 because
    # eInteractionEvent is not yet mapped in NMS.py.
    #
    # All observed values are logged at INFO level so you can discover
    # new ones in-game.  Known/guessed values are mapped to named
    # patterns below; everything else falls back to "InteractionEvent".
    #
    # To identify a new value: trigger the interaction, note the integer
    # in the log, then add it to _INTERACTION_PATTERNS below.
    # ===================================================================

    # Maps raw eInteractionEvent uint32 values to bhaptics pattern names.
    # Values marked (UNVERIFIED) are community best-guesses; confirm in-game.
    _INTERACTION_PATTERNS: dict = {
        0:  "InteractionGreeting",       # greeting / initial contact   (UNVERIFIED)
        1:  "InteractionDialogue",       # NPC dialogue line            (UNVERIFIED)
        2:  "InteractionAccept",         # accept / confirm choice      (UNVERIFIED)
        3:  "InteractionReward",         # reward given                 (UNVERIFIED)
        4:  "InteractionDecline",        # decline / back out           (UNVERIFIED)
        5:  "InteractionShop",           # open shop                    (UNVERIFIED)
        6:  "InteractionMission",        # mission accept / complete    (UNVERIFIED)
        7:  "InteractionTerminal",       # activate terminal / station  (UNVERIFIED)
        8:  "InteractionScan",           # scan / analyse object        (UNVERIFIED)
        9:  "InteractionCraft",          # craft item                   (UNVERIFIED)
        10: "InteractionInstall",        # install technology           (UNVERIFIED)
        11: "InteractionRepair",         # repair item                  (UNVERIFIED)
        12: "InteractionRefuel",         # refuel / recharge            (UNVERIFIED)
    }

    @nms.cGcInteractionComponent.DoInteractionEvent.after
    def on_interaction_event(self, this, leEvent):
        event_id = int(leEvent)
        pattern = self._INTERACTION_PATTERNS.get(event_id, "InteractionEvent")
        logger.info(f"InteractionEvent id={event_id} -> {pattern}")
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
        self.timers.stop_ship_weapons()
        self.timers.stop_spacejump()
        self._ship_weapon_active = False
        self.suit.play_pattern("GetOffSpaceship")

    # ===================================================================
    # SPACESHIP — land state transitions (runs only while piloting)
    # ===================================================================

    _LANDED     = 3
    _TAKING_OFF = 10

    # cGcMissionConditionShipEngineStatus values we care about:
    _SHIP_BOOSTING  = 4
    _SHIP_PULSING   = 5   # pulse jump (different from pulse drive)

    @nms.cGcSpaceshipComponent.Update.after
    def on_ship_update(self, this, lfTimeStep):
        try:
            ship  = map_struct(get_addressof(this), nms.cGcSpaceshipComponent)
            state = int(ship.meLandState)
        except Exception:
            return

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
            logger.debug("SpaceshipPulseJump")
            self.suit.play_pattern("SpaceshipPulseJump")

    # ===================================================================
    # SPACESHIP — acceleration (ship-scoped, runs only while piloting)
    # ===================================================================

    @nms.cGcSpaceshipComponent.GetVelocity.after
    def on_get_velocity(self, this, result, *args):
        if not self.is_in_spaceship:
            return
        try:
            v     = result.contents
            sq    = v.x * v.x + v.y * v.y + v.z * v.z
            delta = sq - self._prev_velocity_sq
            self._prev_velocity_sq = sq
            if delta > SHIP_ACCEL_THRESHOLD_SQ:
                logger.debug("SpaceshipSpeedUp")
                self.suit.play_pattern("SpaceshipSpeedUp")
        except Exception:
            pass

    # ===================================================================
    # SPACESHIP - pulse drive
    #
    # UpdatePulseDrive breaks player movement even on foot, so we use
    # GetPulseDriveFuelFactor instead (ship-scoped and safe).
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
            logger.debug("PulseDrive start")
            self.suit.play_pattern("SpaceshipPulse")
        elif fuel >= 1.0:
            self._pulse_fired = False

    # ===================================================================
    # SPACESHIP — weapons
    #
    # GetHeatFactor is called every frame while in a ship.
    # We detect the rising edge (heat increases) and play one burst.
    # A cooldown flag prevents re-triggering until heat settles back down,
    # which avoids both the stuttering loop and the never-stops problem.
    # ===================================================================

    @nms.cGcSpaceshipWeapons.GetHeatFactor.after
    def on_ship_heat(self, this, _result_):
        if not self.is_in_spaceship:
            return
        heat  = float(_result_)
        delta = heat - self._prev_heat
        self._prev_heat = heat

        if delta > SHIP_WEAPON_HEAT_THRESHOLD and not self._ship_weapon_active:
            self._ship_weapon_active = True
            logger.debug("ShipWeapons fired")
            self.suit.play_pattern("SpaceshipWeaponShoot")
        elif heat < 0.01:
            # Heat fully dissipated — ready to fire again
            self._ship_weapon_active = False


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
    import math
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