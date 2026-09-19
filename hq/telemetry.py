"""Fail-open, local turn accounting. Never sends data or controls workers."""
from __future__ import annotations
import json
import os
import time
import uuid
from pathlib import Path

FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

class TurnStore:
    def __init__(self, path: Path):
        self.path = path
        self.turns = {}
        self.messages = {}
        self.background = {}
        self.error = None
        self.loaded = False

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            self.turns = data.get("turns", {})
            self.background = data.get("background", {})
            for turn in self.turns.values():
                if turn["status"] in ("queued", "running"):
                    turn.update(status="interrupted", finished=time.time(), reason="HQ restarted; completion unknown")
            # A persisted background status is not proof of a live process after restart.
            for task in self.background.values():
                if task.get("status") in ("task_started", "task_progress"):
                    task["status"] = "unknown_after_restart"
        except Exception as e:
            self.error = f"History unavailable: {type(e).__name__}"
            # Preserve a damaged history instead of silently replacing it.
            self.loaded = False

    def save(self):
        if not self.loaded:
            return
        try:
            terminal = sorted((v for v in self.turns.values() if v["status"] not in ("queued", "running")), key=lambda v:v["queued"])
            for old in terminal[:-500]:
                self.turns.pop(old["id"], None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"version":1,"turns":self.turns,"background":self.background}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self.error = None
        except Exception as e:
            self.error = f"History write failed: {type(e).__name__}"

    def create(self, project, trace):
        self.load()
        tid = uuid.uuid4().hex
        self.turns[tid] = dict(id=tid, project=project, source=trace.get("source", "internal"),
                              prompt=trace.get("prompt", "")[:500],
                              task_id=trace.get("task_id"), status="queued", queued=time.time(),
                              usage={}, requests=0, tools=0)
        self.messages[tid] = {}
        self.save()
        return tid

    def update(self, tid, **fields):
        if tid in self.turns:
            self.turns[tid].update(fields)
            self.save()

    def message(self, tid, msg):
        if tid not in self.turns or msg.get("parent_tool_use_id"):
            return  # Subagent usage is separate, never mix it into the parent silently.
        message = msg.get("message") or {}
        mid, usage = message.get("id"), message.get("usage")
        if mid and isinstance(usage, dict):
            self.messages[tid][mid] = {k:max(0,int(usage.get(k) or 0)) for k in FIELDS}
            values = self.messages[tid].values()
            self.turns[tid]["usage"] = {k:sum(u[k] for u in values) for k in FIELDS}
            self.turns[tid]["requests"] = len(self.messages[tid])
        for block in message.get("content") or []:
            if block.get("type") == "tool_use":
                # IDs deduplicate streamed copies of the same tool call.
                ids = self.turns[tid].setdefault("tool_ids", [])
                tool_id = block.get("id")
                if tool_id and tool_id not in ids:
                    ids.append(tool_id)
                    self.turns[tid]["tools"] = len(ids)
                self.turns[tid]["tool"] = block.get("name")
        self.turns[tid]["last_event"] = time.time()
        self.save()

    def finish(self, tid, meta, reason=None):
        failed = reason or any(meta.get(k) for k in ("is_error", "died", "timeout"))
        self.update(tid, status="failed" if failed else "finished", finished=time.time(),
                    reason=reason or meta.get("subtype"), session=meta.get("session_id") or self.turns.get(tid,{}).get("session"))
        self.messages.pop(tid, None)

    def task_event(self, project, msg):
        self.load()
        tid = msg.get("task_id")
        if not tid:
            return
        key = f"{project}:{tid}"
        self.background[key] = dict(id=tid, project=project, status=msg.get("status") or msg.get("subtype"),
                                   description=str(msg.get("description") or "")[:300], last_event=time.time(),
                                   tool_use_id=msg.get("tool_use_id"), session=msg.get("session_id"))
        if len(self.background)>300:
            oldest = min(self.background,key=lambda k:self.background[k]["last_event"])
            self.background.pop(oldest)
        self.save()

    def snapshot(self):
        self.load()
        return {"turns":list(self.turns.values()),"background":list(self.background.values()),"error":self.error}

STORE = TurnStore(Path(__file__).resolve().parent / "state/office-turns.json")

def record(method, *args, **kwargs):
    """Telemetry is never allowed to break a user's turn."""
    try:
        return getattr(STORE, method)(*args, **kwargs)
    except Exception as e:
        STORE.error = f"Telemetry error: {type(e).__name__}"
        return None
