import asyncio
import threading
import time
import logging

import bhaptics_python

logger = logging.getLogger("NMS_bhaptics.bhaptics_library")


class bhaptics_suit:
    """
    Wraps the bHaptics SDK and owns a persistent asyncio event loop that
    lives on a dedicated background thread for the entire session.

    Why a persistent loop?
    ----------------------
    bhaptics_python.play_event() is a coroutine — it must be awaited inside
    a running event loop.  Game hook callbacks arrive on arbitrary threads
    that have no event loop of their own, so we keep one alive permanently
    and dispatch every play call into it with run_coroutine_threadsafe().

    Why not block __init__?
    -----------------------
    pyMHF calls __init__ synchronously on the game's main thread.  Any
    sleep or blocking wait here would freeze the engine.  Instead we start
    the background thread immediately and let it connect in the background;
    play_pattern() calls are silently dropped until self.connected is True.
    """

    def __init__(self, app_id: str, api_key: str, app_name: str):
        self.app_id = app_id
        self.api_key = api_key
        self.app_name = app_name
        self.connected = False

        # Create the event loop and start it on a daemon thread so it never
        # blocks the game process from exiting.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="bhaptics_event_loop",
        )
        self._loop_thread.start()

        # Schedule the SDK initialisation into that loop — returns immediately.
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Entry point for the background thread: runs the loop forever."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self):
        """Coroutine that initialises the SDK — runs on the background loop."""
        try:
            result = await bhaptics_python.registry_and_initialize(
                self.app_id, self.api_key, self.app_name
            )
            if not result:
                logger.error("Failed to initialize bHaptics SDK")
            else:
                logger.info("bHaptics SDK initialized")
                self.connected = True
                # Heartbeat to confirm the suit is alive — we're already
                # inside the event loop here, so call directly.
                bhaptics_python.play_event("heartbeat")
        except Exception as e:
            logger.error(f"Error initializing bHaptics SDK: {e}")

    # ------------------------------------------------------------------
    # Public API — safe to call from any thread at any time
    # ------------------------------------------------------------------

    def play_pattern(self, pattern_name: str, intensity: int = 100):
        """
        Fire a haptic pattern.  Can be called from any thread — the call is
        dispatched into the persistent background event loop.
        """
        if not self.connected:
            # Still connecting, or connection failed — drop silently.
            return
        if not isinstance(pattern_name, str):
            logger.warning(f"play_pattern: pattern_name must be a str, got {type(pattern_name)}")
            return
        if not (0 <= intensity <= 100):
            logger.warning(f"play_pattern: intensity {intensity} out of range 0–100")
            return

        self._loop.call_soon_threadsafe(
            bhaptics_python.play_event, pattern_name.lower()
        )


class TimerController:
    """
    Manages looping haptic patterns that repeat on background threads while
    a continuous action (laser beam, scanning, pulse drive, …) is active.

    Each looping effect has a trio of methods:
        start_<effect>()  — starts the loop if not already running
        stop_<effect>()   — signals the loop to stop and waits for it
        _<effect>_worker() — the thread body

    All three share a threading.Lock so start/stop are race-free even when
    called from different game hook threads.
    """

    def __init__(self, bhaptics_mod_instance):
        self.mod = bhaptics_mod_instance
        self.suit = bhaptics_mod_instance.suit   # bhaptics_suit instance

        # ---- per-effect state (interval in seconds, running flag, thread, lock) ----
        self.pistol_laser_interval  = 0.10
        self.scan_interval          = 0.50
        self.jetpack_interval       = 0.15
        self.spacejump_interval     = 0.50
        self.ship_weapons_interval  = 0.12

        # Create a dict of locks / flags / threads so we don't repeat boilerplate
        self._effects = {}
        for name in ("pistol_laser", "scan", "jetpack", "spacejump", "ship_weapons"):
            self._effects[name] = {
                "running": False,
                "thread": None,
                "lock": threading.Lock(),
            }

    # ------------------------------------------------------------------
    # Generic loop machinery
    # ------------------------------------------------------------------

    def _is_running(self, name: str) -> bool:
        return self._effects[name]["running"]

    def _start(self, name: str, target):
        e = self._effects[name]
        with e["lock"]:
            if e["running"]:
                return
            e["running"] = True
        e["thread"] = threading.Thread(
            target=target, daemon=True, name=f"{name}_timer"
        )
        e["thread"].start()

    def _stop(self, name: str):
        e = self._effects[name]
        with e["lock"]:
            if not e["running"]:
                return
            e["running"] = False
        if e["thread"]:
            e["thread"].join(timeout=1.0)

    # ------------------------------------------------------------------
    # Expose running state as properties (used by the mod)
    # ------------------------------------------------------------------

    @property
    def scan_running(self) -> bool:
        return self._effects["scan"]["running"]

    @property
    def spacejump_running(self) -> bool:
        return self._effects["spacejump"]["running"]

    @property
    def ship_weapons_running(self) -> bool:
        return self._effects["ship_weapons"]["running"]

    # ------------------------------------------------------------------
    # Pistol laser
    # ------------------------------------------------------------------

    def _pistol_laser_worker(self):
        while self._effects["pistol_laser"]["running"]:
            pattern = (
                "righthandpistollasershoot"
                if self.mod.player_hand == 0
                else "lefthandpistollasershoot"
            )
            self.suit.play_pattern(pattern)
            time.sleep(self.pistol_laser_interval)

    def start_pistol_laser(self):
        self._start("pistol_laser", self._pistol_laser_worker)

    def stop_pistol_laser(self):
        self._stop("pistol_laser")

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_worker(self):
        while self._effects["scan"]["running"]:
            self.suit.play_pattern("scanning")
            time.sleep(self.scan_interval)

    def start_scan(self):
        self._start("scan", self._scan_worker)

    def stop_scan(self):
        self._stop("scan")

    # ------------------------------------------------------------------
    # Jetpack
    # ------------------------------------------------------------------

    def _jetpack_worker(self):
        while self._effects["jetpack"]["running"]:
            self.suit.play_pattern("playerusingjetpack")
            time.sleep(self.jetpack_interval)

    def start_jetpack(self):
        self._start("jetpack", self._jetpack_worker)

    def stop_jetpack(self):
        self._stop("jetpack")

    # ------------------------------------------------------------------
    # Spaceship pulse drive
    # ------------------------------------------------------------------

    def _spacejump_worker(self):
        while self._effects["spacejump"]["running"]:
            self.suit.play_pattern("spaceshippulse")
            time.sleep(self.spacejump_interval)

    def start_spacejump(self):
        self._start("spacejump", self._spacejump_worker)

    def stop_spacejump(self):
        self._stop("spacejump")

    # ------------------------------------------------------------------
    # Spaceship weapons
    # ------------------------------------------------------------------

    def _ship_weapons_worker(self):
        while self._effects["ship_weapons"]["running"]:
            self.suit.play_pattern("spaceshipweaponshoot")
            time.sleep(self.ship_weapons_interval)

    def start_ship_weapons(self):
        self._start("ship_weapons", self._ship_weapons_worker)

    def stop_ship_weapons(self):
        self._stop("ship_weapons")