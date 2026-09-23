"""Persistent, nonblocking recovery-alert notification decisions."""

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

from app.recovery_alert import evaluate_recovery_alert


SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = Path("runtime/recovery-alert/state.json")
STATES = {"healthy", "warning", "critical", "check_error"}
ACTIONS = {"none", "notify_warning", "notify_critical", "notify_check_error", "notify_recovery"}
ABNORMAL_STATES = {"warning", "critical", "check_error"}
INTERVALS = {"warning": timedelta(hours=24), "critical": timedelta(hours=6), "check_error": timedelta(hours=1)}
STATE_FIELDS = {
    "schema_version", "last_observed_state", "last_observed_reason_code", "last_evaluated_at",
    "last_notification_state", "last_notification_action", "last_notification_at",
    "pending_decision_id", "pending_action", "pending_state", "pending_created_at",
}


def state_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_STATE_PATH


def _utc(value: datetime | None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("state_error")
    return instant.astimezone(timezone.utc)


def _timestamp(value: object, now: datetime) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("state_error")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("state_error") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("state_error")
    parsed = parsed.astimezone(timezone.utc)
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


def _empty() -> dict[str, Any]:
    return {field: (SCHEMA_VERSION if field == "schema_version" else None) for field in STATE_FIELDS}


def _load(path: Path, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("state_error") from exc
    if not isinstance(value, dict) or set(value) != STATE_FIELDS or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("state_error")
    observed = (value["last_observed_state"], value["last_observed_reason_code"], value["last_evaluated_at"])
    notified = (value["last_notification_state"], value["last_notification_action"], value["last_notification_at"])
    pending = (value["pending_decision_id"], value["pending_action"], value["pending_state"], value["pending_created_at"])
    if any(item is None for item in observed):
        if any(item is not None for item in observed): raise ValueError("state_error")
    elif observed[0] not in STATES or not isinstance(observed[1], str): raise ValueError("state_error")
    else: _timestamp(observed[2], now)
    if any(item is None for item in notified):
        if any(item is not None for item in notified): raise ValueError("state_error")
    elif notified[0] not in STATES or notified[1] not in ACTIONS - {"none"}: raise ValueError("state_error")
    else: _timestamp(notified[2], now)
    if any(item is None for item in pending):
        if any(item is not None for item in pending): raise ValueError("state_error")
    elif not (isinstance(pending[0], str) and len(pending[0]) == 24 and all(c in "0123456789abcdef" for c in pending[0]) and pending[1] in ACTIONS - {"none"} and pending[2] in STATES):
        raise ValueError("state_error")
    else: _timestamp(pending[3], now)
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: str | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(raw, path); raw = None
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if raw:
            try: os.unlink(raw)
            except FileNotFoundError: pass


def _action(state: str, stored: dict[str, Any], now: datetime) -> tuple[str, bool]:
    if state in ABNORMAL_STATES:
        action = f"notify_{state}"
        if stored["last_notification_state"] == state:
            if now - _timestamp(stored["last_notification_at"], now) < INTERVALS[state]: return "none", False
            return action, True
        return action, False
    if state == "healthy" and stored["last_notification_state"] in ABNORMAL_STATES:
        return "notify_recovery", False
    return "none", False


def _id(state: str, action: str, reason: str, now: datetime) -> str:
    return hashlib.sha256(f"{state}:{action}:{reason}:{now.isoformat()}".encode()).hexdigest()[:24]


def _error(code: str) -> dict[str, Any]:
    return {"status": "decision_busy" if code == "decision_busy" else "error", "notify": False, "action": "none", "error_code": code}


def _result(result: dict[str, Any], action: str, decision_id: str | None, reminder: bool, last: object) -> dict[str, Any]:
    return {**result, "status": "ok", "notify": action != "none", "action": action, "is_reminder": reminder, "decision_id": decision_id, "last_notification_at": last, "error_code": None}


def decide(*, state_file: str | Path | None = None, recovery_health_artifact: str | Path | None = None, restore_drill_artifact: str | Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    try: current = _utc(now)
    except ValueError: return _error("state_error")
    path = state_path(state_file)
    with _lock(path) as acquired:
        if not acquired: return _error("decision_busy")
        try:
            stored = _load(path, current)
            kwargs = {"now": current}
            if recovery_health_artifact is not None: kwargs["recovery_health_artifact"] = recovery_health_artifact
            if restore_drill_artifact is not None: kwargs["restore_drill_artifact"] = restore_drill_artifact
            result = evaluate_recovery_alert(**kwargs)
            state, reason = result["state"], result["reason_code"]
            if state not in STATES or not isinstance(reason, str): raise ValueError("state_error")
            action, reminder = _action(state, stored, current)
        except Exception:
            return _error("state_error")
        pending = stored["pending_decision_id"]
        if pending and action != "none" and stored["pending_state"] == state and stored["pending_action"] == action:
            return _result(result, action, pending, False, stored["last_notification_at"])
        updated = dict(stored)
        updated.update(last_observed_state=state, last_observed_reason_code=reason, last_evaluated_at=current.isoformat(), pending_decision_id=None, pending_action=None, pending_state=None, pending_created_at=None)
        decision_id = None
        if action != "none":
            decision_id = _id(state, action, reason, current)
            updated.update(pending_decision_id=decision_id, pending_action=action, pending_state=state, pending_created_at=current.isoformat())
        try: _save(path, updated)
        except OSError: return _error("state_error")
        return _result(result, action, decision_id, reminder, updated["last_notification_at"])


def acknowledge(decision_id: str, *, state_file: str | Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    try: current = _utc(now)
    except ValueError: return _error("state_error")
    path = state_path(state_file)
    with _lock(path) as acquired:
        if not acquired: return _error("decision_busy")
        try: stored = _load(path, current)
        except Exception: return _error("state_error")
        if not isinstance(decision_id, str) or stored["pending_decision_id"] != decision_id: return _error("invalid_decision")
        if stored["pending_action"] not in ACTIONS - {"none"} or stored["pending_state"] not in STATES: return _error("invalid_decision")
        stored.update(last_notification_state=stored["pending_state"], last_notification_action=stored["pending_action"], last_notification_at=current.isoformat(), pending_decision_id=None, pending_action=None, pending_state=None, pending_created_at=None)
        try: _save(path, stored)
        except OSError: return _error("state_error")
        return {"status": "ok", "error_code": None}
