"""Codex app-server session for the advisor.

Scope of this module: **talk to one codex thread, honestly.** No policy, no persistence,
no prompt engineering — those live in ``service.py`` so each layer stays testable.

What we reuse from Hermes instead of re-inventing
-------------------------------------------------
* ``CodexAppServerClient`` — stdio JSON-RPC transport, subprocess lifecycle, stderr tail.
* ``CodexEventProjector``  — turns app-server notifications into the same
  assistant/tool projections the built-in runtime produces, so advisor answers look
  *native* rather than like a bolted-on side channel.

What we add (Hermes' built-in runtime does not do this)
-------------------------------------------------------
* ``thread/resume`` — continue a thread across process restarts. This is the single
  feature that turns "a Codex call" into "an advisor with memory".

Hard gate: model and effort are baked into the **spawn command** (``-c model=…``), so no
call site can silently drop them and land on Codex's built-in default model. That was a
real incident with the previous integration; the fix here is structural, not discipline.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes-codex-advisor.session")

# Imported lazily so the plugin can be imported (and unit-tested) outside a Hermes process.
_CLIENT_CLS: Optional[Callable[..., Any]] = None
_PROJECTOR_CLS: Optional[Callable[[], Any]] = None


def _load_hermes_transport() -> tuple[Callable[..., Any], Callable[[], Any]]:
    global _CLIENT_CLS, _PROJECTOR_CLS
    if _CLIENT_CLS is None or _PROJECTOR_CLS is None:
        from agent.transports.codex_app_server import CodexAppServerClient  # type: ignore
        from agent.transports.codex_event_projector import CodexEventProjector  # type: ignore

        _CLIENT_CLS = CodexAppServerClient
        _PROJECTOR_CLS = CodexEventProjector
    return _CLIENT_CLS, _PROJECTOR_CLS  # type: ignore[return-value]


@dataclass
class TurnOutcome:
    """Result of one advisor question."""

    ok: bool
    text: str = ""
    thread_id: str = ""
    turn_id: str = ""
    error: str = ""
    elapsed: float = 0.0
    declined_approvals: int = 0
    token_usage: dict[str, Any] = field(default_factory=dict)
    tool_iterations: int = 0


class AdvisorSession:
    """One codex app-server process bound to (at most) one advisor thread."""

    DECLINE_RESPONSES: dict[str, dict[str, Any]] = {
        "item/commandExecution/requestApproval": {"decision": "decline"},
        "item/fileChange/requestApproval": {"decision": "decline"},
        "item/permissions/requestApproval": {"decision": "decline"},
        "mcpServer/elicitation/request": {"action": "decline", "content": None, "_meta": None},
    }

    def __init__(
        self,
        *,
        codex_bin: str,
        codex_home: Optional[str],
        model: str,
        effort: str,
        sandbox: str,
        cwd: Optional[str] = None,
        idle_timeout: float = 90.0,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.model = model
        self.effort = effort
        self.sandbox = sandbox
        self.cwd = cwd
        # A live child process can still be wedged (stdio open, nothing coming back).
        # Silence longer than this counts as a hang, not slowness.
        self.idle_timeout = idle_timeout
        self._dead_reason = ""
        self._client_factory = client_factory
        self._client: Any = None
        self._projector: Any = None
        self._thread_id: Optional[str] = None
        self._resumed = False
        self._declined = 0

    # -- lifecycle --------------------------------------------------------
    def _spawn_args(self) -> list[str]:
        """Codex config overrides applied to every turn of this process (the hard gate)."""
        return [
            "-c", f'model={json.dumps(self.model)}',
            "-c", f'model_reasoning_effort={json.dumps(self.effort)}',
            "-c", f'sandbox_mode={json.dumps(self.sandbox)}',
            "-c", 'approval_policy="never"',
        ]

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        # The projector is pure (notification -> messages); it stays on even when the
        # transport is injected, otherwise an injected client would silently yield empty
        # answers. Hermes' projector is used when importable, else a minimal local one.
        self._projector = _make_projector()
        factory = self._client_factory
        if factory is None:
            factory, _projector_cls = _load_hermes_transport()
        self._client = factory(
            codex_bin=self.codex_bin,
            codex_home=self.codex_home,
            extra_args=self._spawn_args(),
        )
        self._client.initialize(
            client_name="hermes-codex-advisor",
            client_title="Hermes Codex Advisor",
            client_version="0.1.0",
        )
        return self._client

    def alive(self) -> bool:
        """True only when the child is running *and* not known to be wedged."""
        if self._dead_reason:
            return False
        try:
            return bool(self._client is not None and self._client.is_alive())
        except Exception:
            return False

    def mark_dead(self, reason: str) -> None:
        """Quarantine this session; the next call gets a fresh app-server."""
        if not self._dead_reason:
            logger.warning("advisor: session marked dead — %s", reason)
        self._dead_reason = reason
        self._thread_id = None

    @property
    def dead_reason(self) -> str:
        return self._dead_reason

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.debug("advisor: close() on client failed", exc_info=True)
            finally:
                self._client = None
                self._thread_id = None
                self._resumed = False

    # -- thread -----------------------------------------------------------
    def ensure_thread(self, thread_id: str | None) -> tuple[str, bool]:
        """Return ``(thread_id, resumed)``.

        ``resumed=True`` only when the thread is actually carrying prior context: either
        Codex confirmed the resume, or this process is already holding that very thread.
        Anything else is reported (not raised) so the caller can fall back to a fresh
        thread *with a digest* instead of silently losing continuity.
        """
        # Already holding exactly the requested thread: the context is live right here.
        # Returning False here would make the caller announce a lost thread that never
        # went missing (a real defect caught by the M1 smoke run).
        if self._thread_id and thread_id == self._thread_id:
            return self._thread_id, True

        client = self._ensure_client()
        if thread_id:
            try:
                result = client.request("thread/resume", {"threadId": thread_id}, timeout=20)
                got = self._extract_thread_id(result) or thread_id
                self._thread_id = got
                self._resumed = True
                logger.info("advisor: resumed codex thread %s", got[:8])
                return got, True
            except Exception as exc:
                logger.warning("advisor: thread/resume failed for %s: %s", thread_id[:8], exc)
                self._thread_id = None

        result = client.request("thread/start", self._thread_start_params(), timeout=20)
        got = self._extract_thread_id(result)
        if not got:
            raise AdvisorTransportError(f"thread/start returned no thread id (keys={sorted(result.keys())})")
        self._thread_id = got
        self._resumed = False
        logger.info("advisor: started codex thread %s", got[:8])
        return got, False

    def _thread_start_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.cwd:
            params["cwd"] = self.cwd
        return params

    @staticmethod
    def _extract_thread_id(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
        return str(
            thread.get("id")
            or thread.get("sessionId")
            or result.get("sessionId")
            or result.get("threadId")
            or ""
        )

    # -- turn -------------------------------------------------------------
    def ask(self, text: str, *, thread_id: str, timeout: float = 300.0, poll: float = 0.25) -> TurnOutcome:
        """Send one user message to ``thread_id`` and block until the turn terminates.

        The thread is a required argument on purpose: an earlier version resolved it
        itself with ``ensure_thread(None)``, which — once a thread was already held —
        started a *fresh* thread for every second question. The turn then ran without any
        context while the caller was told the old thread was still in use. Silent context
        loss is the one failure an advisor must never have, so the resolution lives in
        exactly one place (``ensure_thread``) and this method takes its result.
        """
        started = time.monotonic()
        outcome = TurnOutcome(ok=False)
        if not thread_id:
            outcome.error = "内部错误：ask() 需要显式 thread_id（线程解析只应由 ensure_thread 负责）"
            return outcome

        if self._client is None:
            # ensure_thread() spawns and initialises; refuse to guess here.
            outcome.error = "内部错误：ask() 在 app-server 未初始化时被调用"
            return outcome

        outcome.thread_id = thread_id
        client = self._client
        try:
            ts = client.request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
                timeout=30,
            )
        except Exception as exc:
            outcome.error = f"turn/start 失败: {_short(exc)}"
            return outcome

        turn_obj = (ts or {}).get("turn") if isinstance(ts, dict) else None
        if isinstance(turn_obj, dict):
            outcome.turn_id = str(turn_obj.get("id") or "")

        deadline = started + timeout
        final_text = ""
        last_activity = time.monotonic()
        while time.monotonic() < deadline:
            # Answer any server request first: codex blocks until we do.
            req = client.take_server_request(timeout=0)
            if req is not None:
                last_activity = time.monotonic()
                self._answer_server_request(req)
                continue

            note = client.take_notification(timeout=poll)
            if note is None:
                if self._dead_reason:
                    outcome.error = f"app-server 已失联：{self._dead_reason}"
                    break
                try:
                    child_alive = bool(client.is_alive())
                except Exception:
                    child_alive = False
                if not child_alive:
                    self.mark_dead("codex app-server 进程退出")
                    outcome.error = "app-server 进程已退出（`codex app-server` died）"
                    break
                silent_for = time.monotonic() - last_activity
                if self.idle_timeout and silent_for > self.idle_timeout:
                    self.interrupt()
                    self.mark_dead(f"{silent_for:.0f}s 无任何事件（疑似 wedged）")
                    outcome.error = (
                        f"app-server 无响应：连续 {silent_for:.0f}s 没有事件（阈值 {self.idle_timeout:.0f}s），"
                        "已发中断并把该会话隔离，下次调用将重启进程"
                    )
                    break
                continue
            last_activity = time.monotonic()

            method = str(note.get("method") or "")
            if method == "turn/completed":
                turn = ((note.get("params") or {}).get("turn") or {})
                status = str(turn.get("status") or "completed")
                err = turn.get("error")
                if err:
                    outcome.error = _format_turn_error(err, status)
                elif status not in {"completed", "interrupted"}:
                    outcome.error = f"turn 以 status={status} 结束"
                break
            if method in {"turn/aborted", "turn/cancelled", "turn/canceled"}:
                outcome.error = "turn 被中断"
                break

            if self._projector is not None:
                try:
                    projection = self._projector.project(note)
                except Exception:
                    projection = None
                if projection is not None:
                    if getattr(projection, "is_tool_iteration", False):
                        outcome.tool_iterations += 1
                    if getattr(projection, "final_text", None):
                        final_text = projection.final_text or final_text
                    for msg in getattr(projection, "messages", []) or []:
                        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("content"):
                            final_text = str(msg["content"])
        else:
            outcome.error = f"超时：{timeout:.0f}s 内未收到 turn/completed"
            self.interrupt()

        outcome.text = final_text.strip()
        outcome.elapsed = time.monotonic() - started
        outcome.declined_approvals = self._declined
        outcome.ok = not outcome.error and bool(outcome.text)
        if outcome.ok and not outcome.text:
            outcome.error = "turn 完成但没有返回文本"
            outcome.ok = False
        return outcome

    def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            self._client.notify("turn/interrupt", {})
        except Exception:
            logger.debug("advisor: interrupt failed", exc_info=True)

    # -- server requests --------------------------------------------------
    def _answer_server_request(self, req: dict[str, Any]) -> None:
        client = self._client
        if client is None:
            return
        method = str(req.get("method") or "")
        rid = req.get("id")
        canned = self.DECLINE_RESPONSES.get(method)
        if canned is None:
            try:
                client.respond_error(rid, code=-32601, message=f"Unsupported method: {method}")
            except Exception:
                logger.debug("advisor: respond_error failed", exc_info=True)
            return
        self._declined += 1
        logger.info("advisor: declined codex server request %s (advisor is read-only)", method)
        try:
            client.respond(rid, dict(canned))
        except Exception:
            logger.debug("advisor: respond failed", exc_info=True)


class _MinimalProjector:
    """Fallback projector: agent text only, used when Hermes' projector is unavailable."""

    class _Result:
        __slots__ = ("messages", "final_text", "is_tool_iteration")

        def __init__(self, messages=None, final_text=None, is_tool_iteration=False):
            self.messages = messages or []
            self.final_text = final_text
            self.is_tool_iteration = is_tool_iteration

    def project(self, notification: dict) -> Any:
        if notification.get("method") != "item/completed":
            return self._Result()
        item = (notification.get("params") or {}).get("item") or {}
        if item.get("type") != "agentMessage":
            return self._Result()
        text = item.get("text") or ""
        return self._Result(messages=[{"role": "assistant", "content": text}], final_text=text)


def _make_projector() -> Any:
    """Hermes' projector when available (native parity), otherwise a minimal one."""
    try:
        _client_cls, projector_cls = _load_hermes_transport()
        return projector_cls()
    except Exception as exc:  # pragma: no cover - only outside a Hermes install
        logger.info("advisor: Hermes projector unavailable (%s); using minimal projector", exc)
        return _MinimalProjector()


class AdvisorTransportError(RuntimeError):
    """Raised when the app-server cannot provide a thread at all."""


def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:limit]


def _format_turn_error(err: Any, status: str) -> str:
    if isinstance(err, dict):
        msg = str(err.get("message") or "")
        info = err.get("codex_error_info") or err.get("code") or ""
        suffix = f"（{info}）" if info else ""
        return f"turn status={status}: {msg}{suffix}".strip()
    return f"turn status={status}: {err}"
