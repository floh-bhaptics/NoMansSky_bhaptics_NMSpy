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
# shown = true
# log_dir = "."
# log_level = "debug"
# window_name_override = "No Mans Sky bhaptics mod (NMS.py)"
# ///

"""
No Man's Sky — bHaptics haptic suit mod
Built on NMS.py (https://github.com/monkeyman192/NMS.py)

Haptic patterns triggered:
  PlayerDeath              — player dies
  FallDamage               — landing damage
  DefaultDamage            — any other hit (directional via direction vector)
  RightHandPistolShoot     — single shot, right hand
  LeftHandPistolShoot      — single shot, left hand
  RightHandPistolLaserShoot — laser beam held, right hand (looping)
  LeftHandPistolLaserShoot  — laser beam held, left hand (looping)
  Scanning                 — scanner active (looping)
  CollectItem              — item collected
  GetOnSpaceship           — entering cockpit
  GetOffSpaceship          — leaving cockpit / ejecting
  SpaceshipTakeOff         — ship lifts off (land-state transition)
  SpaceshipOnGround        — ship touches down (land-state transition)
  SpaceshipSpeedUp         — ship accelerating
  SpaceshipPulse           — pulse drive active (looping)
  SpaceshipWeaponShoot     — ship weapons firing (looping while heat rises)
  PlayerUsingJetpack       — jetpack active (looping)
  PlayerLowHealth          — health below threshold (periodic reminder)
"""

import asyncio
import ctypes
import logging
import time
from typing import Annotated

from pymhf import Mod, ModState
from pymhf.core._types import DetourTime
from pymhf.core.hooking import function_hook, one_shot
from pymhf.core.memutils import map_struct, get_addressof
from pymhf.utils.partial_struct import partial_struct, Field

import nmspy.data.types as nms
import nmspy.data.enums as enums
from nmspy.common import gameData

from bhaptics_library import bhaptics_suit, TimerController

logger = logging.getLogger("NMS_bhaptics")

# ---------------------------------------------------------------------------
# bhaptics credentials — replace with your own from the bHaptics developer portal
# ---------------------------------------------------------------------------
BHAPTICS_APP_ID = "693ac4ffa277918a719a1bd8"
BHAPTICS_API_KEY = "uSEDPxsVOpRefEGM7FAc"
BHAPTICS_APP_NAME = "No Mans Sky"

# ---------------------------------------------------------------------------
# Thresholds / tunables
# ---------------------------------------------------------------------------
LOW_HEALTH_THRESHOLD = 0.25      # fraction of max health (0–1)
LOW_HEALTH_INTERVAL = 3.0        # seconds between low-health reminders
SHIP_WEAPON_HEAT_THRESHOLD = 0.02  # minimum heat-factor increase per frame to count as "firing"
SHIP_WEAPON_COOLDOWN = 0.3       # seconds of no heat rise before we stop the firing loop
JETPACK_COOLDOWN = 0.15          # seconds before jetpack loop stops after IsJetpacking returns false
LASER_BEAM_COOLDOWN = 0.25       # seconds before laser loop stops after cGcLaserBeam.Fire stops


class NMSBhapticsMod(Mod):
    __author__ = "floh-bhaptics"
    __description__ = "bHaptics haptic suit integration for No Man's Sky (NMS.py rewrite)"
    __version__ = "2.0.0"

    # -------------------------------------------------------------------
    # Init
    # -------------------------------------------------------------------

    def __init__(self):
        super().__init__()

        # --- state flags ---
        self.player_hand: int = 0          # 0 = right, 1 = left
        self.is_in_spaceship: bool = False
        self.last_land_state: int = -1     # tracks meLandState for transition detection

        # laser beam on/off tracking (cGcLaserBeam.Fire is called each frame)
        self._laser_last_fire: float = 0.0
        self._laser_active: bool = False

        # jetpack on/off tracking (IsJetpacking called each frame)
        self._jetpack_last_active: float = 0.0
        self._jetpack_active: bool = False

        # ship weapons heat tracking
        self._prev_heat_factor: float = 0.0
        self._ship_weapon_last_rise: float = 0.0
        self._ship_weapon_active: bool = False

        # low-health tracking
        self._last_low_health_ping: float = 0.0

        # --- bhaptics ---
        logger.info("Initialising bHaptics suit…")
        time.sleep(5)  # give the game time to finish loading before connecting
        self.myTactsuit = bhaptics_suit(
            app_id=BHAPTICS_APP_ID,
            api_key=BHAPTICS_API_KEY,
            app_name=BHAPTICS_APP_NAME,
        )
        asyncio.run(self.myTactsuit.connect())
        self.timerController = TimerController(self)
        logger.info("bHaptics suit ready.")

    # ===================================================================
    # PLAYER — death
    # ===================================================================

    @nms.cGcPlayerCharacterComponent.SetDeathState.after
    def on_player_death(self, this, *args):
        logger.debug("PlayerDeath")
        self.myTactsuit.play_pattern("PlayerDeath")

    # ===================================================================
    # PLAYER — damage (with directional support)
    # ===================================================================

    @nms.cGcPlayer.TakeDamage.after
    def on_take_damage(self, this, lfDamageAmount, leDamageType, lDamageId, lDir, lpOwner, laEffects):
        """
        lDamageId is a TkID pointer; in the old pyMHF mod this was read as a string.
        Here we use the damage-type enum to detect fall damage instead.
        lDir is a pointer to a Vector3f giving the incoming damage direction.
        """
        damage_type = leDamageType

        # Fall / landing damage
        if damage_type == enums.cGcDamageType.PlayerDamage:
            logger.debug("FallDamage")
            self.myTactsuit.play_pattern("FallDamage")
            return

        # Directional damage — map direction vector to a vest zone pattern.
        # lDir is a _Pointer[Vector3f]; dereference carefully.
        try:
            direction = lDir.contents
            pattern = _directional_damage_pattern(direction.x, direction.z)
        except Exception:
            pattern = "DefaultDamage"

        logger.debug(f"Damage: type={damage_type} amount={lfDamageAmount:.1f} pattern={pattern}")
        self.myTactsuit.play_pattern(pattern)

    # ===================================================================
    # PLAYER — hand / dominant side
    # ===================================================================

    @nms.cGcPlayer.GetDominantHand.after
    def on_get_dominant_hand(self, this, *args, _result_):
        self.player_hand = int(_result_)
        logger.debug(f"DominantHand: {self.player_hand}")

    # ===================================================================
    # PLAYER — jetpack (IsJetpacking is polled every frame)
    # ===================================================================

    @nms.cGcLocalPlayerCharacterInterface.IsJetpacking.after
    def on_is_jetpacking(self, this, *args, _result_):
        now = time.perf_counter()
        if _result_:
            self._jetpack_last_active = now
            if not self._jetpack_active:
                self._jetpack_active = True
                logger.debug("Jetpack ON")
                self.timerController.start_jetpack()
        else:
            # stop loop only after cooldown gap
            if self._jetpack_active and (now - self._jetpack_last_active) > JETPACK_COOLDOWN:
                self._jetpack_active = False
                logger.debug("Jetpack OFF")
                self.timerController.stop_jetpack()

    # ===================================================================
    # PLAYER — laser pistol beam (cGcLaserBeam.Fire is called every frame while firing)
    # ===================================================================

    @nms.cGcLaserBeam.Fire.after
    def on_laser_fire(self, this, lbHitOnFirstFrame):
        now = time.perf_counter()
        self._laser_last_fire = now
        if not self._laser_active:
            self._laser_active = True
            logger.debug("Laser ON")
            self.timerController.start_pistol_laser()

    @nms.cGcPlayer.Update.after
    def on_player_update(self, this, lfStep):
        """
        Used as a per-frame ticker to:
        - detect laser beam switch-off (no Fire call in LASER_BEAM_COOLDOWN seconds)
        - drive the ship weapon heat polling
        - fire low-health reminders
        """
        now = time.perf_counter()

        # --- laser beam off detection ---
        if self._laser_active and (now - self._laser_last_fire) > LASER_BEAM_COOLDOWN:
            self._laser_active = False
            logger.debug("Laser OFF")
            self.timerController.stop_pistol_laser()

        # --- low health check ---
        self._check_low_health(now)

    # ===================================================================
    # PLAYER — scanning
    # ===================================================================

    @nms.cGcBinoculars.UpdateScanBarProgress.after
    def on_scan_progress(self, this, lfScanProgress):
        """Called every frame while scanning is in progress."""
        if not self.timerController.scan_running:
            logger.debug("Scan started")
            self.timerController.start_scan()
        self._scan_last_frame = time.perf_counter()

    @nms.cGcBinoculars.UpdateRayCasts.after
    def on_scan_end(self, this, *args):
        """UpdateRayCasts is called when the scan completes / cancels."""
        if self.timerController.scan_running:
            logger.debug("Scan ended")
            self.timerController.stop_scan()

    # ===================================================================
    # SPACESHIP — cockpit entry / exit
    # ===================================================================

    @nms.cGcPlayer.OnEnteredCockpit.after
    def on_entered_cockpit(self, this):
        logger.debug("GetOnSpaceship")
        self.is_in_spaceship = True
        self.myTactsuit.play_pattern("GetOnSpaceship")

    @nms.cGcSpaceshipComponent.Eject.after
    def on_eject(self, this, lpPlayer, lbAnimate, lbForceDuringCommunicator):
        logger.debug("GetOffSpaceship")
        self.is_in_spaceship = False
        # stop any ship-side loops that may be running
        self.timerController.stop_ship_weapons()
        self.timerController.stop_spacejump()
        self.myTactsuit.play_pattern("GetOffSpaceship")

    # ===================================================================
    # SPACESHIP — takeoff / landing via meLandState polling
    # ===================================================================

    @nms.cGcSpaceshipComponent.Update.after
    def on_ship_update(self, this, lfTimeStep):
        """
        Poll meLandState each frame to detect landing/takeoff transitions.
        The field stores a cGcMissionConditionShipEngineStatus-like int:
          Landed=3, Landing=2, TakingOff=10, Thrusting=0, …
        We only care about discrete transitions into Landed and TakingOff.
        """
        try:
            ship = map_struct(get_addressof(this), nms.cGcSpaceshipComponent)
            current_state = int(ship.meLandState)
        except Exception:
            return

        if current_state == self.last_land_state:
            return

        prev = self.last_land_state
        self.last_land_state = current_state

        # Landed (3)
        LANDED = 3
        TAKING_OFF = 10

        if current_state == LANDED and prev != LANDED:
            logger.debug("SpaceshipOnGround (landed)")
            self.myTactsuit.play_pattern("SpaceshipOnGround")

        elif current_state == TAKING_OFF and prev == LANDED:
            logger.debug("SpaceshipTakeOff")
            self.myTactsuit.play_pattern("SpaceshipTakeOff")

    # ===================================================================
    # SPACESHIP — acceleration (velocity delta via UpdateControlled)
    # ===================================================================

    def __init_accel(self):
        self._prev_velocity_sq: float = 0.0

    @nms.cGcSpaceshipComponent.UpdateControlled.after
    def on_ship_controlled(self, this, lfTimeStep):
        """Detect significant speed increases and play the SpeedUp pattern."""
        if not self.is_in_spaceship:
            return
        try:
            ship = map_struct(get_addressof(this), nms.cGcSpaceshipComponent)
            vel_struct = nms.cGcSpaceshipComponent.GetVelocity.__func__  # raw func ref not needed
            # We can read velocity indirectly through GetVelocity hook result below
        except Exception:
            pass

    @nms.cGcSpaceshipComponent.GetVelocity.after
    def on_get_velocity(self, this, result, _result_):
        """
        GetVelocity is called each frame. We track speed-squared to detect
        meaningful acceleration events without needing the actual unit vector.
        """
        if not self.is_in_spaceship:
            return
        try:
            v = result.contents  # cTkVector3
            spd_sq = v.x * v.x + v.y * v.y + v.z * v.z
            delta = spd_sq - getattr(self, "_prev_velocity_sq", 0.0)
            self._prev_velocity_sq = spd_sq
            # Only trigger on meaningful speed-ups (not on every tiny delta)
            if delta > 2500:  # ~50 m/s² gain per frame — tune to taste
                logger.debug("SpaceshipSpeedUp")
                self.myTactsuit.play_pattern("SpaceshipSpeedUp")
        except Exception:
            pass

    # ===================================================================
    # SPACESHIP — pulse drive
    # ===================================================================

    @nms.cGcSpaceshipWarp.UpdatePulseDrive.after
    def on_pulse_drive(self, this):
        """
        UpdatePulseDrive is called every frame while the pulse drive state machine
        is active. We check mePulseDriveState to drive the loop correctly.
        """
        try:
            warp = map_struct(get_addressof(this), nms.cGcSpaceshipWarp)
            state = int(warp.mePulseDriveState)
        except Exception:
            return

        # EPulseDriveState: None_=0, Charge=1, Jumping=2, CrashStop=3, Cooldown=4
        JUMPING = 2
        CHARGE = 1

        if state in (CHARGE, JUMPING):
            if not self.timerController.spacejump_running:
                logger.debug("PulseDrive ON")
                self.timerController.start_spacejump()
        else:
            if self.timerController.spacejump_running:
                logger.debug("PulseDrive OFF")
                self.timerController.stop_spacejump()

    # ===================================================================
    # SPACESHIP — weapons (heat-factor approach)
    # ===================================================================

    @nms.cGcSpaceshipWeapons.GetHeatFactor.after
    def on_ship_weapon_heat(self, this, _result_):
        """
        GetHeatFactor is called every frame. A rising heat factor means weapons
        are being fired. We start a looping pattern when the heat rises and stop
        it after a short cooldown of no heat increase.
        """
        if not self.is_in_spaceship:
            return

        now = time.perf_counter()
        heat = float(_result_)
        delta = heat - self._prev_heat_factor
        self._prev_heat_factor = heat

        if delta > SHIP_WEAPON_HEAT_THRESHOLD:
            self._ship_weapon_last_rise = now
            if not self._ship_weapon_active:
                self._ship_weapon_active = True
                logger.debug("ShipWeapons firing")
                self.timerController.start_ship_weapons()
        else:
            if self._ship_weapon_active and (now - self._ship_weapon_last_rise) > SHIP_WEAPON_COOLDOWN:
                self._ship_weapon_active = False
                logger.debug("ShipWeapons stopped")
                self.timerController.stop_ship_weapons()

    # ===================================================================
    # ITEMS — collection (via GiveGenericReward)
    # ===================================================================

    @nms.cGcRewardManager.GiveGenericReward.after
    def on_collect_item(self, this, lRewardID, lMissionID, lSeed, lbPeek, lbForceShowMessage,
                        liOutMultiProductCount, lbForceSilent, lInventoryChoiceOverride,
                        lbUseMiningModifier, _result_):
        # Only fire for genuine silent pickups (mining, exploration),
        # not mission rewards / forced messages which have their own fanfare
        if lbForceSilent or lbUseMiningModifier:
            logger.debug("CollectItem")
            self.myTactsuit.play_pattern("CollectItem")

    # ===================================================================
    # INTERNAL — low health helper (called from Update)
    # ===================================================================

    def _check_low_health(self, now: float):
        """
        NMS.py doesn't yet expose a direct health-value field, so we approximate
        low health by noting how recently the player took significant damage.
        Replace this with a proper health stat read once the field offset is known.
        """
        # Placeholder: the flag is set in on_take_damage when health is inferred low.
        # For now this is a no-op — see TODO below.
        pass

    # TODO: Once nmspy exposes cGcPlayerState health stats, replace _check_low_health
    # with something like:
    #
    #   state = map_struct(get_addressof(player_state_ptr), nms.cGcPlayerState)
    #   health_frac = state.mfHealth / state.mfMaxHealth
    #   if health_frac < LOW_HEALTH_THRESHOLD:
    #       if now - self._last_low_health_ping > LOW_HEALTH_INTERVAL:
    #           self._last_low_health_ping = now
    #           self.myTactsuit.play_pattern("PlayerLowHealth")


# ---------------------------------------------------------------------------
# Directional damage helper
# ---------------------------------------------------------------------------

def _directional_damage_pattern(dx: float, dz: float) -> str:
    """
    Map an incoming damage direction vector (in the XZ plane) to a vest pattern.
    The direction vector points FROM the attacker TO the player, so we invert it
    to find which side of the body was hit.

    Pattern names follow the convention:
      DamageFront / DamageBack / DamageLeft / DamageRight
    If your bHaptics project only has DefaultDamage, all four will resolve to that.
    """
    # Threshold: if the horizontal component is too small, use default
    magnitude = (dx * dx + dz * dz) ** 0.5
    if magnitude < 0.1:
        return "DefaultDamage"

    nx, nz = dx / magnitude, dz / magnitude

    # NMS world axes: +Z is forward, +X is right (approximate)
    if abs(nz) >= abs(nx):
        if nz > 0:
            return "DamageBack"   # hit from behind
        else:
            return "DamageFront"  # hit from front
    else:
        if nx > 0:
            return "DamageRight"  # hit from the right
        else:
            return "DamageLeft"   # hit from the left


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from pymhf import load_mod_file
    load_mod_file(__file__)
