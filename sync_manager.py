import json
import socket
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import Callable
from PySide6.QtCore import QThread, Signal

SYNC_INTERVAL_SECS = 300
CONNECTIVITY_HOST = ("8.8.8.8", 53)
CONNECTIVITY_TIMEOUT_SECS = 3
HTTP_TIMEOUT_SECS = 15


def is_internet_available() -> bool:
    try:
        with socket.create_connection(CONNECTIVITY_HOST, timeout=CONNECTIVITY_TIMEOUT_SECS):
            return True
    except OSError:
        return False


def is_server_reachable(server_url: str) -> bool:
    try:
        parsed = urlparse(server_url)
        if not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((parsed.hostname, port), timeout=CONNECTIVITY_TIMEOUT_SECS):
            return True
    except (OSError, ValueError):
        return False


def _num(value):
    """Coerce a possibly-missing/None weight value to a plain float (0 default)."""
    if value is None:
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0

def build_payload(records: list[dict], company_id: str = "", machine_number: str = "") -> dict:
    """Build the EXACT supervisor-approved bulk JSON contract.

    Oracle TRUCK_WEIGHT schema mapping:
        COMPANY_ID      <- factory_number   (VARCHAR2(4) NOT NULL)
        ENTRY_TYPE      <- record_type       (VARCHAR2(10) NOT NULL)
        TRUCK_NO        <- truck_number      (VARCHAR2(50) NOT NULL)
        RECORD_DATE_TIME<- timestamp         (TIMESTAMP NOT NULL)
        MACHINE_ID      <- machine_number    (VARCHAR2(100) NOT NULL)
        DRIVER_NAME     <- driver_name       (VARCHAR2(200) nullable)
        DRIVER_CELL     <- driver_phone      (VARCHAR2(30) nullable)
        ENTRY_WEIGHT    <- entry_weight      (NUMBER(12,0) nullable)
        REF_ENTRY_ID    <- reference_entry_id(VARCHAR2(4) nullable)
        ID              <- entry_id          (VARCHAR2(4) NOT NULL PK)

    CRITICAL RULES:
      - entry_weight: ENTRY record mein actual weight, EXIT mein 0
      - exit_weight:  ENTRY record mein 0, EXIT mein actual weight
      - reference_entry_id: ENTRY mein apni khud ki ID (self-ref),
                            EXIT mein parent entry ki ID
      - company_id: kabhi empty nahi honi chahiye (COMPANY_ID NOT NULL)
    """
    items = []

    for record in records:
        raw_record_type = str(record.get("record_type", "Entry")).strip().upper()
        # "EXITREMOTE" tags an Exit created on a separate exit-gate
        # machine (see database.remote_exit_records) so local sync
        # bookkeeping can route it to the right table. The server only
        # knows ENTRY/EXIT, so it goes out on the wire as plain EXIT.
        is_exit = raw_record_type in ("EXIT", "EXITREMOTE")
        record_type = "EXIT" if is_exit else raw_record_type
        if record_type not in ("ENTRY", "EXIT"):
            raise ValueError(f"Unsupported record_type: {raw_record_type!r}")
        # reference_entry_id: ENTRY=self, EXIT=parent entry
        if is_exit:
            # EXIT points back to the original local Entry ID.
            ref_id = record.get("entry_id")
        else:
            # An ENTRY has no parent/reference entry.  The Entry ID itself
            # is carried in `entry_id`; reference_entry_id must be null.
            ref_id = None

        item = {
            "entry_id": str(record["id"]),
            "record_type": record_type,
            "reference_entry_id": str(ref_id) if ref_id is not None else "0",
            "truck_number": record.get("truck_number", ""),
            "timestamp": record.get("exit_time") if is_exit else record.get("entry_time"),
            "company_id": company_id,
            # Backward-compatible alias; harmless for APIs that ignore unknown fields.
            "factory_number": company_id,
            "machine_number": machine_number,
            "driver_name": record.get("driver_name"),
            "driver_phone": record.get("driver_phone"),
            "entry_weight": _num(record.get("entry_weight")) if not is_exit else 0,
            "exit_weight": _num(record.get("exit_weight")) if is_exit else 0,
        }

        items.append(item)

    return {"truck_records": items}


def push_bulk(server_url: str, token: str, records: list[dict], company_id: str = "", machine_number: str = "") -> dict:
    if not server_url:
        raise ValueError("Sync server URL is not configured.")
    if not token:
        raise ValueError("Sync token is not configured.")

    body = json.dumps(build_payload(records, company_id, machine_number), default=str).encode("utf-8")
    request = urllib.request.Request(
        server_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            if response.status not in (200, 201, 202):
                raise RuntimeError(f"Server returned HTTP {response.status}: {raw}")
            if not raw.strip():
                return {"success": True}
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                return {"success": True}
            if isinstance(result, dict):
                return result
            return {"success": True}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = ""
        raise RuntimeError(f"Server HTTP {exc.code}: {detail}")

class SyncManager(QThread):
    sync_started = Signal(int)
    sync_progress = Signal(int, int)
    sync_finished = Signal(int, int)
    status_changed = Signal(str, str)
    connectivity_changed = Signal(bool, bool)  # internet_ok, server_reachable

    def __init__(self, db_manager, get_server_url: Callable[[], str], get_token: Callable[[], str],
                 get_company_id: Callable[[], str] = lambda: "",
                 get_machine_number: Callable[[], str] = lambda: "",
                 get_auto_sync_enabled: Callable[[], bool] = lambda: True,
                 parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self._get_server_url = get_server_url
        self._get_token = get_token
        self._get_company_id = get_company_id
        self._get_machine_number = get_machine_number
        self._get_auto_sync_enabled = get_auto_sync_enabled
        self._auto_sync_enabled = bool(get_auto_sync_enabled())
        self._auto_sync_lock = threading.Lock()
        self._running = False
        self._wake_condition = threading.Condition()
        self._manual_sync_requested = False
        self._next_auto_sync_at = None

    def stop(self):
        with self._wake_condition:
            self._running = False
            self._manual_sync_requested = False
            self._wake_condition.notify_all()
        if self.isRunning():
            self.wait(5000)

    def _is_auto_sync_enabled(self) -> bool:
        with self._auto_sync_lock:
            return self._auto_sync_enabled

    def sync_now(self):
        # Manual Sync Now is available only while the sync control is ON.
        if not self._is_auto_sync_enabled():
            self.status_changed.emit("warning", "API sync is disabled.")
            return
        with self._wake_condition:
            if not self._running:
                self.status_changed.emit("warning", "Sync manager is not running.")
                return
            self._manual_sync_requested = True
            self._wake_condition.notify_all()

    def set_auto_sync_enabled(self, enabled: bool):
        """Apply the Settings sync switch immediately without restarting the app."""
        enabled = bool(enabled)
        with self._auto_sync_lock:
            previous = self._auto_sync_enabled
            self._auto_sync_enabled = enabled
        self._get_auto_sync_enabled = lambda: enabled

        with self._wake_condition:
            if not enabled:
                # Cancel both pending manual work and the automatic deadline.
                self._manual_sync_requested = False
                self._next_auto_sync_at = None
            elif not previous:
                # Turning ON starts a fresh 5-minute automatic interval.
                self._next_auto_sync_at = time.monotonic() + SYNC_INTERVAL_SECS
            self._wake_condition.notify_all()

    def run(self):
        self._running = True
        with self._wake_condition:
            if self._is_auto_sync_enabled():
                self._next_auto_sync_at = time.monotonic() + SYNC_INTERVAL_SECS
            else:
                self._next_auto_sync_at = None

        while self._running:
            manual_requested = False
            automatic_due = False

            with self._wake_condition:
                while self._running:
                    # Manual requests are handled immediately and do not reset
                    # the independent 5-minute automatic schedule.
                    if self._manual_sync_requested:
                        self._manual_sync_requested = False
                        manual_requested = True
                        break

                    if not self._is_auto_sync_enabled():
                        # OFF means no automatic timer and no API activity.
                        self._wake_condition.wait()
                        continue

                    if self._next_auto_sync_at is None:
                        self._next_auto_sync_at = time.monotonic() + SYNC_INTERVAL_SECS

                    remaining = self._next_auto_sync_at - time.monotonic()
                    if remaining <= 0:
                        automatic_due = True
                        # Schedule the next interval before doing network work
                        # so a slow API request cannot shift the 5-minute cycle.
                        self._next_auto_sync_at = time.monotonic() + SYNC_INTERVAL_SECS
                        break

                    # Settings changes/manual requests wake this wait immediately.
                    self._wake_condition.wait(timeout=remaining)

            if not self._running:
                break

            if not (manual_requested or automatic_due) or not self._is_auto_sync_enabled():
                continue

            try:
                # _do_sync_pass performs the local unsynced-record check first.
                # Therefore an automatic cycle with no pending records makes
                # no API request at all.
                self._do_sync_pass()
            except Exception as exc:
                self.status_changed.emit("error", f"Unexpected sync error: {exc}")

    @staticmethod
    def _result_keys(result: dict, key: str) -> set[tuple[str | None, str]]:
        """Return (record_type, entry_id) keys from a bulk response."""
        values = result.get(key, []) if isinstance(result, dict) else []
        if not isinstance(values, list):
            return set()

        keys = set()
        for value in values:
            record_type = None
            if isinstance(value, dict):
                entry_id = value.get("entry_id")
                if entry_id is None:
                    for alias in ("id", "local_id", "record_id"):
                        if value.get(alias) is not None:
                            entry_id = value.get(alias)
                            break
                record_type = value.get("record_type")
                record_type = (
                    str(record_type).strip().upper()
                    if record_type is not None else None
                )
            else:
                entry_id = value

            if entry_id is not None:
                keys.add((record_type, str(entry_id)))

        return keys

    def _do_sync_pass(self):
        if not self._running or not self._is_auto_sync_enabled():
            return

        server_url = (self._get_server_url() or "").strip()
        token = (self._get_token() or "").strip()

        records = self.db_manager.fetch_unsynced_records(limit=None)
        if not records:
            self.status_changed.emit("ok", "No unsynced records. No API call made.")
            return

        if not server_url:
            self.status_changed.emit("warning", "Server URL is not configured. Records remain Unsynced.")
            return
        if not token:
            self.status_changed.emit("warning", "Sync token is not configured. Records remain Unsynced.")
            return
        company_id = (self._get_company_id() or "").strip()
        if not company_id:
            self.status_changed.emit("warning", "Company ID is not configured. Records remain Unsynced because TRUCK_WEIGHT.COMPANY_ID cannot be NULL.")
            return

        machine_number = (self._get_machine_number() or "").strip()
        if not machine_number:
            self.status_changed.emit("warning", "Machine ID is not configured. Records remain Unsynced because TRUCK_WEIGHT.MACHINE_ID cannot be NULL.")
            return

        internet_ok = is_internet_available()
        server_ok = is_server_reachable(server_url) if internet_ok else False
        self.connectivity_changed.emit(internet_ok, server_ok)
        if not internet_ok:
            self.status_changed.emit("warning", "Internet unavailable. Records remain Unsynced.")
            return
        if not server_ok:
            self.status_changed.emit("warning", "Server is unreachable. Records remain Unsynced.")
            return

        # Re-check immediately before the network operation so switching the
        # Settings control OFF blocks a pending sync without restarting.
        if not self._running or not self._is_auto_sync_enabled():
            self.status_changed.emit("warning", "API sync is disabled. No API call made.")
            return

        # DEBUG: log exact payload to audit trail before sending
        payload_debug = build_payload(records, company_id, machine_number)
        self.db_manager.insert_audit_log(
            "SYNC_PAYLOAD",
            f"URL={server_url} | Payload={json.dumps(payload_debug, default=str)[:1500]}"
        )

        self.sync_started.emit(len(records))
        try:
            result = push_bulk(
                server_url, token, records,
                company_id,
                machine_number
            )
        except Exception as exc:
            for record in records:
                self.db_manager.mark_record_sync_failed(record["id"], str(exc), record.get("record_type", "Entry"))
            self.sync_finished.emit(0, len(records))
            self.status_changed.emit("error", f"Bulk sync failed: {exc}")
            return

        failed_keys = (
            self._result_keys(result, "failed")
            | self._result_keys(result, "errors")
        )
        success_keys = (
            self._result_keys(result, "synced")
            | self._result_keys(result, "success")
            | self._result_keys(result, "accepted")
        )

        succeeded = 0
        failed = 0
        explicit_partial = bool(failed_keys or success_keys)
        response_failed = (
            isinstance(result, dict)
            and result.get("success") is False
            and not explicit_partial
        )

        for index, record in enumerate(records, start=1):
            rid = str(record["id"])
            local_record_type = str(record.get("record_type", "Entry")).strip().upper()
            # Server responses only ever echo ENTRY/EXIT (see build_payload);
            # a separate-exit-gate record is EXITREMOTE locally but was sent
            # (and comes back) as EXIT, so match against the wire type while
            # still marking the correct local table with local_record_type.
            wire_record_type = "EXIT" if local_record_type in ("EXIT", "EXITREMOTE") else "ENTRY"
            record_key = (wire_record_type, rid)
            key_match = record_key in success_keys or (None, rid) in success_keys
            key_failed = record_key in failed_keys or (None, rid) in failed_keys
            if response_failed or key_failed:
                error = f"Server rejected entry_id={rid} ({wire_record_type})"
                self.db_manager.mark_record_sync_failed(record["id"], error, local_record_type)
                failed += 1
            elif explicit_partial and not key_match:
                error = f"Bulk response did not confirm entry_id={rid} ({wire_record_type})"
                self.db_manager.mark_record_sync_failed(record["id"], error, local_record_type)
                failed += 1
            else:
                remote_id = rid
                self.db_manager.mark_record_synced(record["id"], local_record_type, remote_id)
                succeeded += 1
            self.sync_progress.emit(index, len(records))

        self.sync_finished.emit(succeeded, failed)
        self.status_changed.emit(
            "ok" if failed == 0 else "warning",
            f"Bulk sync complete: {succeeded} synced, {failed} failed."
        )