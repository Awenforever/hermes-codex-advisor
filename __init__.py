"""Hermes-Codex-Advisor — a persistent Codex advisor for the Hermes agent.

Why a plugin instead of another MCP server
------------------------------------------
An advisor is used constantly and mid-reasoning, so it has to *be there*: registered
tools, a slash command, and a lifespan the host manages. That is exactly what the
plugin API offers (``register_tool`` / ``register_command`` / ``on_unload``), and it
avoids the previous arrangement — a third-party MCP server with 38 generic tools, three
local patches, and a defect that made health reports permanently degraded.

What this plugin owns
---------------------
* one durable Codex thread per Hermes conversation (see ``advisor/state.py``),
* the recovery policy when that thread dies (digest replay + explicit disclosure),
* a deliberately tiny tool surface (``codex_ask`` / ``codex_status`` / ``codex_new``).

What it borrows from Hermes
---------------------------
``agent.transports.codex_app_server`` (stdio JSON-RPC + subprocess lifecycle) and
``agent.transports.codex_event_projector`` (notification → native message projection),
so answers look like the built-in runtime's, and we inherit its battle-tested bits.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .advisor.config import effective_config
from .advisor.service import AdvisorService, build_service
from .advisor.tools import (
    CODEX_ASK_SCHEMA,
    CODEX_NEW_SCHEMA,
    CODEX_STATUS_SCHEMA,
    TOOLSET,
    make_handlers,
)

logger = logging.getLogger("hermes-codex-advisor")

_SERVICE_LOCK = threading.Lock()
_SERVICE: Optional[AdvisorService] = None


def _config(ctx: Any) -> dict[str, Any]:
    """Settings + defaults, resolved by ``advisor.config`` (never raises)."""
    return effective_config(ctx)


def _get_service(ctx: Any) -> AdvisorService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = build_service(_config(ctx))
            logger.info(
                "codex-advisor ready: model=%s effort=%s sandbox=%s state=%s",
                _SERVICE.model, _SERVICE.effort, _SERVICE.sandbox, _SERVICE.store.path,
            )
        return _SERVICE


def register(ctx) -> None:  # noqa: D401 - plugin entrypoint
    """Plugin entrypoint called once by the Hermes plugin loader."""

    def service() -> AdvisorService:
        return _get_service(ctx)

    handlers = make_handlers(service)

    ctx.register_tool(
        name="codex_ask",
        toolset=TOOLSET,
        schema=CODEX_ASK_SCHEMA,
        handler=handlers["codex_ask"],
        is_async=True,
        description="向私人 Codex 顾问提问（持久线程，跨轮次记得上下文）",
        emoji="🧠",
    )
    ctx.register_tool(
        name="codex_status",
        toolset=TOOLSET,
        schema=CODEX_STATUS_SCHEMA,
        handler=handlers["codex_status"],
        is_async=True,
        description="查看 Codex 顾问线程/模型/存活状态",
        emoji="🩺",
    )
    ctx.register_tool(
        name="codex_new",
        toolset=TOOLSET,
        schema=CODEX_NEW_SCHEMA,
        handler=handlers["codex_new"],
        is_async=True,
        description="重置 Codex 顾问线程（旧问答仍以摘要保留）",
        emoji="🔄",
    )

    def advisor_command(raw_args: str = "") -> str:
        """`/advisor` — talk to the advisor without going through the tool layer.

        Sub-commands exist because a slash command has no place to pass structured args:
        ``/advisor status`` (default), ``/advisor probe`` (zero-token liveness),
        ``/advisor new [reason]`` (rebind thread), anything else is a question.
        """
        import json

        svc = service()
        key = "command:advisor"
        text = (raw_args or "").strip()
        lowered = text.lower()

        if not text or lowered in {"status", "状态"}:
            payload = svc.status(key)
            return json.dumps(
                {
                    "thread_id": payload["thread_id"],
                    "turns_recorded": payload["turns_recorded"],
                    "health": payload["health"],
                    "model": f'{payload["model"]} / {payload["effort"]}',
                    "app_server_alive": payload["app_server_alive"],
                    "last_turn_at": payload["last_turn_at"],
                },
                ensure_ascii=False,
            )
        if lowered in {"probe", "探测"}:
            return json.dumps(svc.probe(key), ensure_ascii=False)
        if lowered.startswith("new") or lowered.startswith("reset"):
            reason = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "via /advisor new"
            return json.dumps(svc.reset(key, reason=reason), ensure_ascii=False)
        if lowered in {"help", "?", "/?"}:
            return "用法：/advisor <问题> | /advisor status | /advisor probe | /advisor new [原因]"

        result = svc.ask(key, text)
        return result.to_payload(max_chars=svc.max_answer_chars)["answer"] or result.error

    ctx.register_command(
        name="advisor",
        handler=advisor_command,
        description="向 Codex 顾问提问（/advisor <问题>；不带参数则显示状态）",
        args_hint="<question>",
    )

    def _shutdown() -> None:
        global _SERVICE
        with _SERVICE_LOCK:
            if _SERVICE is not None:
                _SERVICE.close()
                _SERVICE = None

    try:
        ctx.on_unload(_shutdown)
    except Exception:  # older API surface
        logger.debug("codex-advisor: on_unload unavailable", exc_info=True)