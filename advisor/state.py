"""Persistent advisor state: session ↔ Codex thread binding + Q/A digest.

Why this module exists
----------------------
A Codex *thread* lives inside Codex (``~/.codex``), but the **mapping** from a Hermes
session to that thread must live on our side. Without it every call starts a fresh
thread and the advisor loses continuity — which is the whole point of an advisor.

Design rules
------------
* One Hermes session → at most one Codex thread (invariant; rebinding is explicit).
* The digest keeps the last N (question, answer) pairs **verbatim but truncated** so a
  dead thread can be replayed into a new one (fail-closed recovery).
* Writes are atomic (tmp + os.replace) and serialized with an exclusive file lock, so
  concurrent tool calls cannot corrupt the file.
* The file is human-readable JSON on purpose: this is operational state an operator
  may need to inspect by hand at 3am.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator, Optional

try:  # pragma: no cover - fcntl is POSIX-only; Hermes runs on Linux
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
_MAX_QUESTION_CHARS = 800
_MAX_ANSWER_CHARS = 2000


@dataclass
class TurnRecord:
    """One advisor exchange, kept for digest replay."""

    question: str
    answer: str
    at: float
    turn_id: str = ""
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "TurnRecord":
        return cls(
            question=str(raw.get("question") or ""),
            answer=str(raw.get("answer") or ""),
            at=float(raw.get("at") or 0.0),
            turn_id=str(raw.get("turn_id") or ""),
            error=str(raw.get("error") or ""),
        )


@dataclass
class Binding:
    """State for one Hermes session."""

    session_key: str
    thread_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_turn_at: float = 0.0
    turns: list[TurnRecord] = field(default_factory=list)
    # Number of times the bound thread had to be recreated (resume failure).
    restarts: int = 0
    # Health bookkeeping, persisted so a restart doesn't erase a bad streak.
    last_success_at: float = 0.0
    consecutive_failures: int = 0
    last_error: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["turns"] = [t.to_json() for t in self.turns]
        return data

    @classmethod
    def from_json(cls, session_key: str, raw: dict[str, Any]) -> "Binding":
        return cls(
            session_key=session_key,
            thread_id=str(raw.get("thread_id") or ""),
            created_at=float(raw.get("created_at") or time.time()),
            last_turn_at=float(raw.get("last_turn_at") or 0.0),
            turns=[TurnRecord.from_json(t) for t in (raw.get("turns") or []) if isinstance(t, dict)],
            restarts=int(raw.get("restarts") or 0),
            last_success_at=float(raw.get("last_success_at") or 0.0),
            consecutive_failures=int(raw.get("consecutive_failures") or 0),
            last_error=str(raw.get("last_error") or ""),
        )

    def record(self, question: str, answer: str, *, turn_id: str = "", error: str = "", keep: int = 6) -> None:
        """Append a turn, truncated, keeping at most ``keep`` entries."""
        self.turns.append(
            TurnRecord(
                question=_clip(question, _MAX_QUESTION_CHARS),
                answer=_clip(answer, _MAX_ANSWER_CHARS),
                at=time.time(),
                turn_id=turn_id,
                error=error,
            )
        )
        if keep > 0 and len(self.turns) > keep:
            del self.turns[: len(self.turns) - keep]
        self.last_turn_at = time.time()
        if error:
            self.consecutive_failures += 1
            self.last_error = _clip(error, 400)
        else:
            self.consecutive_failures = 0
            self.last_error = ""
            self.last_success_at = self.last_turn_at

    @property
    def health(self) -> str:
        """Coarse verdict so callers don't have to interpret counters themselves."""
        if self.consecutive_failures >= 3:
            return "failing"
        if self.consecutive_failures > 0:
            return "degraded"
        return "ok" if self.last_success_at else "idle"

    def digest(self, turns: int = 6) -> str:
        """Render recent Q/A as a compact replay block for a fresh thread."""
        if not self.turns:
            return ""
        recent = self.turns[-turns:] if turns > 0 else []
        lines = [
            "以下是此前与同一用户的历史问答（原线程不可用，摘要重放以保持连续性）。"
            "把它们当作背景，不要逐条复述：",
            "",
        ]
        for i, t in enumerate(recent, 1):
            status = "（当时失败）" if t.error else ""
            lines.append(f"Q{i}{status}: {t.question}")
            if t.answer:
                lines.append(f"A{i}: {t.answer}")
            lines.append("")
        return "\n".join(lines).strip()


class StateStore:
    """Atomic, locked JSON store for advisor bindings."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # -- plumbing ---------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": SCHEMA_VERSION, "sessions": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt state must never take the advisor down; start clean but keep a copy.
            try:
                self.path.replace(self.path.with_suffix(self.path.suffix + f".corrupt.{int(time.time())}"))
            except OSError:
                pass
            return {"schema": SCHEMA_VERSION, "sessions": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
            return {"schema": SCHEMA_VERSION, "sessions": {}}
        raw.setdefault("schema", SCHEMA_VERSION)
        return raw

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- public API -------------------------------------------------------
    def get(self, session_key: str) -> Binding:
        with self._locked():
            data = self._read_all()
            raw = data["sessions"].get(session_key)
        return Binding.from_json(session_key, raw or {})

    def save(self, binding: Binding) -> None:
        with self._locked():
            data = self._read_all()
            data["sessions"][binding.session_key] = binding.to_json()
            self._write_all(data)

    def reset(self, session_key: str, *, reason: str = "") -> Binding:
        """Drop the thread binding but keep the digest (called by codex_new)."""
        with self._locked():
            data = self._read_all()
            raw = data["sessions"].get(session_key) or {}
            binding = Binding.from_json(session_key, raw)
            binding.thread_id = ""
            binding.created_at = time.time()
            binding.restarts += 1 if reason else 0
            data["sessions"][session_key] = binding.to_json()
            self._write_all(data)
        return binding

    def all_bindings(self) -> dict[str, Binding]:
        with self._locked():
            data = self._read_all()
        return {k: Binding.from_json(k, v) for k, v in data["sessions"].items()}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n…（已截断）"


def default_session_key(session_id: Optional[str], chat_id: Optional[str] = None) -> str:
    """Stable key for the current Hermes session.

    Prefers the agent session id (stable across turns within one conversation); falls
    back to the platform chat id so a gateway restart with a new session id still lands
    on the same advisor thread.
    """
    if session_id:
        return f"session:{session_id}"
    if chat_id:
        return f"chat:{chat_id}"
    return "session:default"
