#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 main.py  --  Professional Weighbridge System
==============================================================================

Local SQLite is the primary, reliable store: every Entry and Exit is
written to disk the instant it happens, whether or not the internet is
up (see database.py). A background SyncManager (sync_manager.py) keeps
operator-created records marked Unsync locally and sends all currently
unsynced Entry and Exit records together in one bulk API request every
5 minutes, retrying them on later intervals when offline/unreachable,
and flips them to Sync on success. A background BackupManager (backup_manager.py)
snapshots the database once a day. A resilient WeightReader
(weighbridge.py) keeps the live weight flowing even through weighbridge
disconnects, port changes, or read errors, auto-reconnecting in the
background. Every important action is written to an audit trail
(database.audit_log) for traceability.
"""

import os
import sys
import json
import subprocess
import importlib
import re
from typing import Optional

# ------------------------------------------------------------------------
# Dependencies auto-install
# ------------------------------------------------------------------------
REQUIRED_PACKAGES = {"serial": "pyserial==3.5", "PySide6": "PySide6==6.11.1"}


def _ensure_dependencies() -> None:
    if getattr(sys, "frozen", False):
        return
    missing = []
    for module_name, pip_spec in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(pip_spec)
    if not missing:
        return
    print(f"[Setup] Installing missing packages: {', '.join(missing)}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    except subprocess.CalledProcessError as exc:
        print(f"[Setup] ERROR: automatic installation failed: {exc}")
        sys.exit(1)


_ensure_dependencies()

import serial.tools.list_ports as list_ports

from PySide6.QtCore import Qt, QTimer, QDateTime, QTime
from PySide6.QtGui import QIcon, QTextDocument, QPageLayout, QPageSize
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QFrame, QStackedWidget, QButtonGroup,
    QLineEdit, QFormLayout, QGridLayout, QMessageBox, QTableWidget, QTableWidgetItem,
    QAbstractItemView, QHeaderView, QTabWidget, QSizePolicy,
    QProgressBar, QStatusBar, QDateTimeEdit, QCheckBox, QScrollArea,
)


def resource_path(filename: str) -> str:
    """Absolute path to a bundled resource (e.g. the app icon), working both
    when running from source and when running as a frozen PyInstaller EXE
    (where bundled files are extracted to sys._MEIPASS at runtime)."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, filename)


# The same balance.ico is used everywhere: EXE file icon (set in the .spec),
# running-application window/title-bar icon (set below), and — via the
# InstallShield project — the desktop shortcut icon.
APP_ICON_PATH = resource_path("balance.ico")

import database as db
from weighbridge import WeightReader, list_available_ports
from sync_manager import SyncManager, is_internet_available, is_server_reachable
from backup_manager import BackupManager

# --- Abnormal-weight detection thresholds -----------------------------------
# Anything outside this plausible range for a laden truck, or a single-tick
# jump bigger than this, is flagged so the operator can double check the
# hardware/reading instead of silently recording garbage.
MIN_PLAUSIBLE_WEIGHT_KG = 0.0
MAX_PLAUSIBLE_WEIGHT_KG = 80000.0   # ~80 tonnes: generous ceiling for a loaded truck
MAX_JUMP_PER_READING_KG = 3000.0    # a single ~1s tick should not move this much
ABNORMAL_ALERT_COOLDOWN_SECS = 30   # don't spam the same alert every second


# ==============================================================================
# SETTINGS
# ==============================================================================

def get_app_data_path(filename: str) -> str:
    return db.get_app_data_path(filename)


SETTINGS_FILE = get_app_data_path("settings.json")


def _default_settings() -> dict:
    return {
        "com_port": None,
        "baud_rate": 9600,
        "parity": "None",
        "bytesize": 8,
        "stopbits": 1,
        "theme": "Light",
        "server_url": "",
        "token": "",
        # COMPANY_ID is required by Oracle TRUCK_WEIGHT.COMPANY_ID.
        "company_id": "",
        # Legacy setting retained for migration from older settings.json files.
        "factory_number": "",
        "machine_number": "",
        # Controls only the automatic 5-minute API synchronization.
        # Manual "Sync Now" remains available as an explicit operator action.
        "auto_sync_enabled": True,
    }


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = _default_settings()
            merged.update(data)
            # Migrate old Factory Number setting to the Oracle-required COMPANY_ID.
            if not str(merged.get("company_id", "")).strip():
                merged["company_id"] = str(merged.get("factory_number", "")).strip()
            return merged
        except Exception:
            pass
    settings = _default_settings()
    save_settings(settings)
    return settings


def save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as exc:
        print(f"[Settings] Failed to save: {exc}")


db.init_database()


# ==============================================================================
# STYLING (Light & Dark Themes)
# ==============================================================================

LIGHT_STYLESHEET = """
QMainWindow, QWidget { background-color: #f4f6f9; color: #212529; font-family: "Segoe UI", Arial, sans-serif; }
QFrame#navBar { background-color: #ffffff; border-bottom: 1px solid #dcdcdc; }
QLabel#navTitle { font-size: 18px; font-weight: 700; letter-spacing: 1px; color: #1e293b; }
QFrame#card { background-color: #ffffff; border: 1px solid #dcdcdc; border-radius: 12px; }
QFrame#statCard { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; }
QLabel#statNumber { font-size: 24px; font-weight: 800; color: #2563eb; }
QLabel#statTitle { font-size: 12px; color: #64748b; font-weight: 600; }
QLabel#caption { font-size: 13px; letter-spacing: 2px; color: #64748b; }
QLabel#weightValue { font-size: 64px; font-weight: 800; color: #2563eb; }
QPushButton { background-color: #e2e8f0; color: #1e293b; border: 1px solid #cbd5e1; border-radius: 8px; padding: 9px 14px; font-size: 13px; font-weight: 600; }
QPushButton:hover { background-color: #cbd5e1; border: 1px solid #2563eb; }
QPushButton:checked { background-color: #2563eb; color: #ffffff; border: none; }
QPushButton:disabled { color: #94a3b8; background-color: #f1f5f9; }
QPushButton#primaryButton { background-color: #16a34a; color: #ffffff; border: none; }
QPushButton#primaryButton:hover { background-color: #15803d; }
QPushButton#dangerButton { background-color: #dc2626; color: #ffffff; border: none; }
QPushButton#dangerButton:hover { background-color: #b91c1c; }
QLineEdit, QComboBox { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 7px; padding: 8px 10px; color: #1e293b; font-size: 14px; min-height: 20px; }
#modernForm QLineEdit, #modernForm QComboBox { font-weight: 400; }
#settingsScreen QLineEdit, #settingsScreen QComboBox { font-size: 12px; font-weight: 400; padding: 5px 8px; min-height: 16px; }
QTableWidget { background-color: #ffffff; gridline-color: #e2e8f0; border: 1px solid #cbd5e1; }
QHeaderView::section { background-color: #f1f5f9; color: #1e293b; padding: 6px; border: none; font-size: 12px; }
QProgressBar { border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; font-size: 11px; font-weight: 600; }
QProgressBar::chunk { background-color: #2563eb; border-radius: 6px; }
QTabWidget::pane { border: 1px solid #cbd5e1; border-radius: 8px; background-color: #ffffff; }
QTabBar::tab { background-color: #e2e8f0; padding: 8px 16px; margin-right: 2px; border-top-left-radius: 6px; border-top-right-radius: 6px; font-size: 12px; font-weight: 600; }
QTabBar::tab:selected { background-color: #2563eb; color: #ffffff; }
QStatusBar { background-color: #ffffff; border-top: 1px solid #dcdcdc; }
"""

DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #12161c; color: #e5e9f0; font-family: "Segoe UI", Arial, sans-serif; }
QFrame#navBar { background-color: #171c24; border-bottom: 1px solid #2a3341; }
QLabel#navTitle { font-size: 18px; font-weight: 700; letter-spacing: 1px; color: #f5f7fa; }
QFrame#card { background-color: #171c24; border: 1px solid #2a3341; border-radius: 12px; }
QFrame#statCard { background-color: #1a2028; border: 1px solid #2a3341; border-radius: 8px; }
QLabel#statNumber { font-size: 24px; font-weight: 800; color: #3b82f6; }
QLabel#statTitle { font-size: 12px; color: #93a3b8; font-weight: 600; }
QLabel#caption { font-size: 13px; letter-spacing: 2px; color: #93a3b8; }
QLabel#weightValue { font-size: 64px; font-weight: 800; color: #3b82f6; }
QPushButton { background-color: #1e2530; color: #e5e9f0; border: 1px solid #2a3341; border-radius: 8px; padding: 9px 14px; font-size: 13px; font-weight: 600; }
QPushButton:hover { background-color: #262f3d; border: 1px solid #3b82f6; }
QPushButton:checked { background-color: #3b82f6; color: #ffffff; border: none; }
QPushButton:disabled { color: #5b6472; background-color: #171c24; }
QPushButton#primaryButton { background-color: #16a34a; color: #ffffff; border: none; }
QPushButton#primaryButton:hover { background-color: #15803d; }
QPushButton#dangerButton { background-color: #dc2626; color: #ffffff; border: none; }
QPushButton#dangerButton:hover { background-color: #b91c1c; }
QLineEdit, QComboBox { background-color: #1a2028; border: 1px solid #2a3341; border-radius: 7px; padding: 8px 10px; color: #e5e9f0; font-size: 14px; min-height: 20px; }
#modernForm QLineEdit, #modernForm QComboBox { font-weight: 400; }
#settingsScreen QLineEdit, #settingsScreen QComboBox { font-size: 12px; font-weight: 400; padding: 5px 8px; min-height: 16px; }
QTableWidget { background-color: #171c24; gridline-color: #2a3341; border: 1px solid #2a3341; }
QHeaderView::section { background-color: #1e2530; color: #e5e9f0; padding: 6px; border: none; font-size: 12px; }
QProgressBar { border: 1px solid #2a3341; border-radius: 6px; text-align: center; font-size: 11px; font-weight: 600; color: #e5e9f0; }
QProgressBar::chunk { background-color: #3b82f6; border-radius: 6px; }
QTabWidget::pane { border: 1px solid #2a3341; border-radius: 8px; background-color: #171c24; }
QTabBar::tab { background-color: #1e2530; padding: 8px 16px; margin-right: 2px; border-top-left-radius: 6px; border-top-right-radius: 6px; font-size: 12px; font-weight: 600; }
QTabBar::tab:selected { background-color: #3b82f6; color: #ffffff; }
QStatusBar { background-color: #171c24; border-top: 1px solid #2a3341; }
"""

#compactForm QLineEdit, #compactForm QComboBox { min-height: 30px; max-height: 34px; padding: 5px 8px; font-size: 12px; }

STATUS_COLORS = {"ok": "#16a34a", "warning": "#eab308", "error": "#dc2626"}


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


# ==============================================================================
# SCREEN: DASHBOARD
# ==============================================================================

class DashboardScreen(QWidget):
    def __init__(self, on_toggle_calculation, on_notify, get_settings=None, on_sync_now=None):
        super().__init__()
        self.on_toggle_calculation = on_toggle_calculation
        self.on_notify = on_notify  # (level, message) -> None, surfaces to status bar + audit log
        # Save actions wake the background sync immediately.  The background
        # worker still retries every 5 minutes when offline/unreachable.
        self.on_sync_now = on_sync_now or (lambda: None)
        # Exit always uses this machine's local Entry records.
        # Sync status never controls whether an Entry appears in Exit.
        self.current_weight = 0.0
        self.stable_weight = 0.0
        # After a successful save, keep the dashboard readout at zero until
        # the operator starts the next measurement. Raw live values still update.
        self._zero_readout_after_save = False
        self.is_measuring = False
        self.measurement_readings = []
        self.measurement_timer = None
        self.stable_display_timer = None
        self._measurement_elapsed = 0

        self._pending_records = []
        self._selected_exit_id = None
        self._selected_exit_truck_number = None
        self._selected_entry_weight = None
        self._selected_driver_phone = None
        self._selected_driver_name = None
        self._pending_save_action = None

        self._last_weight_for_jump_check: Optional[float] = None
        self._last_abnormal_alert_at = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        # --- Top Statistics Bar ---
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(12)

        self.stat_inside_val = QLabel("0")
        self.stat_inside_val.setObjectName("statNumber")
        self.stat_inside_val.setAlignment(Qt.AlignCenter)
        inside_card = self._create_stat_widget("TRUCKS INSIDE (PENDING)", self.stat_inside_val)

        self.stat_exited_val = QLabel("0")
        self.stat_exited_val.setObjectName("statNumber")
        self.stat_exited_val.setAlignment(Qt.AlignCenter)
        exited_card = self._create_stat_widget("COMPLETED EXITS", self.stat_exited_val)

        self.stat_total_val = QLabel("0")
        self.stat_total_val.setObjectName("statNumber")
        self.stat_total_val.setAlignment(Qt.AlignCenter)
        total_card = self._create_stat_widget("TOTAL LOGGED TRUCKS", self.stat_total_val)

        self.stat_unsynced_val = QLabel("0")
        self.stat_unsynced_val.setObjectName("statNumber")
        self.stat_unsynced_val.setAlignment(Qt.AlignCenter)
        unsynced_card = self._create_stat_widget("SYNCED / UNSYNCED", self.stat_unsynced_val)

        stats_layout.addWidget(inside_card)
        stats_layout.addWidget(exited_card)
        stats_layout.addWidget(total_card)
        stats_layout.addWidget(unsynced_card)
        root.addLayout(stats_layout)

        # --- Control Bar ---
        control_bar_layout = QHBoxLayout()
        self.stability_status_label = QLabel("Status: Ready — weight reading continuously")
        self.stability_status_label.setStyleSheet("color: #eab308; font-weight: bold; font-size: 13px;")

        self.calculate_btn = QPushButton("⚖ Calculate Weight (10s)")
        self.calculate_btn.setObjectName("primaryButton")
        self.calculate_btn.clicked.connect(self.on_toggle_calculation)

        control_bar_layout.addWidget(self.stability_status_label)
        control_bar_layout.addStretch()
        control_bar_layout.addWidget(self.calculate_btn)
        root.addLayout(control_bar_layout)

        # --- Big Live Weight Readout ---
        weight_card = _card()
        weight_layout = QVBoxLayout(weight_card)
        weight_layout.setAlignment(Qt.AlignCenter)
        weight_layout.setContentsMargins(20, 20, 20, 20)

        caption = QLabel("STABLE / LIVE WEIGHT READOUT")
        caption.setObjectName("caption")
        caption.setAlignment(Qt.AlignCenter)

        self.weight_label = QLabel("0.00 kg")
        self.weight_label.setObjectName("weightValue")
        self.weight_label.setAlignment(Qt.AlignCenter)

        self.status_label = QLabel("● Auto-connected")
        self.status_label.setAlignment(Qt.AlignCenter)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 10)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("Ready")
        self.progress_bar.setMaximumHeight(22)
        self.progress_bar.setVisible(False)

        weight_layout.addWidget(caption)
        weight_layout.addWidget(self.weight_label)
        weight_layout.addWidget(self.status_label)
        weight_layout.addWidget(self.progress_bar)
        root.addWidget(weight_card)

        # --- Entry / Exit tabs ---
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_entry_tab(), "📝 Entry Form")
        self.tabs.addTab(self._build_exit_tab(), "🚪 Exit Form")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs)

        self.update_statistics_display()

    def _create_stat_widget(self, title_text: str, value_label: QLabel) -> QFrame:
        card = QFrame()
        card.setObjectName("statCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(4)

        title = QLabel(title_text)
        title.setObjectName("statTitle")
        title.setAlignment(Qt.AlignCenter)

        layout.addWidget(title)
        layout.addWidget(value_label)
        return card

    def update_statistics_display(self) -> None:
        stats = db.get_truck_statistics()
        self.stat_inside_val.setText(str(stats["entered"]))
        self.stat_exited_val.setText(str(stats["exited"]))
        self.stat_total_val.setText(str(stats["total"]))
        self.stat_unsynced_val.setText(f"{stats["synced"]} / {stats["unsynced"]}")

    def _build_entry_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("modernForm")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        form_row = QHBoxLayout()
        form_row.setSpacing(12)

        self.entry_truck_number = QLineEdit()
        self.entry_truck_number.setPlaceholderText("Truck number")
        self.entry_truck_number.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry_truck_number.setMinimumWidth(150)
        self.entry_truck_number.setMaxLength(30)
        self.entry_truck_number.textChanged.connect(self._sanitize_truck_number)

        self.entry_driver_name = QLineEdit()
        self.entry_driver_name.setPlaceholderText("Driver name")
        self.entry_driver_name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry_driver_name.setMinimumWidth(150)
        self.entry_driver_name.setMaxLength(40)
        self.entry_driver_name.textChanged.connect(self._sanitize_driver_name)

        self.entry_driver_phone = QLineEdit()
        self.entry_driver_phone.setPlaceholderText("Phone (11 digits)")
        self.entry_driver_phone.setMaxLength(11)
        self.entry_driver_phone.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry_driver_phone.setMinimumWidth(150)
        self.entry_driver_phone.textChanged.connect(
            lambda text: self._force_digits(self.entry_driver_phone, text, 11)
        )

        self.entry_driver_cnic = QLineEdit()
        self.entry_driver_cnic.setPlaceholderText("CNIC")
        self.entry_driver_cnic.setMaxLength(15)
        self.entry_driver_cnic.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry_driver_cnic.setMinimumWidth(150)
        self.entry_driver_cnic.textChanged.connect(
            lambda text: self._force_cnic(self.entry_driver_cnic, text)
        )

        # Stable weight is intentionally kept internal to the measurement/save
        # flow. The old form field is retained for compatibility but hidden
        # from the operator; the database still receives the stable weight.
        self.entry_weight_display = QLineEdit("0.00 kg")
        self.entry_weight_display.setReadOnly(True)
        self.entry_weight_display.setVisible(False)

        for widget in (self.entry_truck_number, self.entry_driver_name,
                       self.entry_driver_phone, self.entry_driver_cnic):
            form_row.addWidget(widget, 1)

        outer.addLayout(form_row)
        outer.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.entry_save_btn = QPushButton("💾  Save Entry")
        self.entry_save_btn.setObjectName("primaryButton")
        self.entry_save_btn.setMinimumWidth(180)
        self.entry_save_btn.setMinimumHeight(42)
        self.entry_save_btn.clicked.connect(self._save_entry)
        self.entry_save_btn.setEnabled(False)
        button_row.addWidget(self.entry_save_btn)
        outer.addLayout(button_row)
        return tab

    def _sanitize_driver_name(self, text: str) -> None:
        cleaned = re.sub(r"[^A-Za-z ]", "", text).upper()[:40]
        if cleaned != text:
            cursor = min(self.entry_driver_name.cursorPosition(), len(cleaned))
            self.entry_driver_name.blockSignals(True)
            self.entry_driver_name.setText(cleaned)
            self.entry_driver_name.setCursorPosition(cursor)
            self.entry_driver_name.blockSignals(False)

    def _sanitize_truck_number(self, text: str) -> None:
        # Truck numbers may contain letters and digits together (e.g. AK92, QW018).
        cleaned = re.sub(r"[^A-Za-z0-9 /-]", "", text).upper()[:30]
        if cleaned != text:
            cursor = min(self.entry_truck_number.cursorPosition(), len(cleaned))
            self.entry_truck_number.blockSignals(True)
            self.entry_truck_number.setText(cleaned)
            self.entry_truck_number.setCursorPosition(cursor)
            self.entry_truck_number.blockSignals(False)
        self._update_entry_save_state()

    @staticmethod
    def _force_uppercase(widget: QLineEdit, text: str) -> None:
        upper = text.upper()
        if upper != text:
            cursor = widget.cursorPosition()
            widget.blockSignals(True)
            widget.setText(upper)
            widget.setCursorPosition(min(cursor, len(upper)))
            widget.blockSignals(False)

    @staticmethod
    def _force_digits(widget: QLineEdit, text: str, max_length: int) -> None:
        cleaned = "".join(ch for ch in text if ch.isdigit())[:max_length]
        if cleaned != text:
            cursor = min(widget.cursorPosition(), len(cleaned))
            widget.blockSignals(True)
            widget.setText(cleaned)
            widget.setCursorPosition(cursor)
            widget.blockSignals(False)

    def _force_cnic(self, widget: QLineEdit, text: str) -> None:
        cleaned = re.sub(r"[^0-9-]", "", text)[:15]
        if cleaned != text:
            cursor = min(widget.cursorPosition(), len(cleaned))
            widget.blockSignals(True)
            widget.setText(cleaned)
            widget.setCursorPosition(cursor)
            widget.blockSignals(False)

    def _update_entry_save_state(self) -> None:
        """Enable Entry Save only when the truck is valid and a usable live/stable weight exists."""
        if not hasattr(self, "entry_save_btn"):
            return
        truck = self.entry_truck_number.text().strip()
        valid_truck = bool(truck) and bool(re.fullmatch(r"(?=.*[A-Z0-9])[A-Z0-9 /-]{1,30}", truck))
        if self.is_measuring:
            self.entry_save_btn.setEnabled(False)
        else:
            self.entry_save_btn.setEnabled(valid_truck and (self.stable_weight > 0.0 or self.current_weight > 0.0))

    def _save_entry(self) -> None:
        truck_number = self.entry_truck_number.text().strip().upper()
        driver_name = self.entry_driver_name.text().strip().upper()
        driver_phone = self.entry_driver_phone.text().strip()
        driver_cnic = self.entry_driver_cnic.text().strip()

        if not truck_number:
            QMessageBox.warning(self, "Missing info", "Truck Number is required.")
            return
        if not re.fullmatch(r"(?=.*[A-Z0-9])[A-Z0-9 /-]{1,30}", truck_number):
            QMessageBox.warning(self, "Invalid Truck Number", "Truck number may contain letters, numbers, spaces, hyphens (-), and slashes (/).")
            self.entry_truck_number.setFocus()
            return
        if driver_phone and (not driver_phone.isdigit() or len(driver_phone) != 11):
            QMessageBox.warning(self, "Invalid Phone", "Phone number must contain exactly 11 digits.")
            self.entry_driver_phone.setFocus()
            return

        # First click: stabilize the live weight. Do not save yet.
        if self.stable_weight <= 0.0:
            duplicate = db.check_active_duplicate(truck_number)
            if duplicate:
                QMessageBox.warning(self, "Duplicate record", duplicate)
                return
            self._pending_save_action = "entry_measure_only"
            self.start_measurement()
            self.stability_status_label.setText(
                "Status: Measuring Entry weight. Save will be enabled after the weight stabilizes."
            )
            return

        # Second click: stable weight is already available, so save directly.
        self._pending_save_action = "entry"
        self._complete_pending_entry_save()

    def _complete_pending_entry_save(self) -> None:
        active_weight = self.stable_weight
        truck_number = self.entry_truck_number.text().strip().upper()
        driver_name = self.entry_driver_name.text().strip().upper()
        driver_phone = self.entry_driver_phone.text().strip()
        driver_cnic = self.entry_driver_cnic.text().strip()

        confirm_text = (
            f"Confirm saving this ENTRY record?\n\n"
            f"Truck Number: {truck_number}\n"
            f"Driver: {driver_name or '--'}\n"
            f"Entry Weight: {active_weight:.2f} kg\n"
            f"Phone: {driver_phone or '--'}"
        )
        answer = QMessageBox.question(
            self, "Confirm Entry", confirm_text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            self._pending_save_action = None
            self._resume_live_weight()
            return

        try:
            entry_id = db.create_truck_entry(
                truck_number, driver_name, driver_cnic, driver_phone, active_weight
            )
        except Exception as exc:
            QMessageBox.critical(self, "Entry Save Failed",
                                 f"The Entry could not be saved to the local database.\n\n{exc}")
            self._pending_save_action = None
            self._resume_live_weight()
            return

        QMessageBox.information(
            self, "Entry saved",
            f"Truck {truck_number} entry saved locally — status: Unsync, weight {active_weight:.2f} kg.\n"
            f"Entry ID: {entry_id}\nIt will sync to the server automatically."
        )
        self.entry_truck_number.clear()
        self.entry_driver_name.clear()
        self.entry_driver_phone.clear()
        self.entry_driver_cnic.clear()
        self._pending_save_action = None
        self._refresh_pending_combo()
        self.update_statistics_display()
        # Do not trigger an immediate sync after saving an Entry/Exit.
        # SyncManager performs automatic bulk synchronization every 300 seconds.
        self._resume_live_weight()
        self._reset_readout_after_save()

    def _build_exit_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("modernForm")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        form_row = QHBoxLayout()
        form_row.setSpacing(12)

        self.exit_truck_combo = QComboBox()
        self.exit_truck_combo.setEditable(True)
        self.exit_truck_combo.setInsertPolicy(QComboBox.NoInsert)
        self.exit_truck_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.exit_truck_combo.setMinimumWidth(190)
        self.exit_truck_combo.currentIndexChanged.connect(self._on_exit_selection_changed)
        self.exit_truck_combo.lineEdit().textEdited.connect(self._filter_pending_trucks)

        refresh_btn = QPushButton("🔄")
        refresh_btn.setFixedSize(44, 42)
        refresh_btn.setToolTip("Refresh List")
        refresh_btn.clicked.connect(self._refresh_pending_combo)

        self.exit_entry_weight_display = QLineEdit("--")
        self.exit_entry_weight_display.setReadOnly(True)
        self.exit_entry_weight_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.exit_entry_weight_display.setMinimumWidth(150)

        self.exit_weight_display = QLineEdit("0.00 kg")
        self.exit_weight_display.setReadOnly(True)
        self.exit_weight_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.exit_weight_display.setMinimumWidth(150)

        self.exit_net_weight_display = QLineEdit("--")
        self.exit_net_weight_display.setReadOnly(True)
        self.exit_net_weight_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.exit_net_weight_display.setMinimumWidth(150)

        truck_box = QHBoxLayout()
        truck_box.setSpacing(6)
        truck_box.addWidget(self.exit_truck_combo, 1)
        truck_box.addWidget(refresh_btn)
        form_row.addLayout(truck_box, 2)
        form_row.addWidget(self.exit_entry_weight_display, 1)
        form_row.addWidget(self.exit_weight_display, 1)
        form_row.addWidget(self.exit_net_weight_display, 1)
        outer.addLayout(form_row)
        outer.addStretch(1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.exit_save_btn = QPushButton("💾  Save Exit")
        self.exit_save_btn.setObjectName("primaryButton")
        self.exit_save_btn.setMinimumWidth(180)
        self.exit_save_btn.setMinimumHeight(42)
        self.exit_save_btn.clicked.connect(self._save_exit)
        self.exit_save_btn.setEnabled(False)
        button_row.addWidget(self.exit_save_btn)
        outer.addLayout(button_row)
        return tab

    def _on_tab_changed(self, index: int) -> None:
        if index == 1:
            self._refresh_pending_combo()
            self._pending_save_action = None
            self.exit_save_btn.setEnabled(False)
            # Every time Exit is opened, take one fresh stability measurement.
            self.start_measurement()

    def _refresh_pending_combo(self) -> None:
        """Show every locally-entered truck that has not completed an Exit.

        Exit is local-only. Synced and Unsynced Entry rows are both shown.
        """
        self._pending_records = db.fetch_pending_entries()
        self._populate_combo_items(self._pending_records)

    def _populate_combo_items(self, records) -> None:
        self.exit_truck_combo.blockSignals(True)
        self.exit_truck_combo.clear()
        if not records:
            self.exit_truck_combo.addItem("No pending trucks found", None)
        else:
            for rec in records:
                self.exit_truck_combo.addItem(f"{rec['truck_number']} — {rec['driver_name']}", rec["id"])
            self.exit_truck_combo.setCurrentIndex(-1)
            self.exit_truck_combo.setEditText("")
        self.exit_truck_combo.blockSignals(False)
        self._on_exit_selection_changed(self.exit_truck_combo.currentIndex())

    def _filter_pending_trucks(self, query: str) -> None:
        q = query.strip().lower()
        if not q:
            filtered = self._pending_records
        else:
            filtered = [
                rec for rec in self._pending_records
                if q in rec["truck_number"].lower() or q in rec["driver_name"].lower()
            ]
        self.exit_truck_combo.blockSignals(True)
        current_text = self.exit_truck_combo.lineEdit().text()
        self.exit_truck_combo.clear()
        if not filtered:
            self.exit_truck_combo.addItem("No matching trucks found", None)
        else:
            for rec in filtered:
                self.exit_truck_combo.addItem(f"{rec['truck_number']} — {rec['driver_name']}", rec["id"])
        self.exit_truck_combo.setEditText(current_text)
        self.exit_truck_combo.blockSignals(False)

    def _on_exit_selection_changed(self, index: int) -> None:
        record_id = self.exit_truck_combo.currentData()
        if record_id is None:
            self._selected_exit_id = None
            self.exit_save_btn.setEnabled(False)
            self._selected_exit_truck_number = None
            self._selected_entry_weight = None
            self._selected_driver_phone = None
            self._selected_driver_name = None
            self.exit_entry_weight_display.setText("--")
            self.exit_net_weight_display.setText("--")
            return
        match = next((r for r in self._pending_records if r["id"] == record_id), None)
        if match is None:
            return
        self._selected_exit_id = match["id"]
        self._selected_exit_truck_number = match["truck_number"]
        self._selected_entry_weight = match["entry_weight"]
        self._selected_driver_phone = match["driver_phone"] if "driver_phone" in match.keys() else None
        self._selected_driver_name = match["driver_name"] if "driver_name" in match.keys() else None
        if match["entry_weight"] is None:
            # No local Entry weight is available for this record;
            # the server will compute net weight from its own Entry record.
            self.exit_entry_weight_display.setText("(server will calculate)")
        else:
            self.exit_entry_weight_display.setText(f"{match['entry_weight']:.2f} kg")
        self.exit_save_btn.setEnabled((not self.is_measuring) and self.stable_weight > 0.0)
        self._recalculate_net_weight()

    def _recalculate_net_weight(self) -> None:
        active_weight = self.stable_weight
        if self._selected_entry_weight is None or active_weight <= 0.0:
            self.exit_net_weight_display.setText("--")
            return
        net = abs(float(active_weight) - float(self._selected_entry_weight))
        self.exit_net_weight_display.setText(f"{net:.2f} kg")

    def _save_exit(self) -> None:
        if self._selected_exit_id is None:
            QMessageBox.warning(self, "No truck selected", "Select the truck that is exiting first.")
            return
        if self._selected_entry_weight is None:
            QMessageBox.warning(self, "Entry Weight Missing", "The selected truck has no valid Entry weight.")
            return
        if self.stable_weight <= 0.0 or self.is_measuring:
            return

        # Exit form measures automatically when opened. Once stable, this click
        # saves that already-calculated stable weight; it does NOT measure again.
        self._pending_save_action = "exit"
        self._complete_pending_exit_save()

    def _complete_pending_exit_save(self) -> None:
        exit_weight = float(self.stable_weight)
        net = (
            abs(exit_weight - float(self._selected_entry_weight))
            if self._selected_entry_weight is not None else None
        )
        confirm_text = (
            f"Confirm saving this EXIT record?\n\n"
            f"Truck Number: {self._selected_exit_truck_number or '--'}\n"
            f"Entry Weight: {float(self._selected_entry_weight):.2f} kg\n"
            f"Exit Weight: {exit_weight:.2f} kg\n"
            f"Net Weight: {net:.2f} kg" if net is not None else
            f"Confirm saving this EXIT record?\n\n"
            f"Truck Number: {self._selected_exit_truck_number or '--'}\n"
            f"Exit Weight: {exit_weight:.2f} kg\nNet Weight: calculated by server"
        )
        answer = QMessageBox.question(
            self, "Confirm Exit", confirm_text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if answer != QMessageBox.Yes:
            self._pending_save_action = None
            return

        try:
            db.complete_truck_exit(
                self._selected_exit_id,
                self._selected_exit_truck_number,
                exit_weight,
                net,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Exit Save Failed", f"The Exit could not be saved.\n\n{exc}")
            self._pending_save_action = None
            self._resume_live_weight()
            return

        if net is not None:
            QMessageBox.information(
                self, "Exit saved",
                f"Exit saved locally — exit weight {exit_weight:.2f} kg, net weight {net:.2f} kg."
            )
        else:
            QMessageBox.information(
                self, "Exit saved",
                f"Exit saved locally — exit weight {exit_weight:.2f} kg. Queued to sync; "
                f"net weight will be calculated by the server."
            )
        self._selected_exit_id = None
        self._selected_exit_truck_number = None
        self._selected_entry_weight = None
        self._selected_driver_phone = None
        self._selected_driver_name = None
        self.exit_entry_weight_display.setText("--")
        self.exit_weight_display.setText("0.00 kg")
        self.exit_net_weight_display.setText("--")
        self._pending_save_action = None
        self._refresh_pending_combo()
        self.update_statistics_display()
        # Do not trigger an immediate sync after saving an Entry/Exit.
        # SyncManager performs automatic bulk synchronization every 300 seconds.
        self._resume_live_weight()
        self._reset_readout_after_save()

    # --- Measurement Logic (10s average + 60s auto-resume) ---

    def start_measurement(self) -> None:
        self._zero_readout_after_save = False
        if self.measurement_timer:
            self.measurement_timer.stop()
            self.measurement_timer = None
        if self.stable_display_timer:
            self.stable_display_timer.stop()
            self.stable_display_timer = None
        self.is_measuring = True
        self.measurement_readings = []
        if hasattr(self, "entry_save_btn"):
            self.entry_save_btn.setEnabled(False)
        if hasattr(self, "exit_save_btn"):
            self.exit_save_btn.setEnabled(False)
        self.stable_weight = 0.0
        self._measurement_elapsed = 0

        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Measuring: %v sec / 10")

        self.stability_status_label.setText("Status: Measuring weight (collecting 10s average)...")
        self.stability_status_label.setStyleSheet("color: #2563eb; font-weight: bold; font-size: 13px;")

        self.calculate_btn.setText("⏹ Stop")
        self.calculate_btn.setObjectName("dangerButton")
        self.calculate_btn.style().unpolish(self.calculate_btn)
        self.calculate_btn.style().polish(self.calculate_btn)

        self.measurement_timer = QTimer(self)
        self.measurement_timer.setInterval(1000)
        self.measurement_timer.timeout.connect(self._on_measurement_tick)
        self.measurement_timer.start()

    def _on_measurement_tick(self) -> None:
        self._measurement_elapsed += 1
        self.progress_bar.setValue(self._measurement_elapsed)
        if self._measurement_elapsed >= 10:
            self._finish_measurement()

    def _finish_measurement(self) -> None:
        if self.measurement_timer:
            self.measurement_timer.stop()
            self.measurement_timer = None
            # remove  average function
            self.stable_weight = self.current_weight

        self.is_measuring = False
        self.progress_bar.setVisible(False)

        self.weight_label.setText(f"{self.stable_weight:.2f} kg (Stable)")
        if hasattr(self, "entry_save_btn"):
            self._update_entry_save_state()
        if hasattr(self, "exit_save_btn"):
            self.exit_save_btn.setEnabled(self._selected_exit_id is not None and self.stable_weight > 0.0)
        self.stability_status_label.setText(f"Status: Stable weight locked — {self.stable_weight:.2f} kg (auto-resume in 60s)")
        self.stability_status_label.setStyleSheet("color: #16a34a; font-weight: bold; font-size: 13px;")

        self.entry_weight_display.setText(f"{self.stable_weight:.2f} kg")
        self.exit_weight_display.setText(f"{self.stable_weight:.2f} kg")
        self._recalculate_net_weight()

        db.insert_audit_log("WEIGHT_CAPTURE", f"Stable weight captured: {self.stable_weight:.2f} kg (latest live reading at measurement finish).")

        pending_action = self._pending_save_action
        if pending_action == "entry_measure_only":
            # After the first Save click, measure for 10 seconds and then
            # immediately show the confirmation dialog (no second click needed).
            self._pending_save_action = "entry"
            self._complete_pending_entry_save()
            return
        if pending_action == "entry":
            self._complete_pending_entry_save()
            return
        if pending_action == "exit":
            self._complete_pending_exit_save()
            return

        self.calculate_btn.setText("⚖ Calculate Weight (10s)")
        self.calculate_btn.setObjectName("primaryButton")
        self.calculate_btn.style().unpolish(self.calculate_btn)
        self.calculate_btn.style().polish(self.calculate_btn)

        self.stable_display_timer = QTimer(self)
        self.stable_display_timer.setSingleShot(True)
        self.stable_display_timer.setInterval(60000)
        self.stable_display_timer.timeout.connect(self._auto_resume_live)
        self.stable_display_timer.start()

    def _auto_resume_live(self) -> None:
        # Keep the last captured stable transaction weight latched until the
        # operator starts a new measurement. Live readings may resume on the
        # dashboard, but they must never invalidate a captured transaction weight.
        self.stability_status_label.setText(
            f"Status: Stable weight available — {self.stable_weight:.2f} kg (click Calculate for a new reading)"
        )
        self.stability_status_label.setStyleSheet("color: #16a34a; font-weight: bold; font-size: 13px;")
        self.weight_label.setText(f"{self.current_weight:.2f} kg (Live) | Stable: {self.stable_weight:.2f} kg")
        self.entry_weight_display.setText(f"{self.stable_weight:.2f} kg")
        self.exit_weight_display.setText(f"{self.stable_weight:.2f} kg")
        self._recalculate_net_weight()

    def _resume_live_weight(self) -> None:
        if self.stable_display_timer:
            self.stable_display_timer.stop()
            self.stable_display_timer = None
        self.stable_weight = 0.0
        self.is_measuring = False
        self.progress_bar.setVisible(False)
        if hasattr(self, "entry_save_btn"):
            self._update_entry_save_state()
        if hasattr(self, "exit_save_btn"):
            self.exit_save_btn.setEnabled(self._selected_exit_id is not None and self.stable_weight > 0.0)
        self.stability_status_label.setText("Status: Live weight resumed")
        self.stability_status_label.setStyleSheet("color: #eab308; font-weight: bold; font-size: 13px;")
        self.weight_label.setText(f"{self.current_weight:.2f} kg (Live)")
        self.entry_weight_display.setText(f"{self.current_weight:.2f} kg")
        self.exit_weight_display.setText(f"{self.current_weight:.2f} kg")
        self._recalculate_net_weight()
        self.calculate_btn.setText("⚖ Calculate Weight (10s)")
        self.calculate_btn.setObjectName("primaryButton")
        self.calculate_btn.style().unpolish(self.calculate_btn)
        self.calculate_btn.style().polish(self.calculate_btn)

    def _reset_readout_after_save(self) -> None:
        """Show zero after a record is saved, even if the truck remains on the scale.

        The serial reader continues updating current_weight in the background.
        Starting the next measurement releases this display latch.
        """
        self._zero_readout_after_save = True
        self.stable_weight = 0.0
        self.weight_label.setText("0.00 kg")
        self.entry_weight_display.setText("0.00 kg")
        self.exit_weight_display.setText("0.00 kg")
        self.exit_net_weight_display.setText("--")
        self.stability_status_label.setText(
            "Status: Record saved — display reset to 0.00 kg. Start next measurement when ready."
        )
        self.stability_status_label.setStyleSheet(
            "color: #eab308; font-weight: bold; font-size: 13px;"
        )
        if hasattr(self, "entry_save_btn"):
            self._update_entry_save_state()
        if hasattr(self, "exit_save_btn"):
            self.exit_save_btn.setEnabled(False)

    def stop_measurement(self) -> None:
        self._pending_save_action = None
        if self.measurement_timer:
            self.measurement_timer.stop()
            self.measurement_timer = None
        self.is_measuring = False
        self.progress_bar.setVisible(False)
        if hasattr(self, "entry_save_btn"):
            self._update_entry_save_state()
        if hasattr(self, "exit_save_btn"):
            self.exit_save_btn.setEnabled(self._selected_exit_id is not None and self.stable_weight > 0.0)
        self.stability_status_label.setText("Status: Measurement stopped — showing live weight")
        self.stability_status_label.setStyleSheet("color: #eab308; font-weight: bold; font-size: 13px;")
        self.calculate_btn.setText("⚖ Calculate Weight (10s)")
        self.calculate_btn.setObjectName("primaryButton")
        self.calculate_btn.style().unpolish(self.calculate_btn)
        self.calculate_btn.style().polish(self.calculate_btn)

    def update_live_weight(self, weight: float) -> None:
        self._check_abnormal_weight(weight)
        self.current_weight = weight

        # Keep the dashboard at zero after a successful save until a new
        # measurement is explicitly started. The actual live value is retained
        # in current_weight for the next capture.
        if self._zero_readout_after_save and not self.is_measuring:
            self.weight_label.setText("0.00 kg")
            return

        if self.is_measuring:
            self.measurement_readings.append(weight)
            if hasattr(self, "entry_save_btn"):
                self.entry_save_btn.setEnabled(False)
            if hasattr(self, "exit_save_btn"):
                self.exit_save_btn.setEnabled(False)
            self.weight_label.setText(f"{weight:.2f} kg (Measuring...)")
        else:
            # The large dashboard readout always reflects the actual live value,
            # including 0.00 kg. A captured stable_weight remains separate and
            # is still used for the pending transaction until a new measurement.
            self.weight_label.setText(f"{weight:.2f} kg (Live)")
            if self.stable_weight <= 0.0:
                if hasattr(self, "entry_save_btn"):
                    self._update_entry_save_state()
                if hasattr(self, "exit_save_btn"):
                    self.exit_save_btn.setEnabled(self._selected_exit_id is not None and weight > 0)
                self.entry_weight_display.setText(f"{weight:.2f} kg")
                self.exit_weight_display.setText(f"{weight:.2f} kg")
                self._recalculate_net_weight()

    def _check_abnormal_weight(self, weight: float) -> None:
        """Flags out-of-range readings or implausible single-tick jumps.
        Debounced so a sustained bad reading raises one alert every
        ABNORMAL_ALERT_COOLDOWN_SECS rather than once a second."""
        import time
        reason = None
        if weight < MIN_PLAUSIBLE_WEIGHT_KG or weight > MAX_PLAUSIBLE_WEIGHT_KG:
            reason = f"Weight {weight:.2f} kg is outside the plausible range (0–{MAX_PLAUSIBLE_WEIGHT_KG:.0f} kg)."
        elif self._last_weight_for_jump_check is not None:
            jump = abs(weight - self._last_weight_for_jump_check)
            if jump > MAX_JUMP_PER_READING_KG:
                reason = f"Weight jumped {jump:.2f} kg in one reading ({self._last_weight_for_jump_check:.2f} → {weight:.2f} kg)."
        self._last_weight_for_jump_check = weight

        if reason:
            now = time.time()
            if now - self._last_abnormal_alert_at >= ABNORMAL_ALERT_COOLDOWN_SECS:
                self._last_abnormal_alert_at = now
                db.insert_audit_log("ABNORMAL_WEIGHT", reason)
                self.on_notify("error", f"Abnormal weight detected — {reason}")

    def update_status(self, level: str, message: str) -> None:
        color = STATUS_COLORS.get(level, "#64748b")
        icon = {"ok": "●", "warning": "●", "error": "●"}.get(level, "●")
        self.status_label.setText(f"{icon} {message}")
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")


# ==============================================================================
# SCREEN: RECORDS, CONTINUOUS LOG & AUDIT TRAIL
# ==============================================================================

class RecordsScreen(QWidget):
    """
    Record Section.

    Entry and Exit are stored as separate transactions, so they are
    listed here as independent rows too (each tagged Entry/Exit) —
    but every row carries a Ref ID, so the related Entry/Exit history
    for a truck can still be traced by matching that column.

    Filters available:
      - Truck number (free text, matches partial)
      - From / To Date-Time (matches each row's own entry_time or
        exit_time, whichever applies)
      - Entry / Exit type
      - Sync / Unsync status
    """

    def __init__(self, on_sync_now):
        super().__init__()
        self.on_sync_now = on_sync_now
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        title = QLabel("Truck Records")
        title.setObjectName("navTitle")

        self.sync_btn = QPushButton("☁ Sync Now")
        self.sync_btn.clicked.connect(self.on_sync_now)

        self.view_log_btn = QPushButton("📦 Weight Log")
        self.view_log_btn.clicked.connect(self._show_continuous_log)

        self.view_audit_btn = QPushButton("🧾 Audit Trail")
        self.view_audit_btn.clicked.connect(self._show_audit_log)

        self.print_selected_btn = QPushButton("🖨 Print Selected (A4)")
        self.print_selected_btn.setToolTip("Print the selected record on A4 paper")
        self.print_selected_btn.clicked.connect(self._print_selected_record)

        self.print_daily_btn = QPushButton("📄 Print Daily Report (A4)")
        self.print_daily_btn.setToolTip("Print today's records on A4 landscape paper")
        self.print_daily_btn.clicked.connect(self._print_daily_report)

        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(self.sync_btn)
        toolbar.addWidget(self.view_log_btn)
        toolbar.addWidget(self.view_audit_btn)
        toolbar.addWidget(self.print_selected_btn)
        toolbar.addWidget(self.print_daily_btn)
        root.addLayout(toolbar)

        # Sync Now is unavailable whenever API sync is turned OFF.
        self.set_sync_enabled = lambda enabled: self.sync_btn.setEnabled(bool(enabled))

        # --- Filter bar ---
        filter_card = _card()
        filter_layout = QHBoxLayout(filter_card)
        filter_layout.setSpacing(10)
        filter_layout.setContentsMargins(14, 10, 14, 10)

        self.filter_truck_number = QLineEdit()
        self.filter_truck_number.setPlaceholderText("Truck number...")
        self.filter_truck_number.setMaximumWidth(150)
        self.filter_truck_number.textChanged.connect(self.refresh)

        self.filter_use_date_range = QCheckBox("Date/Time range")
        self.filter_use_date_range.toggled.connect(self._on_date_range_toggled)
        self.filter_use_date_range.toggled.connect(self.refresh)

        self.filter_date_from = QDateTimeEdit()
        self.filter_date_from.setCalendarPopup(True)
        self.filter_date_from.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.filter_date_from.setDateTime(QDateTime.currentDateTime().addDays(-7))
        self.filter_date_from.setEnabled(False)
        self.filter_date_from.dateTimeChanged.connect(self.refresh)

        self.filter_date_to = QDateTimeEdit()
        self.filter_date_to.setCalendarPopup(True)
        self.filter_date_to.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.filter_date_to.setDateTime(QDateTime.currentDateTime())
        self.filter_date_to.setEnabled(False)
        self.filter_date_to.dateTimeChanged.connect(self.refresh)

        self.filter_type = QComboBox()
        self.filter_type.addItem("Entry + Exit", "")
        self.filter_type.addItem("Entry only", "Entry")
        self.filter_type.addItem("Exit only", "Exit")
        self.filter_type.currentIndexChanged.connect(self.refresh)

        self.filter_sync = QComboBox()
        self.filter_sync.addItem("Sync + Unsync", "")
        self.filter_sync.addItem("Synced", "Sync")
        self.filter_sync.addItem("Unsynced", "Unsync")
        self.filter_sync.currentIndexChanged.connect(self.refresh)

        clear_btn = QPushButton("Clear Filters")
        clear_btn.clicked.connect(self._clear_filters)

        filter_layout.addWidget(QLabel("Truck No"))
        filter_layout.addWidget(self.filter_truck_number)
        filter_layout.addWidget(self.filter_use_date_range)
        filter_layout.addWidget(QLabel("From"))
        filter_layout.addWidget(self.filter_date_from)
        filter_layout.addWidget(QLabel("To"))
        filter_layout.addWidget(self.filter_date_to)
        filter_layout.addWidget(QLabel("Type"))
        filter_layout.addWidget(self.filter_type)
        filter_layout.addWidget(QLabel("Sync"))
        filter_layout.addWidget(self.filter_sync)
        filter_layout.addWidget(clear_btn)
        filter_layout.addStretch()
        root.addWidget(filter_card)

        self.result_count_label = QLabel("")
        self.result_count_label.setObjectName("caption")
        root.addWidget(self.result_count_label)

        self.table = QTableWidget(0, 14)
        self.table.setHorizontalHeaderLabels([
            "Print?", "ID", "Entry ID", "Type", "Truck No", "Driver", "CNIC",
            "Entry Wt", "Entry Time", "Exit Wt", "Exit Time", "Net Wt",
            "Sync", "Time"
        ])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        root.addWidget(self.table)

        self._dialog_refs = []  # keep references so popup dialogs aren't garbage-collected

    def _on_date_range_toggled(self, checked: bool) -> None:
        self.filter_date_from.setEnabled(checked)
        self.filter_date_to.setEnabled(checked)

    def _clear_filters(self) -> None:
        self.filter_truck_number.blockSignals(True)
        self.filter_truck_number.clear()
        self.filter_truck_number.blockSignals(False)

        self.filter_use_date_range.blockSignals(True)
        self.filter_use_date_range.setChecked(False)
        self.filter_use_date_range.blockSignals(False)
        self._on_date_range_toggled(False)

        self.filter_type.blockSignals(True)
        self.filter_type.setCurrentIndex(0)
        self.filter_type.blockSignals(False)

        self.filter_sync.blockSignals(True)
        self.filter_sync.setCurrentIndex(0)
        self.filter_sync.blockSignals(False)

        self.refresh()

    def refresh(self) -> None:
        date_from = ""
        date_to = ""
        if self.filter_use_date_range.isChecked():
            date_from = self.filter_date_from.dateTime().toString("yyyy-MM-dd HH:mm:ss")
            date_to = self.filter_date_to.dateTime().toString("yyyy-MM-dd HH:mm:ss")

        records = db.fetch_filtered_records(
            date_from=date_from,
            date_to=date_to,
            truck_number=self.filter_truck_number.text(),
            sync_status=self.filter_sync.currentData() or "",
            record_type=self.filter_type.currentData() or "",
        )

        self.result_count_label.setText(f"{len(records)} record(s) found")

        self.table.setRowCount(len(records))
        for row_idx, rec in enumerate(records):
            values = [
                rec["id"],
                rec["entry_id"],
                rec["record_type"],
                rec["truck_number"],
                rec["driver_name"] if rec["driver_name"] is not None else "--",
                rec["driver_cnic"] if rec["driver_cnic"] is not None else "--",
                f"{rec['entry_weight']:.2f}" if rec["entry_weight"] is not None else "--",
                rec["entry_time"] if rec["entry_time"] is not None else "--",
                f"{rec['exit_weight']:.2f}" if rec["exit_weight"] is not None else "--",
                rec["exit_time"] if rec["exit_time"] is not None else "--",
                f"{rec['net_weight']:.2f}" if rec["net_weight"] is not None else "--",
                rec["sync_status"],
                rec["record_time"],
            ]
            check_item = QTableWidgetItem()
            check_item.setFlags((check_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            check_item.setCheckState(Qt.Unchecked)
            self.table.setItem(row_idx, 0, check_item)
            for col_idx, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value))
                if col_idx == 3:  # Type column (offset by checkbox)
                    item.setForeground(Qt.darkBlue if rec["record_type"] == "Entry" else Qt.darkMagenta)
                if col_idx == 12:  # Sync column (offset by checkbox)
                    if rec["sync_status"] == "Sync":
                        item.setText("✓")
                        item.setTextAlignment(Qt.AlignCenter)
                        item.setForeground(Qt.darkGreen)
                        item.setToolTip("Synced")
                    else:
                        item.setText("✕")
                        item.setTextAlignment(Qt.AlignCenter)
                        item.setForeground(Qt.darkRed)
                        item.setToolTip(rec["sync_error"] or "Unsynced")
                self.table.setItem(row_idx, col_idx, item)

    def _print_html(self, title: str, html: str, landscape: bool = False) -> None:
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        # Qt 6 / PySide6 uses QPageSize and QPageLayout enums; QPrinter.A4
        # and QPrinter.setOrientation() are not available in this API.
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setPageOrientation(
            QPageLayout.Orientation.Landscape if landscape
            else QPageLayout.Orientation.Portrait
        )
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle(f"Print {title}")
        if dialog.exec() != QPrintDialog.Accepted:
            return
        document = QTextDocument()
        document.setDocumentMargin(28)
        document.setHtml(html)
        document.print_(printer)

    @staticmethod
    def _html_escape(value) -> str:
        text = "" if value is None else str(value)
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;"))

    def _print_selected_record(self) -> None:
        checked_rows = [
            row for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
            and self.table.item(row, 0).checkState() == Qt.Checked
        ]
        if not checked_rows:
            QMessageBox.warning(
                self, "No Records Checked",
                "Tick the checkbox in the Print? column for at least one record, then click Print Selected (A4)."
            )
            return

        headers = [self.table.horizontalHeaderItem(c).text() for c in range(1, self.table.columnCount())]
        pages = []
        for row in checked_rows:
            values = []
            for col in range(1, self.table.columnCount()):
                item = self.table.item(row, col)
                values.append(item.text() if item else "")
            pairs = "".join(
                f"<tr><th>{self._html_escape(h)}</th><td>{self._html_escape(v)}</td></tr>"
                for h, v in zip(headers, values)
            )
            pages.append(
                "<section class='record-page'><h1>Weighbridge Record</h1>"
                f"<table>{pairs}</table><p class='foot'>Operator Signature: ____________________</p></section>"
            )
        html = f"""
        <html><head><style>body{{font-family:Arial;font-size:11pt}} h1{{text-align:center}}
        table{{width:100%;border-collapse:collapse}} th,td{{border:1px solid #555;padding:7px;text-align:left}}
        th{{width:30%;background:#eee}} .foot{{margin-top:30px}}
        .record-page{{page-break-after:always}} .record-page:last-child{{page-break-after:auto}}</style></head><body>
        <p>Printed: {self._html_escape(QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm:ss'))}</p>
        {''.join(pages)}
        </body></html>
        """
        self._print_html(f"{len(checked_rows)} Selected Record(s)", html, landscape=False)

    def _print_daily_report(self) -> None:
        today = QDateTime.currentDateTime().date()
        date_from = f"{today.toString('yyyy-MM-dd')} 00:00:00"
        date_to = f"{today.toString('yyyy-MM-dd')} 23:59:59"
        records = db.fetch_filtered_records(date_from=date_from, date_to=date_to)
        if not records:
            QMessageBox.information(self, "Daily Report", "No records were found for today.")
            return
        headers = ["ID", "Entry ID", "Type", "Truck No", "Driver", "CNIC", "Entry Wt", "Entry Time", "Exit Wt", "Exit Time", "Net Wt", "Sync", "Time"]
        rows_html = []
        for rec in records:
            values = [
                rec["id"], rec["entry_id"], rec["record_type"], rec["truck_number"],
                rec["driver_name"] if rec["driver_name"] is not None else "--",
                rec["driver_cnic"] if rec["driver_cnic"] is not None else "--",
                f"{rec['entry_weight']:.2f}" if rec["entry_weight"] is not None else "--",
                rec["entry_time"] if rec["entry_time"] is not None else "--",
                f"{rec['exit_weight']:.2f}" if rec["exit_weight"] is not None else "--",
                rec["exit_time"] if rec["exit_time"] is not None else "--",
                f"{rec['net_weight']:.2f}" if rec["net_weight"] is not None else "--",
                rec["sync_status"], rec["record_time"],
            ]
            rows_html.append("<tr>" + "".join(f"<td>{self._html_escape(v)}</td>" for v in values) + "</tr>")
        header_html = "".join(f"<th>{self._html_escape(h)}</th>" for h in headers)
        html = f"""
        <html><head><style>body{{font-family:Arial;font-size:8pt}} h1{{text-align:center;font-size:16pt}}
        table{{width:100%;border-collapse:collapse}} th,td{{border:1px solid #555;padding:4px;text-align:left}}
        th{{background:#e5e7eb}} .meta{{margin-bottom:10px}}</style></head><body>
        <h1>Daily Weighbridge Report</h1><div class='meta'>Date: {today.toString('yyyy-MM-dd')} &nbsp; | &nbsp; Total records: {len(records)}
        &nbsp; | &nbsp; Printed: {self._html_escape(QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm:ss'))}</div>
        <table><thead><tr>{header_html}</tr></thead><tbody>{''.join(rows_html)}</tbody></table></body></html>
        """
        self._print_html("Daily Report", html, landscape=True)

    def _show_continuous_log(self) -> None:
        logs = db.fetch_weight_logs()
        dialog = QWidget(None, Qt.Window)
        dialog.setWindowTitle("Continuous Weight Log")
        dialog.resize(600, 400)
        layout = QVBoxLayout(dialog)

        lbl = QLabel("Incoming raw weights logged continuously with precise timestamps:")
        layout.addWidget(lbl)

        log_table = QTableWidget(len(logs), 3)
        log_table.setHorizontalHeaderLabels(["Log ID", "Weight (kg)", "Timestamp"])
        log_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r_idx, log in enumerate(logs):
            log_table.setItem(r_idx, 0, QTableWidgetItem(str(log["id"])))
            log_table.setItem(r_idx, 1, QTableWidgetItem(f"{log['weight']:.2f}"))
            log_table.setItem(r_idx, 2, QTableWidgetItem(str(log["logged_time"])))
        layout.addWidget(log_table)
        dialog.show()
        self._dialog_refs.append(dialog)

    def _show_audit_log(self) -> None:
        entries = db.fetch_audit_log()
        dialog = QWidget(None, Qt.Window)
        dialog.setWindowTitle("Audit Trail")
        dialog.resize(760, 460)
        layout = QVBoxLayout(dialog)

        lbl = QLabel("Every entry, exit, weight capture, sync, and backup event — most recent first:")
        layout.addWidget(lbl)

        audit_table = QTableWidget(len(entries), 4)
        audit_table.setHorizontalHeaderLabels(["Timestamp", "Action", "Details", "Record ID"])
        audit_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        audit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for r_idx, entry in enumerate(entries):
            audit_table.setItem(r_idx, 0, QTableWidgetItem(str(entry["timestamp"])))
            audit_table.setItem(r_idx, 1, QTableWidgetItem(str(entry["action"])))
            audit_table.setItem(r_idx, 2, QTableWidgetItem(str(entry["details"] or "")))
            audit_table.setItem(r_idx, 3, QTableWidgetItem(str(entry["record_id"]) if entry["record_id"] else "--"))
        layout.addWidget(audit_table)
        dialog.show()
        self._dialog_refs.append(dialog)


# ==============================================================================
# SCREEN: SETTINGS (with Auto-Detect, Sync & Backup controls)
# ==============================================================================

class SettingsScreen(QWidget):
    def __init__(self, settings: dict, on_save, on_sync_now, on_backup_now, on_auto_detect, on_auto_sync_changed=None):
        super().__init__()
        self.setObjectName("settingsScreen")
        self.settings = settings
        # Settings inputs use a responsive short width (about 40% of the
        # available settings content area) while remaining usable on small screens.
        self._settings_field_widgets = []
        self.on_save = on_save
        self.on_sync_now = on_sync_now
        self.on_backup_now = on_backup_now
        self.on_auto_detect = on_auto_detect
        self.on_auto_sync_changed = on_auto_sync_changed or (lambda enabled: None)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(16)

        # --- Weighbridge hardware card ---
        card = _card()
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        form = QFormLayout(card)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(12)

        self.com_port_combo = QComboBox()
        self._populate_ports()

        auto_detect_btn = QPushButton("⚡ Auto-Detect")
        auto_detect_btn.clicked.connect(self._auto_detect_hardware)

        port_row = QHBoxLayout()
        port_row.addWidget(self.com_port_combo)
        port_row.addWidget(auto_detect_btn)
        port_row.addStretch(1)
        form.addRow("COM Port", port_row)
        self._settings_field_widgets.append(self.com_port_combo)

        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText(str(self.settings.get("baud_rate", 9600)))
        form.addRow("Baud Rate", self.baud_combo)
        self._settings_field_widgets.append(self.baud_combo)

        self.parity_combo = QComboBox()
        self.parity_combo.addItems(["None", "Even", "Odd"])
        self.parity_combo.setCurrentText(self.settings.get("parity", "None"))
        form.addRow("Parity Bit", self.parity_combo)
        self._settings_field_widgets.append(self.parity_combo)

        self.bytesize_combo = QComboBox()
        self.bytesize_combo.addItems(["7", "8"])
        self.bytesize_combo.setCurrentText(str(self.settings.get("bytesize", 8)))
        form.addRow("Data Bits", self.bytesize_combo)
        self._settings_field_widgets.append(self.bytesize_combo)

        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItems(["1", "1.5", "2"])
        self.stopbits_combo.setCurrentText(str(self.settings.get("stopbits", 1)))
        form.addRow("Stop Bits", self.stopbits_combo)
        self._settings_field_widgets.append(self.stopbits_combo)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Light", "Dark"])
        self.theme_combo.setCurrentText(self.settings.get("theme", "Light"))
        form.addRow("Theme Mode", self.theme_combo)
        self._settings_field_widgets.append(self.theme_combo)

        self.factory_number_input = QLineEdit()
        self.factory_number_input.setPlaceholderText("Factory number")
        self.factory_number_input.setText(str(self.settings.get("company_id") or self.settings.get("factory_number", "")))
        form.addRow("Factory Number", self.factory_number_input)
        self._settings_field_widgets.append(self.factory_number_input)

        self.machine_number_input = QLineEdit()
        self.machine_number_input.setPlaceholderText("Machine number")
        self.machine_number_input.setText(str(self.settings.get("machine_number", "")))
        form.addRow("Machine Number", self.machine_number_input)
        self._settings_field_widgets.append(self.machine_number_input)

        self.server_url_input = QLineEdit()
        self.server_url_input.setPlaceholderText("https://server.example.com/api/sync")
        self.server_url_input.setText(self.settings.get("server_url", ""))
        form.addRow("Sync Server URL", self.server_url_input)
        self._settings_field_widgets.append(self.server_url_input)

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Authorization token")
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setText(self.settings.get("token", ""))
        form.addRow("Token", self.token_input)
        self._settings_field_widgets.append(self.token_input)

        # Automatic API synchronization toggle. When ON, the background
        # sync manager calls the API every 5 minutes. When OFF, all API sync
        # activity is disabled immediately, including manual "Sync Now".
        self.auto_sync_toggle = QCheckBox("Enable automatic API sync (every 5 minutes)")
        self.auto_sync_toggle.setChecked(bool(self.settings.get("auto_sync_enabled", True)))
        self.auto_sync_toggle.toggled.connect(self._auto_sync_toggled)
        form.addRow("Automatic API Sync", self.auto_sync_toggle)


        save_btn = QPushButton("Save Settings")
        save_btn.setObjectName("primaryButton")
        save_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        save_btn.clicked.connect(self._save)
        form.addRow(save_btn)
        self._settings_action_buttons = [save_btn]

        content_layout.addWidget(card)

        # --- Sync & Backup status card ---
        ops_card = _card()
        ops_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        ops_layout = QVBoxLayout(ops_card)
        ops_layout.setContentsMargins(20, 20, 20, 20)
        ops_layout.setSpacing(10)

        ops_title = QLabel("Sync & Backup")
        ops_title.setObjectName("navTitle")
        ops_layout.addWidget(ops_title)

        self.sync_info_label = QLabel(self._sync_info_text())
        ops_layout.addWidget(self.sync_info_label)

        self.backup_info_label = QLabel(self._backup_info_text())
        ops_layout.addWidget(self.backup_info_label)

        btn_row = QHBoxLayout()
        self.sync_now_btn = QPushButton("☁ Sync Now")
        self.sync_now_btn.clicked.connect(self._sync_now_clicked)
        self.sync_now_btn.setEnabled(self.auto_sync_toggle.isChecked())
        backup_now_btn = QPushButton("🗄 Backup Now")
        backup_now_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        backup_now_btn.clicked.connect(self._backup_now_clicked)
        self.sync_now_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self._settings_action_buttons.extend([self.sync_now_btn, backup_now_btn])
        btn_row.addWidget(self.sync_now_btn)
        btn_row.addWidget(backup_now_btn)
        ops_layout.addLayout(btn_row)

        content_layout.addWidget(ops_card)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self._update_settings_field_widths()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self._refresh_info_labels)
        self._refresh_timer.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_settings_field_widths()

    def _update_settings_field_widths(self) -> None:
        # Keep settings fields short and responsive: approximately 40% of the
        # available settings content width, with sensible bounds for usability.
        available_width = max(1, self.width() - 80)
        field_width = max(220, int(available_width * 0.40))
        for widget in self._settings_field_widgets:
            widget.setMaximumWidth(field_width)

        # Keep Settings action buttons compact and responsive (about 35% width).
        action_width = max(140, min(220, int(available_width * 0.35)))
        for button in getattr(self, "_settings_action_buttons", []):
            button.setMaximumWidth(action_width)

    def _sync_info_text(self) -> str:
        last = db.get_meta("last_sync_check") or "never"
        stats = db.get_truck_statistics()
        return f"Last sync check: {last}    |    Synced: {stats['synced']}    |    Unsynced: {stats['unsynced']}"

    def _backup_info_text(self) -> str:
        last = db.get_meta("last_backup_time") or "never"
        return f"Last backup: {last}"

    def _refresh_info_labels(self) -> None:
        self.sync_info_label.setText(self._sync_info_text())
        self.backup_info_label.setText(self._backup_info_text())

    def _auto_sync_toggled(self, enabled: bool) -> None:
        self.settings["auto_sync_enabled"] = bool(enabled)
        save_settings(self.settings)
        self.sync_now_btn.setEnabled(bool(enabled))
        self.on_auto_sync_changed(bool(enabled))

    def _sync_now_clicked(self) -> None:
        if not self.auto_sync_toggle.isChecked():
            return
        self.on_sync_now()
        QMessageBox.information(self, "Sync", "Sync started in the background — check the status bar for progress.")

    def _backup_now_clicked(self) -> None:
        self.on_backup_now()
        QMessageBox.information(self, "Backup", "Backup started in the background.")
        QTimer.singleShot(1500, self._refresh_info_labels)

    def _populate_ports(self) -> None:
        self.com_port_combo.clear()
        for device, description in list_available_ports():
            self.com_port_combo.addItem(f"{device} — {description}", device)
        configured = self.settings.get("com_port")
        if configured:
            idx = self.com_port_combo.findData(configured)
            if idx >= 0:
                self.com_port_combo.setCurrentIndex(idx)

    def _auto_detect_hardware(self) -> None:
        self.on_auto_detect()
        QMessageBox.information(
            self,
            "Auto-Detect Started",
            "The system is testing available COM ports and communication settings using actual weighbridge data. You can continue using the application; the detected configuration will be applied automatically when found."
        )

    def apply_detected_configuration(self, port: str, baud: int, parity: str, bytesize: int, stopbits: float) -> None:
        idx = self.com_port_combo.findData(port)
        if idx < 0:
            self.com_port_combo.addItem(f"{port} — detected weighbridge", port)
            idx = self.com_port_combo.findData(port)
        if idx >= 0:
            self.com_port_combo.setCurrentIndex(idx)
        self.baud_combo.setCurrentText(str(baud))
        self.parity_combo.setCurrentText(str(parity))
        self.bytesize_combo.setCurrentText(str(bytesize))
        self.stopbits_combo.setCurrentText(str(stopbits).rstrip("0").rstrip("."))


    def _save(self) -> None:
        self.settings["com_port"] = self.com_port_combo.currentData()
        self.settings["baud_rate"] = int(self.baud_combo.currentText())
        self.settings["parity"] = self.parity_combo.currentText()
        self.settings["bytesize"] = int(self.bytesize_combo.currentText())
        self.settings["stopbits"] = float(self.stopbits_combo.currentText())
        self.settings["theme"] = self.theme_combo.currentText()
        self.settings["server_url"] = self.server_url_input.text().strip()
        self.settings["token"] = self.token_input.text().strip()
        self.settings["company_id"] = self.factory_number_input.text().strip()
        # Keep legacy key synchronized for backward compatibility.
        self.settings["factory_number"] = self.settings["company_id"]
        self.settings["machine_number"] = self.machine_number_input.text().strip()
        self.settings["auto_sync_enabled"] = self.auto_sync_toggle.isChecked()
        save_settings(self.settings)
        self.on_save(self.settings)
        QMessageBox.information(self, "Saved", "Settings saved successfully! Auto-connection and sync target updated.")


# ==============================================================================
# MAIN WINDOW
# ==============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Weighbridge System")
        self.setWindowIcon(QIcon(APP_ICON_PATH))
        self.setMinimumSize(1000, 650)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        if available:
            width = max(self.minimumWidth(), available.width() // 2)
            height = max(self.minimumHeight(), available.height() // 2)
            self.resize(width, height)
            self.move(available.x() + (available.width() - width) // 2,
                      available.y() + (available.height() - height) // 2)
        else:
            self.resize(1000, 650)

        self.settings = load_settings()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- top navigation bar ---
        nav_bar = QFrame()
        nav_bar.setObjectName("navBar")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(24, 14, 24, 14)

        app_title = QLabel("WEIGHBRIDGE")
        app_title.setObjectName("navTitle")
        nav_layout.addWidget(app_title)
        nav_layout.addSpacing(40)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = {}
        for name in ["Dashboard", "Records", "Settings"]:
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, n=name: self._go_to(n))
            nav_layout.addWidget(btn)
            self.nav_group.addButton(btn)
            self.nav_buttons[name] = btn

        nav_layout.addStretch()
        root.addWidget(nav_bar)

        # --- stacked screens ---
        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self.dashboard_screen = DashboardScreen(
            self._toggle_weight_calculation, self._notify,
            on_sync_now=self._sync_now,
        )
        self.records_screen = RecordsScreen(self._sync_now)
        self.records_screen.set_sync_enabled(bool(self.settings.get("auto_sync_enabled", True)))
        self.settings_screen = SettingsScreen(
            self.settings,
            self._on_settings_saved,
            self._sync_now,
            self._backup_now,
            self._start_auto_detection,
            self._on_auto_sync_changed,
        )

        self.stack.addWidget(self.dashboard_screen)
        self.stack.addWidget(self.records_screen)
        self.stack.addWidget(self.settings_screen)

        self._go_to("Dashboard")
        self._apply_theme(self.settings.get("theme", "Light"))

        # --- status bar: weighbridge connection + sync/backup state ---
        self.status_bar = QStatusBar()
        self.status_bar.setContentsMargins(0, 0, 0, 10)
        self.status_bar.setMaximumHeight(30)
        self.setStatusBar(self.status_bar)
        self.weighbridge_status_label = QLabel("Weighbridge: starting…")
        self.sync_status_label = QLabel("Sync: idle")
        self.internet_status_label = QLabel("Internet: checking…")
        self.server_status_label = QLabel("Server: checking…")
        self.port_status_label = QLabel(f"Port: {self.settings.get('com_port') or 'Auto'}")
        self.backup_status_label = QLabel("Backup: idle")
        self.status_bar.addWidget(self.weighbridge_status_label, 2)
        self.status_bar.addPermanentWidget(self.port_status_label, 1)
        self.status_bar.addPermanentWidget(self.internet_status_label, 1)
        self.status_bar.addPermanentWidget(self.server_status_label, 1)
        self.status_bar.addPermanentWidget(self.sync_status_label, 1)
        self.status_bar.addPermanentWidget(self.backup_status_label, 1)

        # --- Weighbridge reader thread (auto-reconnect, never blocks GUI) ---
        self.weight_reader = WeightReader(
            self.settings.get("com_port"),
            self.settings.get("baud_rate", 9600),
            self.settings.get("parity", "None"),
            self.settings.get("bytesize", 8),
            self.settings.get("stopbits", 1),
        )
        self.weight_reader.weight_updated.connect(self._on_weight_updated)
        self.weight_reader.connection_changed.connect(self._on_weighbridge_status)
        self.weight_reader.configuration_detected.connect(self._on_configuration_detected)
        if not self.settings.get("com_port"):
            self.weight_reader.configure(
                None,
                self.settings.get("baud_rate", 9600),
                self.settings.get("parity", "None"),
                self.settings.get("bytesize", 8),
                self.settings.get("stopbits", 1),
                auto_detect=True,
            )
        self.weight_reader.start()

        # --- Background sync thread (300s interval, offline-safe) ---
        self.sync_manager = SyncManager(
            db_manager=db,
            get_server_url=lambda: self.settings.get("server_url", ""),
            get_token=lambda: self.settings.get("token", ""),
            get_company_id=lambda: self.settings.get("company_id") or self.settings.get("factory_number", ""),
            get_machine_number=lambda: self.settings.get("machine_number", ""),
            get_auto_sync_enabled=lambda: bool(self.settings.get("auto_sync_enabled", True)),
        )
        self.sync_manager.status_changed.connect(self._on_sync_status)
        self.sync_manager.connectivity_changed.connect(self._on_connectivity_changed)
        self.sync_manager.sync_finished.connect(lambda ok, fail: self._refresh_all_stats())
        self.sync_manager.start()

        # Lightweight connectivity probes only; these are TCP checks, not API calls.
        self._connectivity_timer = QTimer(self)
        self._connectivity_timer.setInterval(5000)
        self._connectivity_timer.timeout.connect(self._refresh_connectivity_status)
        self._connectivity_timer.start()
        self._refresh_connectivity_status()

        # --- Background daily backup thread ---
        self.backup_manager = BackupManager()
        self.backup_manager.backup_completed.connect(self._on_backup_completed)
        self.backup_manager.backup_failed.connect(self._on_backup_failed)
        self.backup_manager.start()

        # Trim old raw weight-log rows periodically so the DB stays fast.
        self._housekeeping_timer = QTimer(self)
        self._housekeeping_timer.setInterval(3600_000)  # hourly
        self._housekeeping_timer.timeout.connect(lambda: db.purge_old_weight_logs(keep_days=7))
        self._housekeeping_timer.start()

        # Persist the latest live weight exactly once every 5 minutes.
        self._latest_weight_for_log = 0.0
        self._weight_log_timer = QTimer(self)
        self._weight_log_timer.setInterval(300_000)
        self._weight_log_timer.timeout.connect(self._persist_weight_log)
        self._weight_log_timer.start()

        # Reset the single WeightLog.log file exactly at each local midnight.
        self._schedule_weight_log_midnight_reset()

    # ---------------------------------------------------------------- theme
    def _apply_theme(self, theme_name: str) -> None:
        self.setStyleSheet(LIGHT_STYLESHEET if theme_name == "Light" else DARK_STYLESHEET)

    def _go_to(self, name: str) -> None:
        index = {"Dashboard": 0, "Records": 1, "Settings": 2}[name]
        self.stack.setCurrentIndex(index)
        self.nav_buttons[name].setChecked(True)
        if name == "Records":
            self.records_screen.refresh()
        if name == "Dashboard":
            self.dashboard_screen.update_statistics_display()

    def _toggle_weight_calculation(self) -> None:
        if self.dashboard_screen.is_measuring:
            self.dashboard_screen.stop_measurement()
        else:
            self.dashboard_screen.start_measurement()

    def _refresh_all_stats(self) -> None:
        self.dashboard_screen.update_statistics_display()
        if self.stack.currentWidget() is self.records_screen:
            self.records_screen.refresh()

    # ------------------------------------------------------------- settings
    def _on_settings_saved(self, settings: dict) -> None:
        self.settings = settings
        self._apply_theme(settings.get("theme", "Light"))
        self.weight_reader.configure(
            settings.get("com_port"),
            settings.get("baud_rate", 9600),
            settings.get("parity", "None"),
            settings.get("bytesize", 8),
            settings.get("stopbits", 1),
            auto_detect=not bool(settings.get("com_port")),
        )
        if not self.weight_reader.isRunning():
            self.weight_reader.start()
        self.port_status_label.setText(f"Port: {settings.get('com_port') or 'Auto'}")
        self.sync_manager.set_auto_sync_enabled(
            bool(settings.get("auto_sync_enabled", True))
        )

    # ---------------------------------------------------------- weighbridge
    def _on_weight_updated(self, weight: float) -> None:
        self._latest_weight_for_log = float(weight)
        self.dashboard_screen.update_live_weight(weight)

    def _persist_weight_log(self) -> None:
        db.insert_weight_log(self._latest_weight_for_log)

    def _schedule_weight_log_midnight_reset(self) -> None:
        now = QDateTime.currentDateTime()
        tomorrow = now.addDays(1)
        midnight = QDateTime(tomorrow.date(), QTime(0, 0, 0))
        delay_ms = max(1000, now.msecsTo(midnight))
        QTimer.singleShot(delay_ms, self._reset_weight_log_at_midnight)

    def _reset_weight_log_at_midnight(self) -> None:
        db.reset_weight_log()
        self._schedule_weight_log_midnight_reset()

    def _on_weighbridge_status(self, level: str, message: str) -> None:
        self.dashboard_screen.update_status(level, message)
        color = STATUS_COLORS.get(level, "#64748b")
        self.weighbridge_status_label.setText(f"Weighbridge: {message}")
        self.weighbridge_status_label.setStyleSheet(f"color: {color};")
        db.insert_audit_log(f"WEIGHBRIDGE_{level.upper()}", message)

    def _start_auto_detection(self) -> None:
        # Reuse the same real-data probing engine as the background reader.
        self.weight_reader.configure(
            None,
            self.settings.get("baud_rate", 9600),
            self.settings.get("parity", "None"),
            self.settings.get("bytesize", 8),
            self.settings.get("stopbits", 1),
            auto_detect=True,
        )

    def _on_configuration_detected(self, port: str, baud: int, parity: str, bytesize: int, stopbits: float) -> None:
        self.settings.update({
            "com_port": port,
            "baud_rate": int(baud),
            "parity": parity,
            "bytesize": int(bytesize),
            "stopbits": float(stopbits),
        })
        save_settings(self.settings)
        if hasattr(self, "port_status_label"):
            self.port_status_label.setText(f"Port: {port}")
        self.settings_screen.apply_detected_configuration(port, baud, parity, bytesize, stopbits)

    def _refresh_connectivity_status(self) -> None:
        internet_ok = is_internet_available()
        server_url = (self.settings.get("server_url", "") or "").strip()
        server_ok = internet_ok and bool(server_url) and is_server_reachable(server_url)
        if internet_ok:
            self.internet_status_label.setText("Internet: Connected")
            self.internet_status_label.setStyleSheet(f"color: {STATUS_COLORS['ok']};")
        else:
            self.internet_status_label.setText("Internet: Disconnected")
            self.internet_status_label.setStyleSheet(f"color: {STATUS_COLORS['error']};")
        if not server_url:
            self.server_status_label.setText("Server: Not Configured")
            self.server_status_label.setStyleSheet("color: #64748b;")
        elif server_ok:
            self.server_status_label.setText("Server: Reachable")
            self.server_status_label.setStyleSheet(f"color: {STATUS_COLORS['ok']};")
        else:
            self.server_status_label.setText("Server: Unreachable")
            self.server_status_label.setStyleSheet(f"color: {STATUS_COLORS['error']};")

    # ------------------------------------------------------------------ sync
    def _on_auto_sync_changed(self, enabled: bool) -> None:
        self.settings["auto_sync_enabled"] = bool(enabled)
        self.sync_manager.set_auto_sync_enabled(bool(enabled))
        self.records_screen.set_sync_enabled(bool(enabled))

    def _sync_now(self) -> None:
        # The SyncManager also enforces this guard, so every manual sync
        # entry point is blocked immediately while the setting is OFF.
        if not bool(self.settings.get("auto_sync_enabled", True)):
            self._on_sync_status("warning", "API sync is disabled.")
            return
        self.sync_manager.sync_now()

    def _on_sync_status(self, level: str, message: str) -> None:
        color = STATUS_COLORS.get(level, "#64748b")
        self.sync_status_label.setText(f"Sync: {message}")
        self.sync_status_label.setStyleSheet(f"color: {color};")
        self._refresh_all_stats()

    def _on_connectivity_changed(self, internet_ok: bool, server_ok: bool) -> None:
        internet_text = "Internet: Connected" if internet_ok else "Internet: Disconnected"
        server_text = "Server: Reachable" if server_ok else "Server: Unreachable"
        self.internet_status_label.setText(internet_text)
        self.server_status_label.setText(server_text)
        self.internet_status_label.setStyleSheet(f"color: {STATUS_COLORS['ok'] if internet_ok else STATUS_COLORS['error']};")
        self.server_status_label.setStyleSheet(f"color: {STATUS_COLORS['ok'] if server_ok else STATUS_COLORS['error']};")

    # ---------------------------------------------------------------- backup
    def _backup_now(self) -> None:
        self.backup_manager.backup_now()

    def _on_backup_completed(self, path: str) -> None:
        self.backup_status_label.setText(f"Backup: OK ({db.now_str()})")
        self.backup_status_label.setStyleSheet(f"color: {STATUS_COLORS['ok']};")

    def _on_backup_failed(self, error: str) -> None:
        self.backup_status_label.setText(f"Backup: FAILED — {error}")
        self.backup_status_label.setStyleSheet(f"color: {STATUS_COLORS['error']};")
        self._notify("error", f"Database backup failed: {error}")

    # -------------------------------------------------------------- notify
    def _notify(self, level: str, message: str) -> None:
        """Generic alert surface used for abnormal-weight detection and
        anything else that needs the operator's attention right away."""
        color = STATUS_COLORS.get(level, "#64748b")
        self.status_bar.showMessage(message, 8000)
        if level == "error":
            self.weighbridge_status_label.setStyleSheet(f"color: {color};")

    def closeEvent(self, event) -> None:
        if hasattr(self, "_connectivity_timer"):
            self._connectivity_timer.stop()
        if hasattr(self, "_weight_log_timer"):
            self._weight_log_timer.stop()
        if self.weight_reader.isRunning():
            self.weight_reader.stop()
            self.weight_reader.wait(2000)
        if self.sync_manager.isRunning():
            self.sync_manager.stop()
            self.sync_manager.wait(2000)
        if self.backup_manager.isRunning():
            self.backup_manager.stop()
            self.backup_manager.wait(2000)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    # Sets the icon used in the taskbar and for any window/dialog that
    # doesn't set its own icon; MainWindow also sets it explicitly on itself.
    app.setWindowIcon(QIcon(APP_ICON_PATH))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()