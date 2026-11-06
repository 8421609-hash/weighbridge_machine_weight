#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
database.py
-----------
SQLite storage layer for Weighbridge Management System.

Features:
- SQLite local database
- Offline Entry / Exit stored as SEPARATE, independent transactions
- Sync / Unsync status tracked independently per Entry record and per
  Exit record
- Automatic schema migration (including a one-time split of any legacy
  combined truck_records table into entry_records / exit_records)
- Audit trail
- Weight logging
- Search/filter support (truck number, date/time range, entry/exit
  type, sync status)
- Statistics
- Sync helpers
- Database backup
- WAL mode + thread-safe writes

Data model
----------
Entry and Exit are two separate tables. A Truck Entry creates a new row
in `entry_records`. A Truck Exit NEVER updates that row — it always
creates a brand new row in `exit_records`. The two rows are linked directly by a foreign key:

    exit_records.entry_id == entry_records.id

This means the entry record's values are permanently immutable once
written, exit records are permanently immutable once written, and both
sides carry their own independent sync_status/sync_error/remote_id so
one can be synced while the other is still pending.
"""

import os
import sys
import time
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional, List, Dict, Any


# ============================================================
# APP PATHS
# ============================================================

APP_FOLDER_NAME = "Weighbridge"


def get_app_data_dir() -> str:
    if sys.platform == "win32":
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.path.join(
                os.path.expanduser("~"),
                "AppData",
                "Local"
            )
        )
    else:
        base = (
            os.environ.get("XDG_DATA_HOME")
            or os.path.join(
                os.path.expanduser("~"),
                ".local",
                "share"
            )
        )

    app_dir = os.path.join(base, APP_FOLDER_NAME)
    os.makedirs(app_dir, exist_ok=True)

    return app_dir


def get_app_data_path(filename: str) -> str:
    return os.path.join(get_app_data_dir(), filename)


DB_FILE = get_app_data_path("weighbridge.db")
BACKUP_DIR = get_app_data_path("backups")
WEIGHT_LOG_DIR = get_app_data_path("WeightLogs")
AUDIT_LOG_DIR = get_app_data_path("AuditLogs")


# ============================================================
# THREAD LOCK
# ============================================================

_write_lock = threading.RLock()


# ============================================================
# DATABASE CONNECTION
# ============================================================

def _connect() -> sqlite3.Connection:

    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")

    return conn


@contextmanager
def _cursor(write: bool = False):

    if write:

        with _write_lock:

            conn = _connect()

            try:
                cur = conn.cursor()
                yield cur
                conn.commit()

            except Exception:
                conn.rollback()
                raise

            finally:
                conn.close()

    else:

        conn = _connect()

        try:
            cur = conn.cursor()
            yield cur

        finally:
            conn.close()


# ============================================================
# TIME
# ============================================================

def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_database() -> None:

    """
    Initialize database.

    IMPORTANT:
    Migration is performed BEFORE indexes are created.
    """

    # --------------------------------------------------------
    # STEP 1 - CREATE BASE TABLES
    # --------------------------------------------------------

    with _cursor(write=True) as cur:

        cur.execute("""
            CREATE TABLE IF NOT EXISTS entry_records (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                truck_number TEXT NOT NULL,

                driver_name TEXT,

                driver_cnic TEXT,

                driver_phone TEXT,

                entry_weight REAL NOT NULL,

                entry_time TEXT NOT NULL,

                sync_status TEXT NOT NULL
                    DEFAULT 'Unsync',

                sync_error TEXT,

                remote_id TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS exit_records (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                entry_id INTEGER NOT NULL,

                truck_number TEXT NOT NULL,

                exit_weight REAL NOT NULL,

                exit_time TEXT NOT NULL,

                net_weight REAL,

                sync_status TEXT NOT NULL
                    DEFAULT 'Unsync',

                sync_error TEXT,

                remote_id TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL,

                FOREIGN KEY (entry_id)
                    REFERENCES entry_records(id)
                    ON UPDATE CASCADE
                    ON DELETE RESTRICT
            )
        """)

        # --------------------------------------------------------
        # SEPARATE EXIT GATE SUPPORT
        #
        # When this machine is used as a standalone exit gate, the
        # matching Entry record lives only on the entry-gate machine
        # (and, once synced, on the server) -- it is never present in
        # this machine's own entry_records table. These rows cannot
        # carry the entry_records FOREIGN KEY that exit_records uses,
        # so they get their own table. entry_id here stores the
        # server-side reference entry ID associated with a remote
        # Entry, not a local autoincrement id.
        # --------------------------------------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS remote_exit_records (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                entry_id TEXT NOT NULL,

                truck_number TEXT NOT NULL,

                driver_name TEXT,

                driver_phone TEXT,

                exit_weight REAL NOT NULL,

                exit_time TEXT NOT NULL,

                net_weight REAL,

                sync_status TEXT NOT NULL
                    DEFAULT 'Unsync',

                sync_error TEXT,

                remote_id TEXT,

                created_at TEXT NOT NULL,

                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (

                key TEXT PRIMARY KEY,

                value TEXT
            )
        """)


    # --------------------------------------------------------
    # STEP 2 - MIGRATE OLD DATABASE
    # --------------------------------------------------------

    _migrate_legacy_schema()
    _migrate_legacy_combined_table()

    # Weight history and audit history are file-only. Remove legacy DB tables.
    with _cursor(write=True) as cur:
        cur.execute("DROP TABLE IF EXISTS weight_log")
        cur.execute("DROP TABLE IF EXISTS audit_log")


    # --------------------------------------------------------
    # STEP 3 - CREATE INDEXES
    # --------------------------------------------------------

    # IMPORTANT:
    # This comes AFTER migration.

    with _cursor(write=True) as cur:

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_entry_sync_status
            ON entry_records(sync_status)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_entry_truck_number
            ON entry_records(truck_number)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_entry_entry_time
            ON entry_records(entry_time)
        """)


        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_exit_sync_status
            ON exit_records(sync_status)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_exit_truck_number
            ON exit_records(truck_number)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_exit_exit_time
            ON exit_records(exit_time)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_exit_entry_id
            ON exit_records(entry_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_remote_exit_sync_status
            ON remote_exit_records(sync_status)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_remote_exit_entry_id
            ON remote_exit_records(entry_id)
        """)


# ============================================================
# DATABASE MIGRATION
# ============================================================

def _table_exists(cur, name: str) -> bool:

    cur.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (name,)
    )

    return cur.fetchone() is not None


def _migrate_legacy_schema() -> None:
    """
    Migrate older split Entry/Exit schemas from reference_id to entry_id.

    Existing rows keep their original primary keys. The old reference_id
    columns are removed by rebuilding the tables so the final schema can
    enforce a real SQLite foreign-key constraint.
    """
    with _cursor(write=True) as cur:
        cur.execute("PRAGMA table_info(entry_records)")
        entry_cols = {row["name"] for row in cur.fetchall()}

        cur.execute("PRAGMA table_info(exit_records)")
        exit_cols = {row["name"] for row in cur.fetchall()}

        if "reference_id" not in entry_cols and "reference_id" not in exit_cols:
            return

        if "reference_id" not in entry_cols and "reference_id" in exit_cols:
            raise RuntimeError(
                "Cannot migrate exit_records.reference_id because "
                "entry_records.reference_id is missing."
            )

        if "sync_error" not in exit_cols:
            cur.execute("ALTER TABLE exit_records ADD COLUMN sync_error TEXT")
            exit_cols.add("sync_error")

        cur.execute("DROP INDEX IF EXISTS idx_entry_reference_id")
        cur.execute("DROP INDEX IF EXISTS idx_exit_reference_id")

        cur.execute("""
            CREATE TABLE entry_records_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_number TEXT NOT NULL,
                driver_name TEXT,
                driver_cnic TEXT,
                driver_phone TEXT,
                entry_weight REAL NOT NULL,
                entry_time TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'Unsync',
                sync_error TEXT,
                remote_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            INSERT INTO entry_records_new
            (id, truck_number, driver_name, driver_cnic, driver_phone,
             entry_weight, entry_time, sync_status, sync_error, remote_id,
             created_at, updated_at)
            SELECT id, truck_number, driver_name, driver_cnic, driver_phone,
                   entry_weight, entry_time,
                   COALESCE(sync_status, 'Unsync'),
                   sync_error, remote_id,
                   COALESCE(created_at, entry_time),
                   COALESCE(updated_at, entry_time)
            FROM entry_records
            ORDER BY id
        """)

        cur.execute("""
            CREATE TABLE exit_records_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                truck_number TEXT NOT NULL,
                exit_weight REAL NOT NULL,
                exit_time TEXT NOT NULL,
                net_weight REAL,
                sync_status TEXT NOT NULL DEFAULT 'Unsync',
                sync_error TEXT,
                remote_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (entry_id)
                    REFERENCES entry_records_new(id)
                    ON UPDATE CASCADE
                    ON DELETE RESTRICT
            )
        """)

        cur.execute("""
            INSERT INTO exit_records_new
            (id, entry_id, truck_number, exit_weight, exit_time, net_weight,
             sync_status, sync_error, remote_id, created_at, updated_at)
            SELECT
                x.id,
                e.id,
                x.truck_number,
                x.exit_weight,
                x.exit_time,
                x.net_weight,
                COALESCE(x.sync_status, 'Unsync'),
                x.sync_error,
                x.remote_id,
                COALESCE(x.created_at, x.exit_time),
                COALESCE(x.updated_at, x.exit_time)
            FROM exit_records x
            JOIN entry_records e
              ON CAST(e.reference_id AS TEXT) = CAST(x.reference_id AS TEXT)
            ORDER BY x.id
        """)

        cur.execute("SELECT COUNT(*) FROM exit_records")
        old_exit_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM exit_records_new")
        new_exit_count = cur.fetchone()[0]
        if old_exit_count != new_exit_count:
            raise RuntimeError(
                "Migration aborted: some existing Exit records could not be "
                "linked to an Entry record."
            )

        cur.execute("DROP TABLE exit_records")
        cur.execute("DROP TABLE entry_records")
        cur.execute("ALTER TABLE entry_records_new RENAME TO entry_records")
        cur.execute("ALTER TABLE exit_records_new RENAME TO exit_records")


def _migrate_legacy_combined_table() -> None:
    """
    ONE-TIME MIGRATION from the old combined truck_records table.

    Each legacy row becomes one immutable Entry row and, when exit data
    exists, one immutable Exit row linked by exit_records.entry_id.
    """
    with _cursor(write=True) as cur:
        if not _table_exists(cur, "truck_records"):
            return

        if get_meta_sync("legacy_truck_records_migrated") == "1":
            return

        cur.execute("SELECT * FROM truck_records ORDER BY id ASC")
        legacy_rows = cur.fetchall()

        for row in legacy_rows:
            ts = (
                row["updated_at"]
                if "updated_at" in row.keys() and row["updated_at"]
                else now_str()
            )

            cur.execute(
                """
                INSERT INTO entry_records
                (
                    truck_number,
                    driver_name,
                    driver_cnic,
                    driver_phone,
                    entry_weight,
                    entry_time,
                    sync_status,
                    sync_error,
                    remote_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["truck_number"],
                    row["driver_name"],
                    row["driver_cnic"],
                    row["driver_phone"],
                    row["entry_weight"],
                    row["entry_time"],
                    row["sync_status"]
                    if "sync_status" in row.keys() and row["sync_status"]
                    else "Unsync",
                    row["sync_error"] if "sync_error" in row.keys() else None,
                    row["remote_id"] if "remote_id" in row.keys() else None,
                    row["created_at"]
                    if "created_at" in row.keys() and row["created_at"]
                    else row["entry_time"],
                    ts,
                ),
            )

            new_entry_id = cur.lastrowid

            has_exit = (
                row["exit_time"] is not None
                and row["exit_weight"] is not None
            )

            if has_exit:
                cur.execute(
                    """
                    INSERT INTO exit_records
                    (
                        entry_id,
                        truck_number,
                        exit_weight,
                        exit_time,
                        net_weight,
                        sync_status,
                        sync_error,
                        remote_id,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_entry_id,
                        row["truck_number"],
                        row["exit_weight"],
                        row["exit_time"],
                        row["net_weight"],
                        row["sync_status"]
                        if "sync_status" in row.keys() and row["sync_status"]
                        else "Unsync",
                        row["sync_error"] if "sync_error" in row.keys() else None,
                        None,
                        ts,
                        ts,
                    ),
                )

        cur.execute(
            "ALTER TABLE truck_records RENAME TO truck_records_legacy_migrated"
        )

    set_meta("legacy_truck_records_migrated", "1")


# ============================================================
# AUDIT LOG
# ============================================================

def _append_daily_log(directory: str, prefix: str, line: str) -> None:
    """Append one human-readable event to today's daily log file."""
    os.makedirs(directory, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(directory, f"{prefix}_{day}.log")
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")


def insert_audit_log(action: str, details: str = "", record_id: Optional[int] = None) -> None:
    """Audit Trail is file-only; no SQLite write is performed."""
    path = get_app_data_path(os.path.join("AuditLogs", time.strftime("%Y-%m-%d.log")))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        suffix = f" | record_id={record_id}" if record_id is not None else ""
        f.write(f"[{now_str()}] [{action}] {details}{suffix}\n")


def fetch_audit_log(
    limit: int = 500,
    action_filter: str = ""
) -> List[dict]:
    """Read recent audit events directly from the daily audit files."""
    if not os.path.isdir(AUDIT_LOG_DIR):
        return []
    files = sorted((f for f in os.listdir(AUDIT_LOG_DIR) if f.endswith(".log")), reverse=True)
    entries = []
    needle = action_filter.strip().lower()
    for filename in files:
        path = os.path.join(AUDIT_LOG_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.rstrip("\n")
            if not line:
                continue
            timestamp, sep, rest = line.partition("] ")
            timestamp = timestamp.lstrip("[")
            if not sep:
                continue
            action, sep, details = rest.partition("] ")
            if not sep:
                continue
            record_id = None
            details = details.strip()
            if " | record_id=" in details:
                details, _, rid = details.rpartition(" | record_id=")
                try:
                    record_id = int(rid)
                except ValueError:
                    record_id = None
            action = action.strip("[] ")
            if needle and needle not in action.lower():
                continue
            entries.append({"timestamp": timestamp, "action": action, "details": details, "record_id": record_id})
            if len(entries) >= limit:
                return entries
    return entries


# ============================================================
# META
# ============================================================

def get_meta(key: str) -> Optional[str]:

    with _cursor() as cur:

        cur.execute(
            """
            SELECT value
            FROM meta
            WHERE key = ?
            """,
            (key,)
        )

        row = cur.fetchone()

        return row["value"] if row else None


def get_meta_sync(key: str) -> Optional[str]:

    """
    Same as get_meta but usable from inside a write cursor's own
    transaction context during migration, before init has fully
    finished setting up meta reads elsewhere.
    """

    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT value FROM meta WHERE key = ?",
                (key,)
            )
            row = cur.fetchone()
            return row["value"] if row else None
    except sqlite3.OperationalError:
        return None


def set_meta(key: str, value: str) -> None:

    with _cursor(write=True) as cur:

        cur.execute(
            """
            INSERT INTO meta
            (
                key,
                value
            )

            VALUES (?, ?)

            ON CONFLICT(key)
            DO UPDATE SET
                value = excluded.value
            """,
            (
                key,
                value
            )
        )


# ============================================================
# DUPLICATE TRUCK CHECK
# ============================================================

def check_active_duplicate(
    truck_number: str
) -> Optional[str]:

    """
    A truck is "active" (already inside) if it has an Entry record
    that has no linked Exit record yet.
    """

    with _cursor() as cur:

        cur.execute(
            """
            SELECT e.truck_number

            FROM entry_records e

            LEFT JOIN exit_records x
                ON x.entry_id = e.id

            WHERE x.id IS NULL
                AND e.truck_number = ?

            LIMIT 1
            """,
            (truck_number,)
        )

        row = cur.fetchone()

        if row:

            return (
                f"Truck '{truck_number}' is already "
                f"inside (exit not yet recorded)."
            )

        return None


# ============================================================
# CREATE ENTRY  (always a brand-new, independent transaction)
# ============================================================

def create_truck_entry(
    truck_number: str,
    driver_name: str,
    driver_cnic: str,
    driver_phone: str,
    entry_weight: float
) -> int:
    """Create an independent Entry row and return its primary key."""
    duplicate = check_active_duplicate(truck_number)
    if duplicate:
        raise ValueError(duplicate)

    ts = now_str()
    with _cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO entry_records
            (truck_number, driver_name, driver_cnic, driver_phone,
             entry_weight, entry_time, sync_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'Unsync', ?, ?)
            """,
            (truck_number, driver_name or "N/A", driver_cnic or "N/A",
             driver_phone or "N/A", float(entry_weight), ts, ts, ts)
        )
        record_id = cur.lastrowid

    insert_audit_log(
        "ENTRY",
        f"Truck {truck_number} entered at {entry_weight:.2f} kg. (Entry #{record_id})",
        record_id
    )
    return record_id


# ============================================================
# CREATE EXIT  (always a brand-new, independent transaction —
# the original Entry record is NEVER updated)
# ============================================================

def complete_truck_exit(
    entry_id: int,
    truck_number: str,
    exit_weight: float,
    net_weight: float
) -> int:
    """Create an independent Exit row linked to entry_records.id."""
    entry = get_entry_record(entry_id)
    if entry is None:
        raise ValueError(f"Entry ID '{entry_id}' does not exist.")
    already_exited = get_exit_record_for_entry(entry_id)
    if already_exited is not None:
        raise ValueError(f"Truck '{entry['truck_number']}' already has an Exit record.")

    ts = now_str()
    with _cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO exit_records
            (entry_id, truck_number, exit_weight, exit_time, net_weight,
             sync_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Unsync', ?, ?)
            """,
            (int(entry_id), truck_number, float(exit_weight), ts, float(net_weight), ts, ts)
        )
        exit_id = cur.lastrowid

    insert_audit_log(
        "EXIT",
        f"Truck {truck_number} exited - exit {exit_weight:.2f} kg, "
        f"net {net_weight:.2f} kg. (Exit #{exit_id}, Entry #{entry_id})",
        exit_id
    )
    return exit_id


# ============================================================
# SEPARATE EXIT GATE -- CREATE EXIT AGAINST A REMOTE ENTRY
#
# Used when this machine has no local entry_records row for the
# truck (the truck entered on a different gate machine). The
# matching Entry lives on the server; this only records the Exit
# side locally and queues it for the normal bulk sync, which
# reports reference_entry_id + exit_weight so the server can
# compute the net weight itself.
# ============================================================

def get_remote_exit_record_for_reference(
    reference_entry_id: str
) -> Optional[sqlite3.Row]:

    with _cursor() as cur:
        cur.execute(
            """
            SELECT * FROM remote_exit_records
            WHERE entry_id = ?
            LIMIT 1
            """,
            (str(reference_entry_id),)
        )
        return cur.fetchone()


def fetch_remote_exit_reference_ids() -> set:
    """Reference entry IDs that already have a local Exit row here
    (synced or not), so local Exit selection can avoid duplicates."""

    with _cursor() as cur:
        cur.execute("SELECT entry_id FROM remote_exit_records")
        return {str(row["entry_id"]) for row in cur.fetchall()}


def create_remote_exit_entry(
    reference_entry_id: str,
    truck_number: str,
    driver_name: Optional[str],
    driver_phone: Optional[str],
    exit_weight: float,
    net_weight: Optional[float] = None
) -> int:
    """Create an independent Exit row for a truck that entered on a
    different (remote) gate machine. Returns the new local exit id."""

    if not str(reference_entry_id).strip():
        raise ValueError("reference_entry_id is required.")

    already = get_remote_exit_record_for_reference(reference_entry_id)
    if already is not None:
        raise ValueError(
            f"Truck '{truck_number}' already has an Exit recorded here "
            f"for entry #{reference_entry_id}."
        )

    ts = now_str()
    with _cursor(write=True) as cur:
        cur.execute(
            """
            INSERT INTO remote_exit_records
            (entry_id, truck_number, driver_name, driver_phone,
             exit_weight, exit_time, net_weight,
             sync_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Unsync', ?, ?)
            """,
            (
                str(reference_entry_id), truck_number,
                driver_name or "N/A", driver_phone or "N/A",
                float(exit_weight), ts,
                float(net_weight) if net_weight is not None else None,
                ts, ts
            )
        )
        exit_id = cur.lastrowid

    insert_audit_log(
        "EXIT_REMOTE",
        f"Truck {truck_number} exited (separate exit gate) - "
        f"exit {exit_weight:.2f} kg. Queued for sync to reference "
        f"entry #{reference_entry_id}. (Remote Exit #{exit_id})",
        exit_id
    )
    return exit_id


# ============================================================
# FETCH SINGLE RECORDS
# ============================================================

# ============================================================

def get_entry_record(entry_id: int) -> Optional[sqlite3.Row]:

    with _cursor() as cur:

        cur.execute(
            "SELECT * FROM entry_records WHERE id = ? LIMIT 1",
            (entry_id,)
        )

        return cur.fetchone()


def get_entry_by_id(entry_id: int) -> Optional[sqlite3.Row]:
    return get_entry_record(entry_id)


def get_exit_record_for_entry(
    entry_id: int
) -> Optional[sqlite3.Row]:
    with _cursor() as cur:
        cur.execute(
            """
            SELECT * FROM exit_records
            WHERE entry_id = ?
            LIMIT 1
            """,
            (entry_id,)
        )
        return cur.fetchone()


def get_truck_record(
    record_id: int,
    record_type: str = "Entry"
) -> Optional[sqlite3.Row]:

    """
    Backward/forward-compatible single-record lookup.
    record_type: 'Entry' or 'Exit'
    """

    table = "entry_records" if record_type == "Entry" else "exit_records"

    with _cursor() as cur:

        cur.execute(
            f"SELECT * FROM {table} WHERE id = ? LIMIT 1",
            (record_id,)
        )

        return cur.fetchone()


# ============================================================
# FETCH PENDING ENTRIES (Entries with no linked Exit yet)
# ============================================================

def fetch_pending_entries() -> List[sqlite3.Row]:

    with _cursor() as cur:

        cur.execute(
            """
            SELECT
                e.id,
                e.id AS entry_id,
                e.truck_number,
                e.driver_name,
                e.entry_weight

            FROM entry_records e

            LEFT JOIN exit_records x
                ON x.entry_id = e.id

            WHERE x.id IS NULL

            ORDER BY e.id DESC
            """
        )

        return cur.fetchall()


# ============================================================
# COMBINED VIEW SQL
# (Entry and Exit each surfaced as their own independent row,
#  tagged by record_type, joined only by entry_id for display)
# ============================================================

_COMBINED_VIEW_SQL = """
    SELECT
        id,
        id AS entry_id,
        'Entry' AS record_type,
        truck_number,
        driver_name,
        driver_cnic,
        driver_phone,
        entry_weight,
        entry_time,
        NULL AS exit_weight,
        NULL AS exit_time,
        NULL AS net_weight,
        sync_status,
        sync_error,
        remote_id,
        created_at,
        updated_at,
        entry_time AS record_time
    FROM entry_records

    UNION ALL

    SELECT
        id,
        entry_id,
        'Exit' AS record_type,
        truck_number,
        NULL AS driver_name,
        NULL AS driver_cnic,
        NULL AS driver_phone,
        NULL AS entry_weight,
        NULL AS entry_time,
        exit_weight,
        exit_time,
        net_weight,
        sync_status,
        sync_error,
        remote_id,
        created_at,
        updated_at,
        exit_time AS record_time
    FROM exit_records
"""


# ============================================================
# FETCH ALL RECORDS (search across Entry + Exit)
# ============================================================

def fetch_all_truck_records(
    search_query: str = "",
    limit: int = 500
) -> List[sqlite3.Row]:

    with _cursor() as cur:

        if search_query.strip():

            q = f"%{search_query.strip()}%"

            cur.execute(
                f"""
                SELECT * FROM ( {_COMBINED_VIEW_SQL} )

                WHERE
                    truck_number LIKE ?
                    OR driver_name LIKE ?
                    OR driver_cnic LIKE ?
                    OR driver_phone LIKE ?

                ORDER BY record_time DESC, id DESC

                LIMIT ?
                """,
                (q, q, q, q, limit)
            )

        else:

            cur.execute(
                f"""
                SELECT * FROM ( {_COMBINED_VIEW_SQL} )

                ORDER BY record_time DESC, id DESC

                LIMIT ?
                """,
                (limit,)
            )

        return cur.fetchall()


# ============================================================
# FILTER RECORDS (Record Section)
# ============================================================

def fetch_filtered_records(
    date_from: str = "",
    date_to: str = "",
    truck_number: str = "",
    sync_status: str = "",
    record_type: str = "",
    limit: int = 500
) -> List[sqlite3.Row]:

    """
    Advanced record filtering across Entry + Exit, shown as independent
    rows (each still carries entry_id so related Entry/Exit history
    can be traced).

    date_from / date_to:
        YYYY-MM-DD or YYYY-MM-DD HH:MM:SS — matched against each row's
        own event time (entry_time for Entry rows, exit_time for Exit
        rows).

    sync_status:
        'Sync' / 'Unsync' / '' (all)

    record_type:
        'Entry' / 'Exit' / '' (both)
    """

    conditions = []

    params = []


    if date_from.strip():

        conditions.append(
            "record_time >= ?"
        )

        value = date_from.strip()

        params.append(
            value if " " in value else f"{value} 00:00:00"
        )


    if date_to.strip():

        conditions.append(
            "record_time <= ?"
        )

        value = date_to.strip()

        params.append(
            value if " " in value else f"{value} 23:59:59"
        )


    if truck_number.strip():

        conditions.append(
            "truck_number LIKE ?"
        )

        params.append(
            f"%{truck_number.strip()}%"
        )


    if sync_status.strip():

        conditions.append(
            "sync_status = ?"
        )

        params.append(
            sync_status.strip()
        )


    if record_type.strip():

        conditions.append(
            "record_type = ?"
        )

        params.append(
            record_type.strip()
        )


    query = f"SELECT * FROM ( {_COMBINED_VIEW_SQL} )"


    if conditions:

        query += (
            " WHERE "
            + " AND ".join(conditions)
        )


    query += """
        ORDER BY record_time DESC, id DESC
        LIMIT ?
    """


    params.append(limit)


    with _cursor() as cur:

        cur.execute(
            query,
            params
        )

        return cur.fetchall()


# ============================================================
# STATISTICS
# ============================================================

def get_truck_statistics() -> Dict[str, int]:

    with _cursor() as cur:

        cur.execute(
            """
            SELECT COUNT(*)
            FROM entry_records e
            LEFT JOIN exit_records x
                ON x.entry_id = e.id
            WHERE x.id IS NULL
            """
        )

        entered = cur.fetchone()[0]


        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM exit_records)
                +
                (SELECT COUNT(*) FROM remote_exit_records)
            """
        )

        exited = cur.fetchone()[0]


        cur.execute(
            "SELECT COUNT(*) FROM entry_records"
        )

        total = cur.fetchone()[0]


        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM entry_records WHERE sync_status = 'Unsync')
                +
                (SELECT COUNT(*) FROM exit_records WHERE sync_status = 'Unsync')
                +
                (SELECT COUNT(*) FROM remote_exit_records WHERE sync_status = 'Unsync')
            """
        )

        unsynced = cur.fetchone()[0]


        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM entry_records WHERE sync_status = 'Sync')
                +
                (SELECT COUNT(*) FROM exit_records WHERE sync_status = 'Sync')
                +
                (SELECT COUNT(*) FROM remote_exit_records WHERE sync_status = 'Sync')
            """
        )

        synced = cur.fetchone()[0]


        return {
            "entered": entered,
            "exited": exited,
            "total": total,
            "unsynced": unsynced,
            "synced": synced
        }


# ============================================================
# UNSYNCED RECORDS (across both tables)
# ============================================================

def fetch_unsynced_records(
    limit: Optional[int] = None
) -> List[Dict[str, Any]]:

    """
    Returns Unsync rows from BOTH entry_records and exit_records,
    each as a plain dict with a 'record_type' key ('Entry' / 'Exit')
    so the caller (SyncManager) knows which table an id belongs to.
    """

    with _cursor() as cur:

        # Every operator-created Entry is eligible for automatic sync as soon
        # as it is saved.  It must NOT wait for Exit.  If the server/network
        # is unavailable it stays Unsync locally and is retried later.
        cur.execute(
            """
            SELECT e.* FROM entry_records e
            WHERE e.sync_status = 'Unsync'
            ORDER BY e.id ASC
            """
            if limit is None else """
            SELECT e.* FROM entry_records e
            WHERE e.sync_status = 'Unsync'
            ORDER BY e.id ASC
            LIMIT ?
            """,
            () if limit is None else (limit,)
        )

        entries = [dict(row, record_type="Entry") for row in cur.fetchall()]

        if limit is None:
            cur.execute(
                """
                SELECT x.*, e.driver_name, e.driver_phone
                FROM exit_records x
                LEFT JOIN entry_records e ON e.id = x.entry_id
                WHERE x.sync_status = 'Unsync'
                ORDER BY x.id ASC
                """
            )
        else:
            cur.execute(
                """
                SELECT x.*, e.driver_name, e.driver_phone
                FROM exit_records x
                LEFT JOIN entry_records e ON e.id = x.entry_id
                WHERE x.sync_status = 'Unsync'
                ORDER BY x.id ASC
                LIMIT ?
                """,
                (limit,)
            )

        exits = [dict(row, record_type="Exit") for row in cur.fetchall()]


        combined = entries + exits
        combined.sort(key=lambda r: (r["created_at"], r["id"]))

        return combined if limit is None else combined[:limit]


# ============================================================
# MARK RECORD SYNCED / FAILED (record_type-aware)
# ============================================================

def _sync_table(record_type: str) -> str:

    # SyncManager normalizes record types to uppercase (ENTRY / EXIT /
    # EXITREMOTE). Keep this helper tolerant of either existing
    # database/UI spelling.
    normalized = str(record_type).strip().upper()
    if normalized == "EXITREMOTE":
        return "remote_exit_records"
    if normalized == "EXIT":
        return "exit_records"

    return "entry_records"


def mark_record_synced(
    record_id: int,
    record_type: str = "Entry",
    remote_id: Optional[str] = None
) -> None:

    table = _sync_table(record_type)

    with _cursor(write=True) as cur:

        cur.execute(
            f"""
            UPDATE {table}

            SET
                sync_status = 'Sync',
                sync_error = NULL,
                remote_id =
                    COALESCE(?, remote_id),
                updated_at = ?

            WHERE id = ?
            """,
            (
                remote_id,
                now_str(),
                record_id
            )
        )


    insert_audit_log(
        "SYNC",
        (
            f"{record_type} record #{record_id} "
            f"successfully synchronized."
        ),
        record_id
    )


def mark_record_sync_failed(
    record_id: int,
    error: str,
    record_type: str = "Entry"
) -> None:

    table = _sync_table(record_type)

    with _cursor(write=True) as cur:

        cur.execute(
            f"""
            UPDATE {table}

            SET
                sync_status = 'Unsync',
                sync_error = ?,
                updated_at = ?

            WHERE id = ?
            """,
            (
                str(error)[:500],
                now_str(),
                record_id
            )
        )


    insert_audit_log(
        "SYNC_FAILED",
        (
            f"{record_type} record #{record_id} "
            f"sync failed: {str(error)[:400]}"
        ),
        record_id
    )


# ============================================================
# WEIGHT LOG
# ============================================================

WEIGHT_LOG_FILENAME = "WeightLog.log"
WEIGHT_LOG_DATE_MARKER = "WeightLog.current_date"


def _weight_log_path() -> str:
    os.makedirs(WEIGHT_LOG_DIR, exist_ok=True)
    return os.path.join(WEIGHT_LOG_DIR, WEIGHT_LOG_FILENAME)


def reset_weight_log() -> None:
    """Reset the single continuous Weight Log file for a new day."""
    path = _weight_log_path()
    with _write_lock:
        with open(path, "w", encoding="utf-8"):
            pass
    # Keep a tiny marker outside the log so an app restart can detect
    # a missed midnight rollover.
    try:
        with open(os.path.join(WEIGHT_LOG_DIR, WEIGHT_LOG_DATE_MARKER), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d"))
    except OSError:
        pass


def _ensure_current_weight_log() -> str:
    """Return the one WeightLog.log file, resetting it when the day changes."""
    os.makedirs(WEIGHT_LOG_DIR, exist_ok=True)
    path = _weight_log_path()
    today = time.strftime("%Y-%m-%d")
    marker_path = os.path.join(WEIGHT_LOG_DIR, WEIGHT_LOG_DATE_MARKER)

    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            marker = f.read().strip()
    except OSError:
        marker = ""

    if marker != today:
        reset_weight_log()

    return path


def insert_weight_log(weight: float) -> None:
    """Append the latest weight to the single daily WeightLog.log file."""
    path = _ensure_current_weight_log()
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {float(weight):.2f} kg\n")


def fetch_weight_logs(limit: int = 200) -> List[dict]:
    """Read entries from today's single continuous WeightLog.log file."""
    path = _ensure_current_weight_log()
    logs = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    for index, line in enumerate(reversed(lines), 1):
        line = line.strip()
        if not line:
            continue
        timestamp, sep, weight_text = line.partition("] ")
        if not sep:
            continue
        timestamp = timestamp.lstrip("[")
        weight_text = weight_text.strip()
        if weight_text.lower().endswith(" kg"):
            weight_text = weight_text[:-3].strip()
        try:
            weight = float(weight_text)
        except ValueError:
            continue
        logs.append({
            "id": index,
            "weight": weight,
            "logged_time": timestamp,
        })
        if len(logs) >= limit:
            break
    return logs


# ============================================================
# PURGE OLD WEIGHT LOG FILES
# ============================================================

def purge_old_weight_logs(keep_days: int = 7) -> int:
    """Compatibility hook. The active WeightLog is intentionally a single file."""
    return 0


# ============================================================
# DATABASE BACKUP
# ============================================================

def backup_database(
    dest_path: str
) -> None:

    """
    Creates a consistent SQLite backup
    using SQLite's online backup API.
    """

    directory = os.path.dirname(
        dest_path
    )

    if directory:

        os.makedirs(
            directory,
            exist_ok=True
        )


    src_conn = sqlite3.connect(
        DB_FILE
    )

    dst_conn = sqlite3.connect(
        dest_path
    )


    try:

        with dst_conn:

            src_conn.backup(
                dst_conn
            )

    finally:

        src_conn.close()
        dst_conn.close()


# ============================================================
# TEST / DIRECT EXECUTION
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("Weighbridge SQLite Database")
    print("=" * 60)

    print(f"Database: {DB_FILE}")

    try:

        init_database()

        print("Database initialized successfully.")

        stats = get_truck_statistics()

        print(
            f"Total Entries : {stats['total']}"
        )

        print(
            f"Pending Trucks: {stats['entered']}"
        )

        print(
            f"Completed Exits: {stats['exited']}"
        )

        print(
            f"Synced        : {stats['synced']}"
        )

        print(
            f"Unsynced      : {stats['unsynced']}"
        )

        print("=" * 60)

    except Exception as e:

        print(
            "DATABASE ERROR:"
        )

        print(e)

        raise