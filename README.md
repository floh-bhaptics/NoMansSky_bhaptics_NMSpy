# No Man's Sky — bHaptics Haptic Suit Mod

Feel the game. Directional damage, laser fire, jetpack, spaceship
weapons, landing, pulse drive, and more — all on your bHaptics vest.

---

## Requirements

- **Windows 10/11** (64-bit)
- **No Man's Sky** via Steam
- **bHaptics Player** — download from https://www.bhaptics.com/play/bhapticsplayer
- **Python 3.9, 3.10, or 3.11** — download from https://www.python.org/downloads/
  - ⚠️ Do **not** use Python 3.12 or newer — not yet supported by NMS.py
  - ⚠️ Do **not** install from the Microsoft Store — use the official installer
  - ✅ Tick **"Add Python to PATH"** during installation

---

## Installation

1. Unzip this folder anywhere you like (e.g. `C:\NMS_bhaptics\`)
2. Double-click **`setup.bat`** — this creates a virtual environment and
   installs all required packages. Takes about a minute, only needed once.

---

## Launching the mod

1. Start **bHaptics Player** first
2. Double-click **`Launch.bat`**
3. No Man's Sky will start automatically with the mod injected

---

## Updating

To get the latest version of the underlying NMS.py library:

- Double-click **`Update.bat`**

To update the mod itself (new `.py` files from a new release):

- Replace `NoMansSky_bhaptics_nmspy.py` and `bhaptics_library.py` with
  the new versions, then run **`Update.bat`**

---

## Haptic patterns used

The mod plays the following pattern names from your bHaptics project:

| Pattern | Trigger |
|---|---|
| `heartbeat` | Played once on successful connection |
| `PlayerDeath` | Player dies |
| `FallDamage` | Landing / fall damage |
| `DefaultDamage` | Any directional hit (rotated to match direction) |
| `RightHandPistolShoot` | Projectile weapon fired, right hand |
| `LeftHandPistolShoot` | Projectile weapon fired, left hand |
| `RightHandPistolLaserShoot` | Mining laser held, right hand |
| `LeftHandPistolLaserShoot` | Mining laser held, left hand |
| `Scanning` | Scanner activated |
| `CollectItem` | Item collected via mining or pickup |
| `GetOnSpaceship` | Entering cockpit |
| `GetOffSpaceship` | Leaving cockpit / ejecting |
| `SpaceshipTakeOff` | Ship lifts off |
| `SpaceshipOnGround` | Ship touches down |
| `SpaceshipSpeedUp` | Significant acceleration detected |
| `SpaceshipPulse` | Pulse drive engaged |
| `SpaceshipBoost` | Boost thruster activated |
| `SpaceshipPulseJump` | Pulse jump activated |
| `SpaceshipWeaponShoot` | Ship weapon fired |

---

## Troubleshooting

**The mod doesn't start / Python not found**
→ Install Python 3.9–3.11 from python.org and tick "Add Python to PATH"

**setup.bat fails with "Incompatible Python version"**
→ You have Python 3.12+. Install 3.11 from python.org alongside it.
   If both are installed, `setup.bat` will use whichever `python` points
   to — you may need to temporarily adjust your PATH or call the 3.11
   executable directly: `py -3.11 -m venv venv`

**Game launches but no haptics**
→ Make sure bHaptics Player is running *before* launching the mod.
   Check the `pymhf-*.log` file in this folder for error messages.

**Game feels sluggish or movement is broken**
→ This usually means a hook is conflicting. Check the log for
   `ERROR ... has been disabled` messages and report them.

---

## Credits

Mod by [floh-bhaptics](https://github.com/floh-bhaptics)
Built on [NMS.py](https://github.com/monkeyman192/NMS.py) by monkeyman192
