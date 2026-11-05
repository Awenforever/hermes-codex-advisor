"""Tool surface for Hermes-Codex-Advisor.

Three tools, deliberately. An advisor is a *conversation*, not a toolbox: every extra
tool is another way for the agent to misuse it, and another schema burned into every
request's KV cache. Task execution already belongs to the Codex control plane / CLI.

Handlers are async and hop to a worker thread: a Codex turn can take minutes, and
blocking the gateway's event loop would stall every other tool in the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes-codex-advisor.tools")

TOOLSET = "codex-advisor"

CODEX_ASK_SCHEMA: dict[str, Any] = {
    "name": "codex_ask",
    "description": (
        "向你的私人 Codex 顾问提问（持久会话，跨轮次记得之前聊过什么）。\n"
        "用途：方案判断、难题攻坚、验收把关、方向纠偏——需要**判断力**的场景。\n"
        "不用于：批量改代码、跑测试、执行任务（那些走 terminal / codex CLI）。\n"
        "提问纪律：只给证据（现状、原始输出、约束、验收标准），不要写『我认为是 X』。\n"
        "顾问只读：它能看到仓库，但不会改任何文件。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问什么。讲清你要它做判断的点，别写成任务指令。",
            },
            "context": {
                "type": "string",
                "description": "可选：这次提问附带的新证据（命令输出、文件:行号、日志片段）。原文粘贴，不要摘要。",
            },
            "fresh": {
                "type": "boolean",
                "description": "可选：true = 丢掉当前线程上下文，开一个干净的新线程（历史会作为摘要带入）。",
            },
        },
        "required": ["question"],
    },
}

CODEX_STATUS_SCHEMA: dict[str, Any] = {
    "name": "codex_status",
    "description": (
        "查看 Codex 顾问的当前状态：绑定的线程、模型/effort、健康度、最近一轮问答、"
        "app-server 是否存活、状态文件路径。排查『顾问是不是挂了』时先看这个。\n"
        "probe=true 时会真的去 Codex 确认绑定线程还在不在——控制面调用，**不消耗 token**，"
        "额度紧张时也能用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "probe": {
                "type": "boolean",
                "description": "可选：true = 实际探测绑定线程是否仍存在（不消耗 token，约 1–3s，首次会拉起 app-server）。",
            },
        },
    },
}

CODEX_NEW_SCHEMA: dict[str, Any] = {
    "name": "codex_new",
    "description": (
        "重置顾问线程：下一个问题会开一个全新的 Codex 线程（旧问答仍作为摘要保留，不会丢上下文）。"
        "用于话题彻底切换、或线程被污染时。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "可选：为什么要重置（会记进状态）。"},
        },
    },
}


CODEX_HISTORY_SCHEMA: dict[str, Any] = {
    "name": "codex_history",
    "description": (
        "读取顾问线程在 **Codex 侧的真实历史**（不是我的本地摘要）：每轮的提问、回答、状态、错误。\n"
        "用途：① 核对顾问到底记得什么；② 审计某轮失败的真实原因；③ 本地超时但服务端已完成时取回答案。\n"
        "零 token（控制面调用）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "turns": {
                "type": "integer",
                "description": "可选：只看最近 N 轮（默认 5）。",
            },
            "max_chars": {
                "type": "integer",
                "description": "可选：回答正文的字符上限（默认 800/轮），避免把长回答灌进上下文。",
            },
        },
    },
}


def _session_key(kwargs: dict[str, Any]) -> str:
    """Stable binding key for the current Hermes conversation."""
    for key in ("session_id", "parent_session_id"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return f"session:{value.strip()}"
    chat_id = kwargs.get("chat_id")
    if isinstance(chat_id, str) and chat_id.strip():
        return f"chat:{chat_id.strip()}"
    return "session:default"


def make_handlers(get_service: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    """Build the three async handlers around a lazily-created service."""

    async def codex_ask(args: dict[str, Any], **kwargs: Any) -> str:
        question = str((args or {}).get("question") or "").strip()
        if not question:
            return json.dumps({"ok": False, "error": "question 不能为空"}, ensure_ascii=False)
        context = str((args or {}).get("context") or "")
        fresh = bool((args or {}).get("fresh"))
        key = _session_key(kwargs)
        service = get_service()
        result = await asyncio.to_thread(service.ask, key, question, extra_context=context, fresh=fresh)
        return json.dumps(result.to_payload(max_chars=service.max_answer_chars), ensure_ascii=False)

    async def codex_status(args: dict[str, Any], **kwargs: Any) -> str:
        key = _session_key(kwargs)
        service = get_service()
        payload = service.status(key)
        if (args or {}).get("probe"):
            probe, quota = await asyncio.gather(
                asyncio.to_thread(service.probe, key),
                asyncio.to_thread(service.quota),
            )
            payload["probe"] = probe
            payload["quota"] = quota
            if not probe.get("thread_ok"):
                payload["warning"] = probe.get("reason") or "绑定线程不可用"
            if quota.get("ok") and quota.get("allowed") is False:
                payload["warning"] = "Codex 额度已耗尽，下一次提问会直接返回 QUOTA_EXHAUSTED"
        return json.dumps(payload, ensure_ascii=False)

    async def codex_history(args: dict[str, Any], **kwargs: Any) -> str:
        key = _session_key(kwargs)
        service = get_service()
        limit = int((args or {}).get("turns") or 5)
        max_chars = int((args or {}).get("max_chars") or 800)
        payload = await asyncio.to_thread(service.history, key, limit=limit, max_chars=max_chars)
        return json.dumps(payload, ensure_ascii=False)

    async def codex_new(args: dict[str, Any], **kwargs: Any) -> str:
        key = _session_key(kwargs)
        reason = str((args or {}).get("reason") or "")
        service = get_service()
        return json.dumps(service.reset(key, reason=reason), ensure_ascii=False)

    return {
        "codex_ask": codex_ask,
        "codex_status": codex_status,
        "codex_new": codex_new,
        "codex_history": codex_history,
    }
