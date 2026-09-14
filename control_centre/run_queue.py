"""A small persisted FIFO queue for control centre actions — lets you queue
up many jobs (most usefully "Run pipeline" across several areas) to run
unattended, one at a time, instead of clicking Run and waiting for each one
individually.

Pure state management only — no subprocess logic lives here. server.py's
own background worker thread drains this queue using the exact same
actions.build_command() / subprocess execution /api/run already uses (see
server.py's _stream_command), so a queued run is never a different code
path than a manual one, just deferred and unattended. It also shares the
same single "currently running" slot as a manual run, so the two can never
run two processes at once.

Persisted to queue_state.json so a queued batch survives a server restart
(the worker thread just picks back up where it left off) — this is the
whole point of a queue: leave it running overnight.
"""

import json
import threading
import time
import uuid
from pathlib import Path

_STATE_PATH = Path(__file__).resolve().parent / "queue_state.json"

_lock = threading.Lock()
_items: list[dict] = []
_paused = False
_loaded = False


def _save() -> None:
    _STATE_PATH.write_text(json.dumps(_items, indent=2))


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if _STATE_PATH.exists():
        try:
            items = json.loads(_STATE_PATH.read_text())
        except Exception:
            items = []
    else:
        items = []
    # An item still "running" here means the server process itself stopped
    # or crashed mid-run (a normal Stop/Kill already resolves its item to
    # "cancelled" well before this would ever be read back) — reset it to
    # pending so it gets retried rather than staying stuck forever. Rare
    # edge case: if the underlying subprocess somehow survived the server's
    # own death (it normally doesn't), this could start a second, duplicate
    # run of the same area — acceptable given how unlikely it is.
    for item in items:
        if item["status"] == "running":
            item["status"] = "pending"
    _items.extend(items)


def add(action_id: str, slug: str | None, params: dict, label: str) -> dict:
    """Returns the *live* item (same object stored internally, like
    next_pending() — see its docstring), not a copy, so a caller that goes
    on to call mark_running/mark_finished on what add() gave back (the
    worker loop doesn't do this today, but a future caller might) actually
    mutates the real entry."""
    _ensure_loaded()
    with _lock:
        item = {
            "id": uuid.uuid4().hex,
            "action_id": action_id,
            "slug": slug,
            "params": params,
            "label": label,
            "status": "pending",
            "added_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
        }
        _items.append(item)
        _save()
        return item


def remove(item_id: str) -> bool:
    """Only removes a still-pending item — a running one needs /api/kill
    (stopping it also resolves its queue entry, see server.py's worker
    loop); a finished one is just history."""
    _ensure_loaded()
    with _lock:
        for i, item in enumerate(_items):
            if item["id"] == item_id and item["status"] == "pending":
                del _items[i]
                _save()
                return True
        return False


def retry(item_id: str) -> bool:
    """Re-queues a failed/cancelled item — resets it back to pending."""
    _ensure_loaded()
    with _lock:
        for item in _items:
            if item["id"] == item_id and item["status"] in ("failed", "cancelled"):
                item["status"] = "pending"
                item["started_at"] = None
                item["finished_at"] = None
                item["exit_code"] = None
                _save()
                return True
        return False


def clear_finished() -> int:
    """Drops every done/failed/cancelled item — pending and running are
    left alone. Returns how many were cleared."""
    _ensure_loaded()
    with _lock:
        before = len(_items)
        _items[:] = [i for i in _items if i["status"] in ("pending", "running")]
        _save()
        return before - len(_items)


def list_items() -> list[dict]:
    _ensure_loaded()
    with _lock:
        return [dict(i) for i in _items]


def set_paused(paused: bool) -> None:
    global _paused
    _paused = paused


def is_paused() -> bool:
    return _paused


def next_pending() -> dict | None:
    """Returns a *live* reference into the internal list (not a copy) — the
    worker loop mutates it directly via mark_running/mark_finished rather
    than round-tripping an id, since it's all single-process/single-worker."""
    _ensure_loaded()
    with _lock:
        for item in _items:
            if item["status"] == "pending":
                return item
        return None


def mark_running(item: dict) -> None:
    with _lock:
        item["status"] = "running"
        item["started_at"] = time.time()
        _save()


def mark_finished(item: dict, exit_code: int | None) -> None:
    with _lock:
        cancelled = exit_code is not None and exit_code < 0
        item["status"] = "cancelled" if cancelled else ("done" if exit_code == 0 else "failed")
        item["exit_code"] = exit_code
        item["finished_at"] = time.time()
        _save()
