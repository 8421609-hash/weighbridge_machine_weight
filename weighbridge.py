#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
weighbridge.py
============================================================

Dynamic Weighbridge Serial Reader

Purpose:
    Sirf COM port se weight receive karna aur GUI ko live weight
    provide karna.

Important:
    - Koi fake/random weight generate nahi hota.
    - Real weighbridge aur virtual COM simulator dono same
      serial-reading pipeline use karte hain.
    - COM port unavailable ho to weight 0.00 kg show hota hai.
    - Hardware disconnect ho to 0.00 kg + disconnected status.
    - Hardware dobara connect ho to automatic reconnect.
    - Auto-detect mode mein COM port + communication settings
      automatically detect ki ja sakti hain.
    - Database/settings/main GUI is file ka kaam nahi hai.
"""

import re
import time
import threading
from typing import Optional, List, Tuple

from PySide6.QtCore import QThread, Signal

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:
    serial = None
    list_ports = None


# ============================================================
# CONSTANTS
# ============================================================

RECONNECT_INTERVAL_SECS = 5
STALL_TIMEOUT_SECS = 15

COMMON_BAUD_RATES = (
    9600,
    4800,
    2400,
    19200,
    38400,
    57600,
    115200,
)

PARITY_MAP = {
    "None": "N",
    "Even": "E",
    "Odd": "O",
    "N": "N",
    "E": "E",
    "O": "O",
}


# ============================================================
# WEIGHT PARSING
# ============================================================

# Example:
# ST,GS,+0001250.2,kg
# ST,GS,1250.2,kg
# 1250.2 kg
WEIGHT_PACKET_REGEX = re.compile(
    r"(?:^|[,;\s])"
    r"([+-]?\d+(?:\.\d+)?)"
    r"(?=\s*(?:kg|KG|,|$))"
)

# General fallback numeric parser
NUMBER_REGEX = re.compile(
    r"[-+]?\d+(?:\.\d+)?"
)


def extract_weight(text: str) -> Optional[float]:
    """
    Extract numeric weight from a serial packet.

    Supported examples:

        ST,GS,+0001250.2,kg
        ST,GS,1250.2,kg
        1250.2 kg
        1250.2

    Returns:
        float weight
        None if no valid number found
    """

    if not text:
        return None

    text = text.strip()

    if not text:
        return None

    # Preferred parser
    match = WEIGHT_PACKET_REGEX.search(text)

    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None

    # Fallback parser
    numbers = NUMBER_REGEX.findall(text)

    if not numbers:
        return None

    try:
        return float(numbers[-1])
    except ValueError:
        return None


# ============================================================
# COM PORT LIST
# ============================================================

def list_available_ports() -> List[Tuple[str, str]]:
    """
    Return currently available COM ports.

    Example:
        [
            ("COM3", "Intel Active Management Technology"),
            ("COM10", "USB Serial Port")
        ]
    """

    if list_ports is None:
        return []

    try:
        return [
            (port.device, port.description)
            for port in list_ports.comports()
        ]

    except Exception:
        return []


# ============================================================
# WEIGHT READER
# ============================================================


def _decode_serial_text(raw: bytes) -> str:
    """Decode common industrial serial payloads without assuming ASCII-only packets."""
    return raw.decode("ascii", errors="ignore").replace("\x00", "").strip()


def _read_weight_from_serial(ser, buffer: str = ""):
    """
    Read currently available bytes and extract a weight even when the device
    terminates frames with CR only, LF only, CRLF, or sends a continuous stream.
    Returns (weight, updated_buffer, had_data).
    """
    try:
        waiting = ser.in_waiting
    except Exception:
        waiting = 0

    raw = ser.read(waiting or 1)
    if not raw:
        return None, buffer, False

    buffer += _decode_serial_text(raw)
    # Bound the buffer so a noisy device cannot grow memory indefinitely.
    buffer = buffer[-2048:]

    # Prefer complete frames, but also try the current buffer because many
    # weigh indicators continuously transmit without a terminator.
    frames = re.split(r"[\r\n]+", buffer)
    candidates = frames[:-1] if len(frames) > 1 else []
    candidates.append(frames[-1])

    has_terminator = len(frames) > 1
    for frame in candidates:
        # Do not treat a single incoming byte as a complete weight when a
        # device is transmitting a multi-byte numeric value without a frame
        # terminator. Complete CR/LF frames may legitimately contain "0".
        if not has_terminator and not re.search(r"kg|KG", frame):
            numeric_tokens = NUMBER_REGEX.findall(frame)
            if not numeric_tokens or len(numeric_tokens[-1].lstrip("+-").replace(".", "")) < 2:
                continue

        weight = extract_weight(frame)
        if weight is not None:
            # Keep only the tail after the last terminator.
            if frame != frames[-1]:
                buffer = frames[-1]
            return weight, buffer, True

    return None, buffer, True


class WeightReader(QThread):

    # Live numeric weight
    weight_updated = Signal(float)

    # level, message
    #
    # level:
    #   ok
    #   warning
    #   error
    connection_changed = Signal(str, str)

    # port, baud, parity, bytesize, stopbits
    configuration_detected = Signal(
        str,
        int,
        str,
        int,
        float
    )

    def __init__(
        self,
        port: Optional[str] = None,
        baud_rate: int = 9600,
        parity: str = "None",
        bytesize: int = 8,
        stopbits: float = 1,
        parent=None,
    ):

        super().__init__(parent)

        # Current configuration
        self.port = port
        self.baud_rate = int(baud_rate)
        self.parity = parity
        self.bytesize = int(bytesize)
        self.stopbits = float(stopbits)

        # Auto detection
        self.auto_detect = False

        # Thread state
        self._running = False

        # Used when configuration changes
        self._reconfigure = threading.Event()


    # ========================================================
    # CONFIGURE
    # ========================================================

    def configure(
        self,
        port: Optional[str],
        baud_rate: int = 9600,
        parity: str = "None",
        bytesize: int = 8,
        stopbits: float = 1,
        auto_detect: bool = False,
    ):

        """
        Configure serial communication.

        Explicit mode:

            configure(
                "COM10",
                9600,
                "None",
                8,
                1,
                False
            )

        Auto mode:

            configure(
                None,
                auto_detect=True
            )
        """

        self.port = port

        self.baud_rate = int(baud_rate)

        self.parity = parity

        self.bytesize = int(bytesize)

        self.stopbits = float(stopbits)

        self.auto_detect = bool(auto_detect)

        # Tell running thread to immediately reload configuration
        self._reconfigure.set()


    # ========================================================
    # STOP
    # ========================================================

    def stop(self):

        self._running = False

        self._reconfigure.set()

        # Safe GUI value after stopping
        self.weight_updated.emit(0.0)


    # ========================================================
    # PARITY
    # ========================================================

    def _serial_parity(self, parity: str):

        if serial is None:
            return None

        value = PARITY_MAP.get(
            str(parity),
            "N"
        )

        if value == "E":
            return serial.PARITY_EVEN

        if value == "O":
            return serial.PARITY_ODD

        return serial.PARITY_NONE


    # ========================================================
    # VALIDATE WEIGHT
    # ========================================================

    @staticmethod
    def _valid_weight(
        value: Optional[float]
    ) -> bool:

        if value is None:
            return False

        # Reasonable protection against malformed packets
        return (
            -1000000.0
            <= value
            <= 10000000.0
        )


    # ========================================================
    # ZERO / STATUS
    # ========================================================

    def _emit_zero(
        self,
        level: str,
        message: str
    ):

        self.weight_updated.emit(0.0)

        self.connection_changed.emit(
            level,
            message
        )


    # ========================================================
    # THREAD RUN
    # ========================================================

    def run(self):

        self._running = True

        self._emit_zero(
            "warning",
            "Waiting for weighbridge COM data."
        )

        while self._running:

            self._reconfigure.clear()

            # ------------------------------------------------
            # PY SERIAL CHECK
            # ------------------------------------------------

            if serial is None:

                self._emit_zero(
                    "error",
                    "pyserial is not installed. "
                    "Weight is 0.00 kg."
                )

                self._wait(
                    RECONNECT_INTERVAL_SECS
                )

                continue


            # ------------------------------------------------
            # AUTO DETECTION
            # ------------------------------------------------

            if self.auto_detect:

                detected = (
                    self._auto_detect_configuration()
                )

                if (
                    detected is not None
                    and self._running
                ):

                    (
                        self.port,
                        self.baud_rate,
                        self.parity,
                        self.bytesize,
                        self.stopbits,
                    ) = detected

                    # Tell main.py what was detected
                    self.configuration_detected.emit(
                        self.port,
                        self.baud_rate,
                        self.parity,
                        self.bytesize,
                        self.stopbits,
                    )

                    self.connection_changed.emit(
                        "ok",
                        (
                            f"Auto detected "
                            f"{self.port} @ "
                            f"{self.baud_rate} "
                            f"{self.bytesize}"
                            f"{self.parity}"
                            f"{self.stopbits:g}."
                        )
                    )

                    # Start normal continuous reading
                    if self._run_serial_session():

                        continue


                if self._running:

                    self._emit_zero(
                        "warning",
                        (
                            "No valid weighbridge data "
                            "detected. Weight is 0.00 kg."
                        )
                    )

                    self._wait(
                        RECONNECT_INTERVAL_SECS
                    )

                continue


            # ------------------------------------------------
            # EXPLICIT COM PORT
            # ------------------------------------------------

            if self.port:

                connected = (
                    self._run_serial_session()
                )

                if connected:

                    continue

                if self._running:

                    self._wait(
                        RECONNECT_INTERVAL_SECS
                    )

                continue


            # ------------------------------------------------
            # NO PORT
            # ------------------------------------------------

            self._emit_zero(
                "warning",
                (
                    "No COM port configured. "
                    "Weight is 0.00 kg."
                )
            )

            self._wait(
                RECONNECT_INTERVAL_SECS
            )


    # ========================================================
    # AUTO DETECTION
    # ========================================================

    def auto_detect_configuration(self):
        """Probe all available ports/configurations and return the first valid one.

        This public method is intended for Settings/UI-triggered detection.
        The background reader uses the same probing logic through the private
        method below.
        """
        return self._auto_detect_configuration(force=True)


    def _auto_detect_configuration(self, force: bool = False):

        ports = list_available_ports()

        if not ports:
            return None


        # If a port was already selected, test it first
        if self.port:

            ports = (
                [(self.port, "configured")]
                +
                [
                    p
                    for p in ports
                    if p[0] != self.port
                ]
            )


        for (
            device,
            _description
        ) in ports:

            for baud in COMMON_BAUD_RATES:

                for parity in (
                    "None",
                    "Even",
                    "Odd"
                ):

                    for bytesize in (
                        8,
                        7
                    ):

                        for stopbits in (
                            1,
                            2
                        ):

                            if (
                                (not self._running and not force)
                                or
                                self._reconfigure.is_set()
                            ):

                                return None


                            valid = (
                                self._probe_configuration(
                                    device,
                                    baud,
                                    parity,
                                    bytesize,
                                    stopbits
                                )
                            )


                            if valid:

                                return (
                                    device,
                                    baud,
                                    parity,
                                    bytesize,
                                    float(stopbits)
                                )


        return None


    # ========================================================
    # TEST ONE SERIAL CONFIGURATION
    # ========================================================

    def _probe_configuration(
        self,
        port,
        baud,
        parity,
        bytesize,
        stopbits
    ) -> bool:

        ser = None

        try:

            ser = serial.Serial(
                port=port,
                baudrate=int(baud),
                bytesize=int(bytesize),
                parity=self._serial_parity(
                    parity
                ),
                stopbits=float(stopbits),
                timeout=0.5,
            )


            deadline = (
                time.time()
                + 1.5
            )


            buffer = ""
            while (
                (self._running or not self._reconfigure.is_set())
                and
                time.time() < deadline
            ):
                weight, buffer, had_data = _read_weight_from_serial(ser, buffer)
                if self._valid_weight(weight):
                    return True
                if not had_data:
                    time.sleep(0.01)

            return False


        except Exception:

            return False


        finally:

            if ser is not None:

                try:

                    if ser.is_open:
                        ser.close()

                except Exception:
                    pass


    # ========================================================
    # CONTINUOUS SERIAL SESSION
    # ========================================================

    def _run_serial_session(self) -> bool:

        if serial is None:

            return False


        # Check if COM port currently exists
        available = {
            device
            for device, _description
            in list_available_ports()
        }


        if self.port not in available:

            self._emit_zero(
                "error",
                (
                    f"{self.port or 'COM port'} "
                    f"is not connected. "
                    f"Weight is 0.00 kg."
                )
            )

            return False


        ser = None


        try:

            # ----------------------------------------------
            # OPEN SERIAL PORT
            # ----------------------------------------------

            ser = serial.Serial(
                port=self.port,
                baudrate=int(
                    self.baud_rate
                ),
                bytesize=int(
                    self.bytesize
                ),
                parity=self._serial_parity(
                    self.parity
                ),
                stopbits=float(
                    self.stopbits
                ),
                timeout=1,
            )


            self.connection_changed.emit(
                "ok",
                (
                    f"Connected to "
                    f"{self.port} @ "
                    f"{self.baud_rate} baud."
                )
            )


            last_valid_data = time.time()
            serial_buffer = ""

            # ----------------------------------------------
            # CONTINUOUS READING
            # ----------------------------------------------

            while (
                self._running
                and
                not self._reconfigure.is_set()
            ):

                try:

                    weight, serial_buffer, had_data = _read_weight_from_serial(
                        ser, serial_buffer
                    )

                    # --------------------------------------
                    # NO DATA
                    # --------------------------------------

                    if not had_data:

                        if (
                            time.time()
                            - last_valid_data
                            >
                            STALL_TIMEOUT_SECS
                        ):

                            self._emit_zero(
                                "error",
                                (
                                    f"No valid weight "
                                    f"data from "
                                    f"{self.port} for "
                                    f"{STALL_TIMEOUT_SECS}s. "
                                    f"Weight is 0.00 kg."
                                )
                            )

                            return False

                        continue

                    # Invalid/incomplete packet: keep accumulating data.
                    if not self._valid_weight(weight):
                        continue


                    # --------------------------------------
                    # SEND TO GUI
                    # --------------------------------------

                    self.weight_updated.emit(
                        round(
                            float(weight),
                            2
                        )
                    )


                    last_valid_data = (
                        time.time()
                    )


                # ------------------------------------------
                # OS / DISCONNECT ERROR
                # ------------------------------------------

                except OSError:

                    self._emit_zero(
                        "error",
                        (
                            f"Weighbridge disconnected "
                            f"from {self.port}. "
                            f"Weight is 0.00 kg."
                        )
                    )

                    return False


                # ------------------------------------------
                # SERIAL EXCEPTION
                # ------------------------------------------

                except Exception as exc:

                    if (
                        serial is not None
                        and
                        isinstance(
                            exc,
                            serial.SerialException
                        )
                    ):

                        self._emit_zero(
                            "error",
                            (
                                f"Serial communication "
                                f"error on {self.port}: "
                                f"{exc}"
                            )
                        )

                        return False


                    # Malformed individual packet:
                    # keep connection alive
                    continue


            return True


        # ----------------------------------------------------
        # OPEN PORT FAILURE
        # ----------------------------------------------------

        except Exception as exc:

            self._emit_zero(
                "error",
                (
                    f"Could not open "
                    f"{self.port}: {exc}. "
                    f"Weight is 0.00 kg."
                )
            )

            return False


        # ----------------------------------------------------
        # CLOSE PORT
        # ----------------------------------------------------

        finally:

            if ser is not None:

                try:

                    if ser.is_open:
                        ser.close()

                except Exception:
                    pass


    # ========================================================
    # WAIT WITHOUT BLOCKING STOP
    # ========================================================

    def _wait(
        self,
        seconds: float
    ):

        end = (
            time.time()
            + seconds
        )


        while (
            self._running
            and
            not self._reconfigure.is_set()
            and
            time.time() < end
        ):

            time.sleep(0.2)
