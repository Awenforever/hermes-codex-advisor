"""Advisor business logic: continuity, fail-closed recovery, honest reporting.

Layering
--------
``service.AdvisorService`` is the only thing the tools touch. It owns:
  * the session↔thread binding (via ``StateStore``),
  * one live ``AdvisorSession`` (so we don't respawn codex per question),
  * the "thread died → replay a digest, and say so" recovery path,
  * prompt composition (advisor preamble + digest + question).

None of it touches the wire protocol; ``session.py`` owns that.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .session import AdvisorSession, TurnOutcome
from .state import Binding, StateStore

logger = logging.getLogger("hermes-codex-advisor.service")

ADVISOR_PREAMBLE = """你是 Hermes（一个以 deepseek-flash 为主模型的执行型 agent）的私人顾问。
调用方会把现状、原始证据、约束和验收标准交给你；你的职责是给出**判断**，而不是复述。

工作纪律：
1. 只依据对方给出的证据作答；证据不足就明确说"证据不足，需要 X"，不要猜。
2. 不预设推论：不要说"应该是 X 导致的"，除非证据支持；把事实和判断分开写。
3. 这是只读会话：你无法修改文件、无法执行有副作用的命令。需要落地动作时，给出明确的动作清单让对方执行。
4. 答案要短、可执行：先给结论，再给依据（引用文件:行号或命令输出），必要时给下一步。
5. 用中文回答。"""


@dataclass
class AskResult:
    ok: bool
    answer: str = ""
    thread_id: str = ""
    turn_id: str = ""
    model: str = ""
    effort: str = ""
    elapsed: float = 0.0
    resumed: bool = False
    restarted: bool = False
    declined_approvals: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def to_payload(self, *, max_chars: int = 12000) -> dict[str, Any]:
        answer = self.answer
        if max_chars and len(answer) > max_chars:
            answer = answer[: max_chars - 40].rstrip() + "\n…（答案过长已截断）"
        payload: dict[str, Any] = {
            "ok": self.ok,
            "answer": answer,
            "source": "codex-advisor",
            "thread_id": self.thread_id,
            "model": self.model,
            "effort": self.effort,
            "elapsed_seconds": round(self.elapsed, 1),
            "resumed": self.resumed,
        }
        if self.turn_id:
            payload["turn_id"] = self.turn_id
        if self.restarted:
            payload["restarted"] = True
        if self.declined_approvals:
            payload["declined_approvals"] = self.declined_approvals
        if self.notes:
            payload["notes"] = self.notes
        if self.error:
            payload["error"] = self.error
        return payload


class AdvisorService:
    """Session-scoped advisor facade. One instance per plugin load."""

    def __init__(
        self,
        *,
        store: StateStore,
        session_factory: Optional[Callable[..., AdvisorSession]] = None,
        model: str,
        effort: str,
        sandbox: str,
        codex_bin: str,
        codex_home: Optional[str],
        cwd: Optional[str],
        timeout: float,
        digest_turns: int,
        max_answer_chars: int,
    ) -> None:
        self.store = store
        self.model = model
        self.effort = effort
        self.sandbox = sandbox
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.cwd = cwd
        self.timeout = timeout
        self.digest_turns = digest_turns
        self.max_answer_chars = max_answer_chars
        self._session_factory = session_factory
        self._session: Optional[AdvisorSession] = None
        self._lock = threading.Lock()

    # -- session plumbing -------------------------------------------------
    def _get_session(self) -> AdvisorSession:
        if self._session is not None and self._session.alive():
            return self._session
        if self._session is not None:
            logger.info("advisor: recycling dead app-server session")
            self._session.close()
        factory = self._session_factory or AdvisorSession
        self._session = factory(
            codex_bin=self.codex_bin,
            codex_home=self.codex_home,
            model=self.model,
            effort=self.effort,
            sandbox=self.sandbox,
            cwd=self.cwd,
        )
        return self._session

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    # -- public API -------------------------------------------------------
    def ask(self, session_key: str, question: str, *, extra_context: str = "", fresh: bool = False) -> AskResult:
        question = (question or "").strip()
        if not question:
            return AskResult(ok=False, error="问题为空", model=self.model, effort=self.effort)

        with self._lock:
            binding = self.store.get(session_key)
            if fresh:
                binding = self.store.reset(session_key, reason="fresh=True")

            result = AskResult(ok=False, model=self.model, effort=self.effort)
            session = self._get_session()

            # 1) bind to the persisted thread (or start one)
            wanted = binding.thread_id
            try:
                thread_id, resumed = session.ensure_thread(wanted or None)
            except Exception as exc:
                result.error = f"无法建立 codex 线程：{_short(exc)}"
                binding.record(question, "", error=result.error)
                self.store.save(binding)
                return result

            result.thread_id = thread_id
            result.resumed = resumed

            # 2) thread lost? fail-closed: new thread + digest, and SAY SO
            digest = ""
            if wanted and not resumed:
                digest = binding.digest(self.digest_turns)
                result.restarted = True
                result.notes.append(
                    f"原线程 {wanted[:8]} 已不可用（resume 失败），已新建线程并用最近 {len(binding.turns[-self.digest_turns:])} 轮问答摘要续接。"
                )
                binding.thread_id = thread_id
                binding.restarts += 1
            elif not wanted:
                binding.thread_id = thread_id

            # 3) compose the turn
            parts: list[str] = []
            if not resumed:
                parts.append(ADVISOR_PREAMBLE)
            if digest:
                parts.append(digest)
            if extra_context.strip():
                parts.append("## 补充证据\n" + extra_context.strip())
            parts.append("## 问题\n" + question)
            message = "\n\n".join(parts)

            # 4) run it
            outcome: TurnOutcome = session.ask(message, timeout=self.timeout)
            result.elapsed = outcome.elapsed
            result.turn_id = outcome.turn_id
            result.declined_approvals = outcome.declined_approvals
            result.ok = outcome.ok
            result.answer = outcome.text
            if not outcome.ok:
                result.error = outcome.error or "未知失败"

            # 5) persist (success and failure both — the digest must reflect reality)
            binding.record(question, result.answer, turn_id=outcome.turn_id, error=result.error)
            binding.thread_id = thread_id
            self.store.save(binding)
            return result

    def status(self, session_key: str) -> dict[str, Any]:
        binding: Binding = self.store.get(session_key)
        session = self._session
        alive = bool(session and session.alive())
        payload = {
            "ok": True,
            "source": "codex-advisor",
            "session_key": session_key,
            "thread_id": binding.thread_id,
            "has_thread": bool(binding.thread_id),
            "turns_recorded": len(binding.turns),
            "restarts": binding.restarts,
            "last_turn_at": _iso(binding.last_turn_at),
            "model": self.model,
            "effort": self.effort,
            "sandbox": self.sandbox,
            "app_server_alive": alive,
            "timeout_seconds": self.timeout,
            "state_path": str(self.store.path),
            "codex_bin": self.codex_bin,
            "codex_home": self.codex_home,
        }
        if binding.turns:
            last = binding.turns[-1]
            payload["last_turn"] = {
                "question": last.question[:200],
                "answered": bool(last.answer),
                "error": last.error[:200] if last.error else "",
            }
        return payload

    def reset(self, session_key: str, *, reason: str = "") -> dict[str, Any]:
        binding = self.store.reset(session_key, reason=reason or "manual")
        if self._session is not None:
            # Drop the process too: a fresh thread deserves a fresh app-server.
            self._session.close()
            self._session = None
        return {
            "ok": True,
            "source": "codex-advisor",
            "session_key": session_key,
            "previous_thread_id": binding.thread_id or "",
            "digest_turns_kept": len(binding.turns),
            "reason": reason,
            "note": "下一个问题会新建 Codex 线程；旧问答仍保留在 digest 里用于续接。",
        }


def _iso(ts: float) -> str:
    if not ts:
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:limit]


def build_service(config: dict[str, Any]) -> AdvisorService:
    """Construct a service from explicit config values (no hidden env reads)."""
    state_path = str(config.get("state_path") or "/opt/data/plugin-data/hermes-codex-advisor/state.json")
    codex_home = os.environ.get("CODEX_HOME") or str(config.get("codex_home") or "/opt/data/home/.codex")
    cwd = str(config.get("workspace") or "") or None
    return AdvisorService(
        store=StateStore(state_path),
        model=str(config.get("model") or "gpt-6-luna"),
        effort=str(config.get("effort") or "max"),
        sandbox=str(config.get("sandbox") or "read-only"),
        codex_bin=str(config.get("codex_binary") or "/opt/data/bin/codex"),
        codex_home=codex_home,
        cwd=cwd,
        timeout=float(config.get("timeout_seconds") or 300),
        digest_turns=int(config.get("digest_turns") or 6),
        max_answer_chars=int(config.get("max_answer_chars") or 12000),
    )
