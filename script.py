import re
import time
import serial
import serial.tools.list_ports
from PySide6.QtCore import QThread, Signal

class WeighbridgeSerialService(QThread):
    """
    Production Serial/Weighbridge Service
    Responsibilities:
        1. Connect to real weighbridge through COM port
        2. Continuously read serial data
        3. Extract numeric weight
        4. Maintain rolling readings
        5. Detect stable weight
        6. Automatically reconnect if hardware disconnects

    IMPORTANT:
        No simulated/fake weight is generated here.
        Simulator should be used separately for testing only.
    """

    # ---------------------------------------------------------
    # SIGNALS
    # ---------------------------------------------------------

    weight_updated = Signal(float)
    stable_weight_updated = Signal(float, bool)
    connection_changed = Signal(bool, str)
    error_occurred = Signal(str)

    # ---------------------------------------------------------
    # INIT
    # ---------------------------------------------------------

    def __init__(
        self,
        port=None,
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=1,
        stability_window=5,
        stability_tolerance=1.0,
        parent=None
    ):
    # object saving 
        super().__init__(parent)      
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = int(bytesize)
        self.parity = self._normalize_parity(parity)
        self.stopbits = self._normalize_stopbits(stopbits)
        self.timeout = float(timeout)

        # -----------------------------------------------------
        # STABILITY CONFIG
        # -----------------------------------------------------

        self.stability_window = int(stability_window)
        self.stability_tolerance = float( stability_tolerance)
        self.rolling_readings = []
        self.current_weight = None
        self.stable_weight = None
        self.is_stable = False

        # -----------------------------------------------------
        # SERIAL
        # -----------------------------------------------------

        self.serial_conn = None
        self._running = False
        self.reconnect_delay = 2

    # =========================================================
    # NORMALIZE SETTINGS this function use for normalize the parity 
    # =========================================================

    def _normalize_parity(self, parity):

        parity = str(parity).upper()

        mapping = {
            "N": serial.PARITY_NONE,
            "NONE": serial.PARITY_NONE,

            "E": serial.PARITY_EVEN,
            "EVEN": serial.PARITY_EVEN,

            "O": serial.PARITY_ODD,
            "ODD": serial.PARITY_ODD,

            "M": serial.PARITY_MARK,
            "MARK": serial.PARITY_MARK,

            "S": serial.PARITY_SPACE,
            "SPACE": serial.PARITY_SPACE,
        }
        return mapping.get(
            parity,
            serial.PARITY_NONE
        )

    def _normalize_stopbits(self, stopbits):
# input value convert to float
        value = float(stopbits)
        if value == 1:
            return serial.STOPBITS_ONE
        if value == 1.5:
            return serial.STOPBITS_ONE_POINT_FIVE
        if value == 2:
            return serial.STOPBITS_TWO
        return serial.STOPBITS_ONE

    # =========================================================
    # CONFIGURE
    # =========================================================

    def configure(
        self,
        port,
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=1
    ):
        """
        Update communication settings.
        If service is running, restart it using the new settings.
        """

        was_running = self._running
        if was_running:
            self.stop()
            if self.isRunning():
                self.wait(3000)
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = int(bytesize)
        self.parity = self._normalize_parity(parity)
        self.stopbits = self._normalize_stopbits(stopbits)
        self.timeout = float(timeout)
        # old setting clear and update new setting
        self.clear_stability()
        if was_running:
            self.start()

    # =========================================================
    # CONNECT
    # =========================================================

    def connect_serial(self):
        # self.port check port is configure or not
        if not self.port:
            self.connection_changed.emit(
                False,
                "No COM port configured"
            )
            return False
        try:

            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout
            )
            self.connection_changed.emit(
                True,
                f"Connected to {self.port} @ {self.baudrate}"
            )
            return True
        except Exception as exc:
            self.serial_conn = None
            self.connection_changed.emit(
                False,
                f"Unable to connect {self.port}: {exc}"
            )
            return False

    # =========================================================
    # DISCONNECT
    # =========================================================

    def disconnect_serial(self):

        if self.serial_conn:
            try:
                if self.serial_conn.is_open:
                    self.serial_conn.close()
            except Exception:
                pass
            finally:
                self.serial_conn = None

    # =========================================================
    # THREAD START
    # =========================================================

    def run(self):
# activate the worker
        self._running = True
        while self._running:
            # -------------------------------------------------
            # CONNECT IF NEEDED
            # -------------------------------------------------
            if self.serial_conn is None:
                if not self.connect_serial():
                    if self._running:
                        time.sleep(
                            self.reconnect_delay
                        )
                    continue
            # -------------------------------------------------
            # READ SERIAL DATA
            # -------------------------------------------------
            try:
                raw_data = self.serial_conn.readline()
                if not raw_data:
                    continue
                weight = self.parse_weight_packet(raw_data)
                if weight is None:
                    continue
                self.process_weight(weight)
            except Exception as exc:
                self.connection_changed.emit(
                    False,
                    f"Serial read error: {exc}"
                )
                self.disconnect_serial()
                self.clear_stability()
                if self._running:
                    time.sleep(
                        self.reconnect_delay
                    )
        self.disconnect_serial()

    # =========================================================
    # PACKET PARSER
    # =========================================================

    def parse_weight_packet(self, raw_data):

        """
        Extract the actual weight from a serial packet.
        Examples:
            ST,GS,+0001250.2,kg
            ST,GS,1250.2,kg
            +1250.20 kg
            1250.2
        """
        try:
            if isinstance(raw_data, bytes):
                decoded = raw_data.decode(
                    "ascii",
                    errors="ignore"
                )
            else:
                decoded = str(raw_data)
            decoded = decoded.strip()
            if not decoded:
                return None

            # -------------------------------------------------
            # FIRST TRY:
            # Common weighbridge packet format
            # ST,GS,+0001250.2,kg
            # -------------------------------------------------

            match = re.search(
                r"(?:ST\s*,\s*GS\s*,\s*)"
                r"([+-]?\d+(?:\.\d+)?)",
                decoded,
                re.IGNORECASE
            )
            if match:
                return float(
                    match.group(1)
                )

            # -------------------------------------------------
            # SECOND TRY:
            # General signed decimal number
            # -------------------------------------------------

            matches = re.findall(
                r"[+-]?\d+(?:\.\d+)?",
                decoded
            )
            if not matches:
                return None
            # Avoid selecting obvious protocol numbers
            # where possible.
            for value in matches:
                try:
                    number = float(value)
                    return number
                except ValueError:
                    continue
            return None
        except Exception:
            return None

    # =========================================================
    # PROCESS WEIGHT
    # =========================================================

    def process_weight(self, weight):
        try:
            weight = float(weight)
        except (ValueError, TypeError):
            return
        if weight < 0:
            return
        self.current_weight = round(
            weight,
            2
        )

        # -----------------------------------------------------
        # LIVE WEIGHT
        # -----------------------------------------------------

        self.weight_updated.emit(
            self.current_weight
        )

        # -----------------------------------------------------
        # STABILITY
        # -----------------------------------------------------

        stable, stable_value = (
            self.check_stability(
                self.current_weight
            )
        )

        if stable:

            self.is_stable = True

            self.stable_weight = stable_value

            self.stable_weight_updated.emit(
                stable_value,
                True
            )

        else:

            self.is_stable = False

            self.stable_weight = None

            self.stable_weight_updated.emit(
                0.0,
                False
            )

    # =========================================================
    # STABILITY DETECTION
    # =========================================================

    def check_stability(self, new_reading):

        self.rolling_readings.append(
            float(new_reading)
        )

        # Keep only required rolling readings
        if len(self.rolling_readings) > self.stability_window:

            self.rolling_readings.pop(0)

        # Not enough readings yet
        if len(self.rolling_readings) < self.stability_window:

            return False, None

        minimum = min(
            self.rolling_readings
        )

        maximum = max(
            self.rolling_readings
        )

        variation = maximum - minimum

        # -----------------------------------------------------
        # STABLE
        # -----------------------------------------------------

        if variation <= self.stability_tolerance:

            stable_value = (
                sum(self.rolling_readings)
                / len(self.rolling_readings)
            )

            return (
                True,
                round(stable_value, 2)
            )

        # -----------------------------------------------------
        # NOT STABLE
        # -----------------------------------------------------

        return False, None

    # =========================================================
    # CLEAR STABILITY
    # =========================================================

    def clear_stability(self):

        self.rolling_readings.clear()

        self.stable_weight = None

        self.is_stable = False

        self.stable_weight_updated.emit(
            0.0,
            False
        )

    # =========================================================
    # GET CURRENT WEIGHT
    # =========================================================

    def get_current_weight(self):

        return self.current_weight

    # =========================================================
    # GET STABLE WEIGHT
    # =========================================================

    def get_stable_weight(self):

        if self.is_stable:

            return self.stable_weight

        return None

    # =========================================================
    # CONNECTION STATUS
    # =========================================================

    def is_connected(self):

        return (
            self.serial_conn is not None
            and self.serial_conn.is_open
        )

    # =========================================================
    # STOP
    # =========================================================

    def stop(self):

        self._running = False

        self.disconnect_serial()

        self.clear_stability()

    # =========================================================
    # AVAILABLE PORTS
    # =========================================================

    @staticmethod
    def list_available_ports():

        try:

            return [
                (
                    port.device,
                    port.description
                )
                for port in serial.tools.list_ports.comports()
            ]

        except Exception:

            return []