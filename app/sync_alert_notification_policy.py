"""Read-only notification decisions for sync-health evaluator results."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from app.sync_alert import evaluate_sync_health


SCHEMA_VERSION = 1
STATES = {"not_initialized", "healthy", "warning", "critical", "check_error"}
ACTIONS = {"none", "notify_warning", "notify_critical", "notify_check_error", "notify_recovery"}
ABNORMAL_STATES = {"warning", "critical", "check_error"}
INTERVALS = {
    "warning": timedelta(hours=24),
    "critical": timedelta(hours=6),
    "check_error": timedelta(hours=1),
}
STATE_FIELDS = {
    "schema_version",
    "last_observed_state",
    "last_observed_reason_code",
    "last_evaluated_at",
    "last_notification_state",
    "last_notification_action",
    "last_notification_at",
    "pending_decision_id",
    "pending_action",
    "pending_state",
    "pending_created_at",
}


def state_path(db_path: str) -> Path:
    database = Path(db_path).resolve()
    return database.with_name(f"{database.name}.sync-alert-state.json")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, now: datetime) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("state_error")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("state_error") from exc
    parsed = _utc(parsed)
    if parsed > now:
        raise ValueError("state_error")
    return parsed


@contextmanager
def _lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(f"{path}.lock", "a", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "last_observed_state": None,
        "last_observed_reason_code": None,
        "last_evaluated_at": None,
        "last_notification_state": None,
        "last_notification_action": None,
        "last_notification_at": None,
        "pending_decision_id": None,
        "pending_action": None,
        "pending_state": None,
        "pending_created_at": None,
    }


def _load(path: Path, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("state_error") from exc
    if not isinstance(value, dict) or set(value) != STATE_FIELDS:
        raise ValueError("state_error")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("state_error")

    observed = (value["last_observed_state"], value["last_observed_reason_code"], value["last_evaluated_at"])
    if any(item is None for item in observed):
        if any(item is not None for item in observed):
            raise ValueError("state_error")
    else:
        if observed[0] not in STATES or not isinstance(observed[1], str):
            raise ValueError("state_error")
        _parse_timestamp(observed[2], now)

    notified = (value["last_notification_state"], value["last_notification_action"], value["last_notification_at"])
    if any(item is None for item in notified):
        if any(item is not None for item in notified):
            raise ValueError("state_error")
    else:
        if notified[0] not in STATES or notified[1] not in ACTIONS - {"none"}:
            raise ValueError("state_error")
        _parse_timestamp(notified[2], now)

    pending = (value["pending_decision_id"], value["pending_action"], value["pending_state"], value["pending_created_at"])
    if any(item is None for item in pending):
        if any(item is not None for item in pending):
            raise ValueError("state_error")
    else:
        if (
            not isinstance(pending[0], str)
            or not pending[0]
            or pending[1] not in ACTIONS - {"none"}
            or pending[2] not in STATES
        ):
            raise ValueError("state_error")
        _parse_timestamp(pending[3], now)
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(value, temporary, sort_keys=True, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        os.chmod(path, 0o600)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _action_for(state: str, stored: dict[str, Any], now: datetime) -> tuple[str, bool]:
    if state in ABNORMAL_STATES:
        action = f"notify_{state}"
        if stored["last_notification_state"] == state:
            last = _parse_timestamp(stored["last_notification_at"], now)
            if now - last < INTERVALS[state]:
                return "none", False
            return action, True
        return action, False
    if state == "healthy" and stored["last_notification_state"] in ABNORMAL_STATES:
        return "notify_recovery", False
    return "none", False


def _decision_id(state: str, action: str, now: datetime) -> str:
    material = f"{state}:{action}:{now.isoformat()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _result(result: dict[str, Any], *, action: str, decision_id: str | None, reminder: bool, last_notification_at: object) -> dict[str, Any]:
    return {
        **result,
        "status": "ok",
        "notify": action != "none",
        "action": action,
        "is_reminder": reminder,
        "decision_id": decision_id,
        "last_notification_at": last_notification_at,
        "error_code": None,
    }


def _error(code: str) -> dict[str, Any]:
    return {"status": "decision_busy" if code == "decision_busy" else "error", "notify": False, "action": "none", "error_code": code}


def decide(conn: Any, db_path: str, now: datetime | None = None) -> dict[str, Any]:
    current = _utc(now or datetime.now(timezone.utc))
    path = state_path(db_path)
    with _lock(path) as acquired:
        if not acquired:
            return _error("decision_busy")
        try:
            stored = _load(path, current)
            result = evaluate_sync_health(conn, now=current)
            state = result["state"]
            if state not in STATES:
                raise ValueError("state_error")
            action, reminder = _action_for(state, stored, current)
        except Exception:
            return _error("state_error")

        pending_id = stored["pending_decision_id"]
        if pending_id and stored["pending_state"] == state and stored["pending_action"] == action and action != "none":
            return _result(result, action=action, decision_id=pending_id, reminder=False, last_notification_at=stored["last_notification_at"])

        updated = dict(stored)
        if pending_id:
            updated.update(pending_decision_id=None, pending_action=None, pending_state=None, pending_created_at=None)
        updated.update(
            last_observed_state=state,
            last_observed_reason_code=result["reason_code"],
            last_evaluated_at=current.isoformat(),
        )
        decision_id = None
        if action != "none":
            decision_id = _decision_id(state, action, current)
            updated.update(
                pending_decision_id=decision_id,
                pending_action=action,
                pending_state=state,
                pending_created_at=current.isoformat(),
            )
        _save(path, updated)
        return _result(result, action=action, decision_id=decision_id, reminder=reminder, last_notification_at=updated["last_notification_at"])


def acknowledge(db_path: str, decision_id: str, now: datetime | None = None) -> dict[str, Any]:
    current = _utc(now or datetime.now(timezone.utc))
    path = state_path(db_path)
    with _lock(path) as acquired:
        if not acquired:
            return _error("decision_busy")
        try:
            stored = _load(path, current)
        except Exception:
            return _error("state_error")
        if not isinstance(decision_id, str) or not decision_id or stored["pending_decision_id"] != decision_id:
            return _error("invalid_decision")
        action = stored["pending_action"]
        state = stored["pending_state"]
        if action == "none" or action is None or state is None:
            return _error("invalid_decision")
        stored.update(
            last_notification_state=state,
            last_notification_action=action,
            last_notification_at=current.isoformat(),
            pending_decision_id=None,
            pending_action=None,
            pending_state=None,
            pending_created_at=None,
        )
        _save(path, stored)
        return {"status": "ok", "error_code": None}
