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

import asyncio
import ctypes
import logging
import time

from pymhf import Mod
from pymhf.core.hooking import function_hook, Structure
from pymhf.core.memutils import map_struct, get_addressof

import nmspy.data.types as nms
import nmspy.data.enums as enums

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

        # --- connect bHaptics ---
        logger.info("Initialising bHaptics suit…")
        time.sleep(5)
        self.suit = bhaptics_suit(
            app_id=BHAPTICS_APP_ID,
            api_key=BHAPTICS_API_KEY,
            app_name=BHAPTICS_APP_NAME,
        )
        asyncio.run(self.suit.connect())
        self.timers = TimerController(self)
        logger.info("bHaptics suit ready.")

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
        if leDamageType == enums.cGcDamageType.PlayerDamage:
            logger.debug("FallDamage")
            self.suit.play_pattern("FallDamage")
            return
        try:
            d = lDir.contents
            pattern = _dir_pattern(d.x, d.z)
        except Exception:
            pattern = "DefaultDamage"
        logger.debug(f"Damage type={leDamageType} pattern={pattern}")
        self.suit.play_pattern(pattern)

    # ===================================================================
    # PLAYER — dominant hand (called rarely)
    # ===================================================================

    @nms.cGcPlayer.GetDominantHand.after
    def on_dominant_hand(self, this, *args, _result_):
        self.player_hand = int(_result_)
        # logger.debug(f"DominantHand={self.player_hand}")

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
        # Fast exit when laser is not firing — this hook becomes a no-op.
        if not self._laser_active:
            return
        if time.perf_counter() - self._laser_last_fire > LASER_BEAM_COOLDOWN:
            self._laser_active = False
            logger.debug("Laser OFF")
            self.timers.stop_pistol_laser()

    # ===================================================================
    # PLAYER — scanning
    #
    # UpdateScanBarProgress is only called while the scan bar is moving,
    # not on every global frame.  UpdateRayCasts fires when scan ends.
    # ===================================================================

    @nms.cGcBinoculars.UpdateScanBarProgress.after
    def on_scan_progress(self, this, lfScanProgress):
        if not self.timers.scan_running:
            logger.debug("Scan started")
            self.timers.start_scan()

    @nms.cGcBinoculars.UpdateRayCasts.after
    def on_scan_end(self, this, *args):
        if self.timers.scan_running:
            logger.debug("Scan ended")
            self.timers.stop_scan()

    # ===================================================================
    # PLAYER — single-shot pistol
    # ===================================================================

    @cGcNetworkWeapon.FireRemote.after
    def on_fire_remote(self, this, *args):
        if self._laser_active or self.is_in_spaceship:
            return
        if self.player_hand == 0:
            logger.debug("RightHandPistolShoot")
            self.suit.play_pattern("RightHandPistolShoot")
        else:
            logger.debug("LeftHandPistolShoot")
            self.suit.play_pattern("LeftHandPistolShoot")

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
    # SPACESHIP — pulse drive (ship-scoped)
    # ===================================================================

    _PULSE_ON = (
        int(enums.EPulseDriveState.Charge),
        int(enums.EPulseDriveState.Jumping),
    )

    @nms.cGcSpaceshipWarp.UpdatePulseDrive.after
    def on_pulse_drive(self, this, *args):
        try:
            warp  = map_struct(get_addressof(this), nms.cGcSpaceshipWarp)
            state = int(warp.mePulseDriveState)
        except Exception:
            return

        if state in self._PULSE_ON:
            if not self.timers.spacejump_running:
                logger.debug("PulseDrive ON")
                self.timers.start_spacejump()
        else:
            if self.timers.spacejump_running:
                logger.debug("PulseDrive OFF")
                self.timers.stop_spacejump()

    # ===================================================================
    # SPACESHIP — weapons / heat (ship-scoped, per-frame but acceptable)
    # ===================================================================

    @nms.cGcSpaceshipWeapons.GetHeatFactor.after
    def on_ship_heat(self, this, _result_):
        if not self.is_in_spaceship:
            return
        now   = time.perf_counter()
        heat  = float(_result_)
        delta = heat - self._prev_heat
        self._prev_heat = heat

        if delta > SHIP_WEAPON_HEAT_THRESHOLD:
            self._heat_last_rise = now
            if not self._ship_weapon_active:
                self._ship_weapon_active = True
                logger.debug("ShipWeapons ON")
                self.timers.start_ship_weapons()
        elif self._ship_weapon_active:
            if now - self._heat_last_rise > SHIP_WEAPON_COOLDOWN:
                self._ship_weapon_active = False
                logger.debug("ShipWeapons OFF")
                self.timers.stop_ship_weapons()


# ---------------------------------------------------------------------------
# Directional damage helper
# ---------------------------------------------------------------------------

def _dir_pattern(dx: float, dz: float) -> str:
    mag = (dx * dx + dz * dz) ** 0.5
    if mag < 0.1:
        return "DefaultDamage"
    nx, nz = dx / mag, dz / mag
    if abs(nz) >= abs(nx):
        return "DamageBack" if nz > 0 else "DamageFront"
    return "DamageRight" if nx > 0 else "DamageLeft"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from pymhf import load_mod_file
    load_mod_file(__file__)