# No Man's Sky — bHaptics Mod (NMS.py)

Haptic suit integration for No Man's Sky using
[NMS.py](https://github.com/monkeyman192/NMS.py) instead of raw pyMHF.

This is a rewrite of the original
[NoMansSky_bhaptics](https://github.com/floh-bhaptics/NoMansSky_bhaptics)
mod. It uses the typed hooks and struct access that NMS.py provides,
which makes the code cleaner and more maintainable than hooking against
raw byte patterns directly.

---

## Requirements

- Python 3.9–3.13 (not 3.14; see NMS.py README)
  Download from https://www.python.org/downloads — **not** the Windows Store version.
- No Man's Sky (Steam app 275850) installed and working
- A bHaptics suit and the [bHaptics Player](https://www.bhaptics.com/play/bhapticsplayer) running

---

## Installation

```
pip install nmspy bhaptics_python
```

Place both files from this folder next to each other:

```
your_mods_folder/
    NoMansSky_bhaptics_nmspy.py
    bhaptics_library.py
```

The recommended location for the mods folder is the `MODS` directory
inside the game's `GAMEDATA` folder (same place as regular MBIN mods).

---

## Running

```
pymhf run NoMansSky_bhaptics_nmspy.py
```

NMS.py will start the game automatically and inject into the process.
Two extra windows appear: the pyMHF GUI and a log window.

---

## bHaptics credentials

The `BHAPTICS_APP_ID`, `BHAPTICS_API_KEY`, and `BHAPTICS_APP_NAME`
constants at the top of `NoMansSky_bhaptics_nmspy.py` are taken from
the original mod. If you register your own app at the
[bHaptics developer portal](https://developer.bhaptics.com/), replace
them there.

---

## Haptic patterns

The mod triggers the following pattern names. All names are lowercased
before being sent to the bHaptics SDK (as required by the library).

| Pattern name              | Trigger                                         |
|---------------------------|-------------------------------------------------|
| `PlayerDeath`             | Player dies                                     |
| `FallDamage`              | Landing / fall damage                           |
| `DamageFront`             | Hit from the front (directional)               |
| `DamageBack`              | Hit from behind (directional)                  |
| `DamageLeft`              | Hit from the left (directional)                |
| `DamageRight`             | Hit from the right (directional)               |
| `DefaultDamage`           | Fallback for undetermined damage direction      |
| `RightHandPistolShoot`    | Single pistol shot, right hand                  |
| `LeftHandPistolShoot`     | Single pistol shot, left hand                   |
| `RightHandPistolLaserShoot` | Laser beam held, right hand (looping 100ms)  |
| `LeftHandPistolLaserShoot`  | Laser beam held, left hand (looping 100ms)   |
| `Scanning`                | Scanner active (looping 500ms)                  |
| `CollectItem`             | Item collected via mining or pickup             |
| `GetOnSpaceship`          | Player enters cockpit                           |
| `GetOffSpaceship`         | Player exits cockpit / ejects                   |
| `SpaceshipTakeOff`        | Ship transitions from Landed → TakingOff        |
| `SpaceshipOnGround`       | Ship transitions to Landed state                |
| `SpaceshipSpeedUp`        | Significant velocity increase detected          |
| `SpaceshipPulse`          | Pulse drive active (looping 500ms)              |
| `SpaceshipWeaponShoot`    | Ship weapons firing (looping 120ms while hot)   |
| `PlayerUsingJetpack`      | Jetpack active (looping 150ms)                  |
| `heartbeat`               | Played once on successful bHaptics connection   |

Directional damage (`DamageFront` / `DamageBack` / `DamageLeft` /
`DamageRight`) falls back to `DefaultDamage` if those patterns are not
registered in your bHaptics project.

---

## Differences from the original pyMHF mod

| Feature | Original (pyMHF) | This mod (NMS.py) |
|---|---|---|
| Hook definitions | Raw byte patterns in local `Structure` subclasses | Imported from `nmspy.data.types` |
| Struct field access | Manual `map_struct` + offset maths | `nmspy` typed partial structs |
| Scanning detection | Audio ID matching via `cTkAudioManager.Play` | `cGcBinoculars.UpdateScanBarProgress` hook |
| Laser on/off | Audio ID matching | `cGcLaserBeam.Fire` frame-by-frame with cooldown |
| Pistol single shot | `cGcNetworkWeapon.FireRemote` | Same (via `cGcNetworkWeapon.FireRemote` — add back if desired) |
| Ship takeoff/landing | Audio ID matching | `meLandState` polling in `cGcSpaceshipComponent.Update` |
| Ship acceleration | Audio ID matching | Velocity delta via `cGcSpaceshipComponent.GetVelocity` |
| Pulse drive | `GetPulseDriveFuelFactor` delta | `UpdatePulseDrive` + `mePulseDriveState` enum |
| Ship weapons | `GetCurrentShootPoints` | `GetHeatFactor` delta |
| Damage direction | Parsed from `lDir` pointer (same) | Same, with named directional patterns |

### What's not yet implemented

- **Low health reminders** — requires reading the health stat from
  `cGcPlayerState.GetStatValue`. The hook exists in NMS.py but the
  correct `leStat` index for shield/health needs confirming. A TODO
  comment marks the spot.
- **Collecting items (broad)** — `GiveGenericReward` is used, filtered
  to mining/silent pickups. Picked-up items from the environment may
  not always set `lbForceSilent`; audio-ID matching (as in the original)
  remains an alternative fallback.
- **Hyperdrive** — intentionally omitted for now.

---

## Tuning

Adjust the constants near the top of `NoMansSky_bhaptics_nmspy.py`:

```python
LOW_HEALTH_THRESHOLD = 0.25       # fraction of max health
LOW_HEALTH_INTERVAL  = 3.0        # seconds between reminders
SHIP_WEAPON_HEAT_THRESHOLD = 0.02 # min heat-factor rise per frame = "firing"
SHIP_WEAPON_COOLDOWN = 0.3        # seconds before ship-weapon loop stops
JETPACK_COOLDOWN     = 0.15       # seconds before jetpack loop stops
LASER_BEAM_COOLDOWN  = 0.25       # seconds before laser loop stops
```
