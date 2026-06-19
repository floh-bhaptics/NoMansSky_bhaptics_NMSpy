import bhaptics_python
import asyncio
import time
import threading
import logging

logger = logging.getLogger("NMS_bhaptics.bhaptics_library")


class bhaptics_suit:
    def __init__(self, app_id: str, api_key: str, app_name: str):
        super().__init__()
        self.app_id = app_id
        self.api_key = api_key
        self.app_name = app_name
        self.connected = False

    async def connect(self):
        try:
            result = await bhaptics_python.registry_and_initialize(
                self.app_id, self.api_key, self.app_name
            )
            if not result:
                logger.error("Failed to initialize bHaptics SDK")
            else:
                logger.info("bHaptics SDK initialized")
                self.connected = True
                self.play_pattern("heartbeat")
        except Exception as e:
            logger.error(f"Error initializing SDK: {e}")

    def play_pattern(self, pattern_name: str, intensity: int = 100):
        if not self.connected:
            logger.warning("Cannot send haptic signal: Suit not connected.")
            return
        try:
            if not isinstance(pattern_name, str):
                raise TypeError("Pattern name must be a string.")
            if not (0 <= intensity <= 100):
                raise ValueError("Intensity must be between 0 and 100.")
            lower_pattern = pattern_name.lower()
            bhaptics_python.play_event(lower_pattern)
        except Exception as e:
            logger.warning(f"Failed to send haptic signal: {e}")


class TimerController:
    """Manages looping haptic patterns that run on background threads."""

    def __init__(self, bhaptics_mod_instance):
        self.bhaptics_mod = bhaptics_mod_instance
        self.myTactsuit = bhaptics_mod_instance.myTactsuit

        # Pistol laser (continuous beam)
        self.pistol_laser_interval = 0.1
        self.pistol_laser_running = False
        self.pistol_laser_thread = None
        self.pistol_laser_lock = threading.Lock()

        # Scanning
        self.scan_interval = 0.5
        self.scan_running = False
        self.scan_thread = None
        self.scan_lock = threading.Lock()

        # Spaceship jetpack (continuous use)
        self.jetpack_interval = 0.15
        self.jetpack_running = False
        self.jetpack_thread = None
        self.jetpack_lock = threading.Lock()

        # Spaceship pulse drive
        self.spacejump_interval = 0.5
        self.spacejump_running = False
        self.spacejump_thread = None
        self.spacejump_lock = threading.Lock()

        # Spaceship weapon fire (continuous)
        self.ship_weapons_interval = 0.12
        self.ship_weapons_running = False
        self.ship_weapons_thread = None
        self.ship_weapons_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Pistol laser (looping while beam is held)
    # ------------------------------------------------------------------

    def _pistol_laser_worker(self):
        while True:
            with self.pistol_laser_lock:
                if not self.pistol_laser_running:
                    break
            if self.bhaptics_mod.player_hand == 0:
                self.myTactsuit.play_pattern("righthandpistollasershoot")
            else:
                self.myTactsuit.play_pattern("lefthandpistollasershoot")
            time.sleep(self.pistol_laser_interval)

    def start_pistol_laser(self):
        with self.pistol_laser_lock:
            if self.pistol_laser_running:
                return
            self.pistol_laser_running = True
        self.pistol_laser_thread = threading.Thread(
            target=self._pistol_laser_worker, daemon=True, name="PistolLaserTimer"
        )
        self.pistol_laser_thread.start()

    def stop_pistol_laser(self):
        with self.pistol_laser_lock:
            if not self.pistol_laser_running:
                return
            self.pistol_laser_running = False
        if self.pistol_laser_thread:
            self.pistol_laser_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_worker(self):
        while True:
            with self.scan_lock:
                if not self.scan_running:
                    break
            self.myTactsuit.play_pattern("scanning")
            time.sleep(self.scan_interval)

    def start_scan(self):
        with self.scan_lock:
            if self.scan_running:
                return
            self.scan_running = True
        self.scan_thread = threading.Thread(
            target=self._scan_worker, daemon=True, name="ScanTimer"
        )
        self.scan_thread.start()

    def stop_scan(self):
        with self.scan_lock:
            if not self.scan_running:
                return
            self.scan_running = False
        if self.scan_thread:
            self.scan_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Jetpack (looping while active)
    # ------------------------------------------------------------------

    def _jetpack_worker(self):
        while True:
            with self.jetpack_lock:
                if not self.jetpack_running:
                    break
            self.myTactsuit.play_pattern("playerusingjetpack")
            time.sleep(self.jetpack_interval)

    def start_jetpack(self):
        with self.jetpack_lock:
            if self.jetpack_running:
                return
            self.jetpack_running = True
        self.jetpack_thread = threading.Thread(
            target=self._jetpack_worker, daemon=True, name="JetpackTimer"
        )
        self.jetpack_thread.start()

    def stop_jetpack(self):
        with self.jetpack_lock:
            if not self.jetpack_running:
                return
            self.jetpack_running = False
        if self.jetpack_thread:
            self.jetpack_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Spaceship pulse / hyperdrive charge
    # ------------------------------------------------------------------

    def _spacejump_worker(self):
        while True:
            with self.spacejump_lock:
                if not self.spacejump_running:
                    break
            self.myTactsuit.play_pattern("spaceshippulse")
            time.sleep(self.spacejump_interval)

    def start_spacejump(self):
        with self.spacejump_lock:
            if self.spacejump_running:
                return
            self.spacejump_running = True
        self.spacejump_thread = threading.Thread(
            target=self._spacejump_worker, daemon=True, name="SpaceJumpTimer"
        )
        self.spacejump_thread.start()

    def stop_spacejump(self):
        with self.spacejump_lock:
            if not self.spacejump_running:
                return
            self.spacejump_running = False
        if self.spacejump_thread:
            self.spacejump_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Spaceship weapons (looping while firing)
    # ------------------------------------------------------------------

    def _ship_weapons_worker(self):
        while True:
            with self.ship_weapons_lock:
                if not self.ship_weapons_running:
                    break
            self.myTactsuit.play_pattern("spaceshipweaponshoot")
            time.sleep(self.ship_weapons_interval)

    def start_ship_weapons(self):
        with self.ship_weapons_lock:
            if self.ship_weapons_running:
                return
            self.ship_weapons_running = True
        self.ship_weapons_thread = threading.Thread(
            target=self._ship_weapons_worker, daemon=True, name="ShipWeaponsTimer"
        )
        self.ship_weapons_thread.start()

    def stop_ship_weapons(self):
        with self.ship_weapons_lock:
            if not self.ship_weapons_running:
                return
            self.ship_weapons_running = False
        if self.ship_weapons_thread:
            self.ship_weapons_thread.join(timeout=1.0)
