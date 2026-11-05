"""Plugin settings → advisor config.

Kept out of ``__init__.py`` so it is importable and testable without a running host.

Two traps guarded here
----------------------
1. ``model`` / ``plugins`` / ``security`` / ``settings`` are **reserved roots** in the
   plugin settings API (``hermes_cli/plugins_state.py``): ``ctx.get_config("model")``
   raises ``ValueError`` rather than returning a value. Hence the ``advisor_*`` names.
2. A config subsystem that is missing, renamed, or broken must not take the advisor down.
   An advisor running on sane defaults is useful; a crashed advisor is not. Every read is
   individually guarded, and a failed read simply leaves the default in force.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hermes-codex-advisor.config")

#: plugin-settings key -> advisor config key
SETTING_KEYS: dict[str, str] = {
    "advisor_model": "model",
    "advisor_effort": "effort",
    "advisor_sandbox": "sandbox",
    "advisor_timeout_seconds": "timeout_seconds",
    "advisor_digest_turns": "digest_turns",
    "advisor_max_answer_chars": "max_answer_chars",
    "advisor_workspace": "workspace",
    "advisor_state_path": "state_path",
    "advisor_codex_binary": "codex_binary",
    "advisor_idle_timeout_seconds": "idle_timeout_seconds",
    "advisor_busy_wait_seconds": "busy_wait_seconds",
    "advisor_retry_on_transport_failure": "retry_on_transport_failure",
    "advisor_digest_max_chars": "digest_max_chars",
    "advisor_idle_shutdown_seconds": "idle_shutdown_seconds",
}

#: Values used when neither settings nor caller supply anything.
DEFAULTS: dict[str, Any] = {
    "model": "gpt-6-luna",
    "effort": "max",
    "sandbox": "read-only",
    "timeout_seconds": 300,
    "digest_turns": 6,
    "max_answer_chars": 12000,
    "codex_binary": "/opt/data/bin/codex",
    "idle_timeout_seconds": 90,
    "busy_wait_seconds": 5,
    "retry_on_transport_failure": True,
    "digest_max_chars": 6000,
    "idle_shutdown_seconds": 900,
    "state_path": "/opt/data/plugin-data/hermes-codex-advisor/state.json",
}


def read_config(ctx: Any) -> dict[str, Any]:
    """Read settings through the host plugin-context, tolerating any failure."""
    resolved: dict[str, Any] = {}
    for setting_key, config_key in SETTING_KEYS.items():
        try:
            value = ctx.get_config(setting_key)
        except Exception as exc:  # reserved root, API drift, malformed value
            logger.warning("codex-advisor: setting %r unreadable (%s); default in force", setting_key, exc)
            continue
        if value is not None:
            resolved[config_key] = value
    logger.info("codex-advisor: settings applied %s", sorted(resolved) or "none")
    return resolved


def effective_config(ctx: Any | None = None) -> dict[str, Any]:
    """DEFAULTS overlaid with whatever the host resolved."""
    cfg = dict(DEFAULTS)
    if ctx is not None:
        cfg.update(read_config(ctx))
    return cfg
