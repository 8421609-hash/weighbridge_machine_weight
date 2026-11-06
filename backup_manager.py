#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 backup_manager.py -- Daily automatic SQLite backup
==============================================================================

Runs on its own QThread. Once a day (checked hourly, so the app doesn't
need to be left running at a specific time), it makes a consistent copy of
the live database into a timestamped file under the backups/ folder, using
SQLite's online backup API -- this works safely even while the app is
actively reading/writing the database, no downtime or locking required.

Old backups beyond MAX_BACKUPS_KEPT are pruned automatically so the
backups folder doesn't grow without bound.
"""

import os
import time
import threading

from PySide6.QtCore import QThread, Signal

import database as db

BACKUP_CHECK_INTERVAL_SECS = 3600   # check hourly whether today's backup has run
MAX_BACKUPS_KEPT = 30


class BackupManager(QThread):
    backup_completed = Signal(str)   # path of the new backup file
    backup_failed = Signal(str)      # error message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._wake = threading.Event()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def backup_now(self) -> None:
        """Callable from the UI thread (e.g. a 'Backup Now' button)."""
        self._do_backup()

    def run(self) -> None:
        self._running = True
        # Check immediately on startup, so a backup still happens even if
        # the app is only ever opened briefly once a day.
        self._maybe_backup()
        while self._running:
            self._wake.wait(BACKUP_CHECK_INTERVAL_SECS)
            self._wake.clear()
            if self._running:
                self._maybe_backup()

    def _maybe_backup(self) -> None:
        last = db.get_meta("last_backup_time")
        today = time.strftime("%Y-%m-%d")
        if last and last.startswith(today):
            return  # already backed up today
        self._do_backup()

    def _do_backup(self) -> None:
        try:
            os.makedirs(db.BACKUP_DIR, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            dest = os.path.join(db.BACKUP_DIR, f"weighbridge_{stamp}.db")
            db.backup_database(dest)
            db.set_meta("last_backup_time", db.now_str())
            db.insert_audit_log("BACKUP", f"Database backed up to {dest}")
            self._prune_old_backups()
            self.backup_completed.emit(dest)
        except Exception as exc:
            db.insert_audit_log("BACKUP_FAILED", str(exc))
            self.backup_failed.emit(str(exc))

    def _prune_old_backups(self) -> None:
        try:
            files = sorted(
                (f for f in os.listdir(db.BACKUP_DIR) if f.startswith("weighbridge_") and f.endswith(".db")),
                reverse=True,
            )
            for old in files[MAX_BACKUPS_KEPT:]:
                try:
                    os.remove(os.path.join(db.BACKUP_DIR, old))
                except OSError:
                    pass
        except FileNotFoundError:
            pass
