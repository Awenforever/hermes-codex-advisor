"""Offline contract tests for Hermes-Codex-Advisor.

No Codex, no network, no subprocess: a fake session stands in for the app-server so we
can pin down the behaviour that actually matters — continuity, fail-closed recovery,
honest error reporting. This is the layer that must never regress, and it must be
provable without burning quota.

Run: python3 -m unittest discover -s tests -t . -q
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from typing import Any, Optional

from advisor.service import AdvisorService, ADVISOR_PREAMBLE
from advisor.session import TurnOutcome
from advisor.state import StateStore


class FakeSession:
    """Stand-in for AdvisorSession with scripted behaviour."""

    def __init__(self, *, resume_ok: bool = True, answer: str = "答案 TEXT", error: str = "",
                 fail_first_with: str = ""):
        self.resume_ok = resume_ok
        self.answer = answer
        self.error = error
        self.fail_first_with = fail_first_with
        self.dead_reason = ""
        self._alive = True
        self.messages: list[str] = []
        self.thread_ids_used: list[str] = []
        self.thread_id = ""
        self.closed = False

    # -- advisor session protocol
    def alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        self.closed = True
        self._alive = False

    def mark_dead(self, reason: str) -> None:
        self.dead_reason = reason
        self._alive = False

    def ensure_thread(self, thread_id: Optional[str]) -> tuple[str, bool]:
        if thread_id and self.resume_ok:
            self.thread_id = thread_id
            return thread_id, True
        self.thread_id = "thread-new-0001"
        return self.thread_id, False

    def ask(self, text: str, *, thread_id: str = "", timeout: float = 300.0) -> TurnOutcome:
        assert thread_id, "service must pass an explicit thread_id"
        self.thread_ids_used.append(thread_id)
        self.messages.append(text)
        if self.fail_first_with:
            err, self.fail_first_with = self.fail_first_with, ""
            return TurnOutcome(ok=False, error=err, thread_id=thread_id, elapsed=0.01)
        if self.error:
            return TurnOutcome(ok=False, error=self.error, thread_id=self.thread_id, elapsed=0.01)
        return TurnOutcome(ok=True, text=self.answer, thread_id=self.thread_id, turn_id="turn-1", elapsed=0.02)


def build(tmp: str, session: FakeSession, **overrides: Any) -> AdvisorService:
    cfg = dict(
        model="gpt-6-luna",
        effort="max",
        sandbox="read-only",
        codex_bin="/bin/true",
        codex_home=tmp,
        cwd=None,
        timeout=5,
        digest_turns=6,
        max_answer_chars=12000,
        state_path=os.path.join(tmp, "state.json"),
    )
    cfg.update(overrides)
    return AdvisorService(
        store=StateStore(cfg["state_path"]),
        session_factory=lambda **_: session,
        model=cfg["model"], effort=cfg["effort"], sandbox=cfg["sandbox"],
        codex_bin=cfg["codex_bin"], codex_home=cfg["codex_home"], cwd=cfg["cwd"],
        timeout=cfg["timeout"], digest_turns=cfg["digest_turns"],
        max_answer_chars=cfg["max_answer_chars"],
        busy_wait=float(cfg.get("busy_wait_seconds") or 5),
        idle_shutdown=float(cfg.get("idle_shutdown_seconds") or 900),
    )


class StateTests(unittest.TestCase):
    def test_binding_roundtrip_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.json"))
            b = store.get("session:A")
            b.thread_id = "t-1"
            b.record("问一", "答一")
            b.record("问二", "答二")
            b.record("问三", "答三")
            store.save(b)

            again = store.get("session:A")
            self.assertEqual(again.thread_id, "t-1")
            self.assertEqual([t.question for t in again.turns], ["问一", "问二", "问三"])
            digest = again.digest(2)
            self.assertIn("问三", digest)
            self.assertIn("答三", digest)
            self.assertNotIn("问一", digest)  # only the last N turns

    def test_digest_respects_the_char_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.json"))
            b = store.get("s")
            for i in range(4):
                b.record(f"问题{i}-" + "细" * 400, "答案" + "节" * 400, keep=10)
            budget = 1200
            digest = b.digest(6, max_chars=budget)
            self.assertLessEqual(len(digest), budget + 400, "budget overshoot means the replay could blow the prompt")
            self.assertIn("问题3", digest)          # newest survives
            self.assertNotIn("问题0", digest)       # oldest dropped by budget, not by turn count

    def test_digest_always_keeps_one_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.json"))
            b = store.get("s")
            b.record("巨大问题" + "长" * 5000, "巨大答案" + "长" * 5000)
            digest = b.digest(6, max_chars=500)
            self.assertIn("巨大问题", digest)
            self.assertLessEqual(len(digest), 900)

    def test_digest_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.json"))
            b = store.get("s")
            b.record("Q" * 5000, "A" * 5000, keep=3)
            self.assertLess(len(b.turns[0].question), 900)
            self.assertLess(len(b.turns[0].answer), 2100)

    def test_corrupt_state_is_quarantined_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ not json")
            store = StateStore(path)
            b = store.get("s")  # must not raise
            self.assertEqual(b.thread_id, "")
            self.assertTrue(any(n.startswith("state.json.corrupt") for n in os.listdir(tmp)))

    def test_reset_keeps_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(os.path.join(tmp, "state.json"))
            b = store.get("s")
            b.thread_id = "t-1"
            b.record("问", "答")
            store.save(b)
            store.reset("s", reason="test")
            b2 = store.get("s")
            self.assertEqual(b2.thread_id, "")
            self.assertEqual(len(b2.turns), 1)


class ServiceTests(unittest.TestCase):
    def test_first_ask_uses_new_thread_with_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            res = svc.ask("session:A", "这个方案有什么风险？")
            self.assertTrue(res.ok, res.error)
            self.assertFalse(res.resumed)
            self.assertFalse(res.restarted)
            self.assertIn(ADVISOR_PREAMBLE.strip()[:20], sess.messages[0])
            self.assertIn("这个方案有什么风险？", sess.messages[0])

    def test_second_ask_resumes_and_skips_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "第一个问题")
            res = svc.ask("session:A", "第二个问题")
            self.assertTrue(res.resumed)
            self.assertNotIn(ADVISOR_PREAMBLE.strip()[:20], sess.messages[1])
            self.assertEqual(len(sess.messages), 2)

    def test_resume_failure_is_fail_closed_with_digest_and_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "旧问题甲")
            svc.ask("session:A", "旧问题乙")

            # thread vanished server-side
            sess.resume_ok = False
            res = svc.ask("session:A", "新问题丙")
            self.assertTrue(res.ok, res.error)
            self.assertTrue(res.restarted)
            self.assertTrue(any("resume 失败" in n for n in res.notes), res.notes)
            newest = sess.messages[-1]
            self.assertIn("旧问题甲", newest)   # digest replayed
            self.assertIn("新问题丙", newest)

    def test_turn_error_is_reported_honestly_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession(error="turn status=failed: usage limit exceeded")
            svc = build(tmp, sess)
            res = svc.ask("session:A", "问一句")
            self.assertFalse(res.ok)
            self.assertIn("usage limit", res.error)
            payload = res.to_payload()
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], res.error)
            # failure still lands in the digest so the next thread knows it happened
            binding = svc.store.get("session:A")
            self.assertTrue(binding.turns[-1].error)

    def test_reset_starts_fresh_thread_next_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "问题一")
            svc.reset("session:A", reason="换话题")
            res = svc.ask("session:A", "问题二")
            self.assertFalse(res.resumed)
            self.assertEqual(res.thread_id, "thread-new-0001")

    def test_status_reports_binding_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "问一句")
            st = svc.status("session:A")
            self.assertTrue(st["ok"])
            self.assertEqual(st["model"], "gpt-6-luna")
            self.assertEqual(st["effort"], "max")
            self.assertTrue(st["has_thread"])
            self.assertEqual(st["turns_recorded"], 1)

    def test_empty_question_rejected_without_touching_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            res = svc.ask("session:A", "   ")
            self.assertFalse(res.ok)
            self.assertEqual(sess.messages, [])


class FakeClient:
    """Minimal stand-in for CodexAppServerClient."""

    def __init__(self, *, resume_ids=("t-live",), start_id="t-new"):
        self.resume_ids = set(resume_ids)
        self.start_id = start_id
        self.calls: list[tuple[str, dict]] = []
        self.notifications: list[dict] = []
        self.server_requests: list[dict] = []

    def initialize(self, **kw):
        self.calls.append(("initialize", kw))

    def is_alive(self):
        return True

    def request(self, method, params=None, timeout=None):
        self.calls.append((method, params or {}))
        if method == "thread/resume":
            tid = (params or {}).get("threadId")
            if tid in self.resume_ids:
                return {"thread": {"id": tid}}
            raise RuntimeError("thread not found")
        if method == "thread/start":
            return {"thread": {"id": self.start_id}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    def notify(self, method, params=None):
        self.calls.append((method, params or {}))

    def respond(self, rid, payload):
        self.calls.append(("respond", {"id": rid, "payload": payload}))

    def respond_error(self, rid, code=0, message=""):
        self.calls.append(("respond_error", {"id": rid, "code": code, "message": message}))

    def take_notification(self, timeout=None):
        return self.notifications.pop(0) if self.notifications else None

    def take_server_request(self, timeout=None):
        return self.server_requests.pop(0) if self.server_requests else None

    def close(self):
        pass


def make_session(client: FakeClient):
    from advisor.session import AdvisorSession

    return AdvisorSession(
        codex_bin="/bin/true", codex_home=None, model="gpt-6-luna", effort="max",
        sandbox="read-only", client_factory=lambda **_: client,
    )


class SessionThreadTests(unittest.TestCase):
    """ensure_thread must report continuity truthfully."""

    def test_holding_the_same_thread_reports_resumed(self) -> None:
        client = FakeClient()
        sess = make_session(client)
        tid, resumed = sess.ensure_thread("t-live")     # real resume
        self.assertEqual(tid, "t-live")
        self.assertTrue(resumed)
        again_id, again_resumed = sess.ensure_thread("t-live")  # already in hand
        self.assertEqual(again_id, "t-live")
        self.assertTrue(again_resumed, "same live thread must report resumed=True, not a phantom restart")
        self.assertEqual([c[0] for c in client.calls].count("thread/resume"), 1)

    def test_unresumable_thread_falls_back_to_new(self) -> None:
        client = FakeClient(resume_ids=())
        sess = make_session(client)
        tid, resumed = sess.ensure_thread("t-gone")
        self.assertEqual(tid, "t-new")
        self.assertFalse(resumed)

    def test_no_requested_thread_starts_new_even_if_one_held(self) -> None:
        client = FakeClient()
        sess = make_session(client)
        sess.ensure_thread("t-live")
        tid, resumed = sess.ensure_thread(None)   # binding was reset -> fresh thread
        self.assertEqual(tid, "t-new")
        self.assertFalse(resumed)

    def test_approval_requests_are_declined_not_ignored(self) -> None:
        client = FakeClient()
        client.notifications = [{"method": "turn/completed", "params": {"turn": {"status": "completed"}}}]
        client.server_requests = [{"id": 7, "method": "item/commandExecution/requestApproval"}]
        sess = make_session(client)
        tid, _ = sess.ensure_thread(None)
        outcome = sess.ask("hi", thread_id=tid, timeout=5)
        self.assertFalse(outcome.ok)  # no text came back, and we say so
        responses = [c for c in client.calls if c[0] == "respond"]
        self.assertEqual(responses[0][1]["payload"], {"decision": "decline"})

    def test_ask_requires_an_explicit_thread(self) -> None:
        sess = make_session(FakeClient())
        outcome = sess.ask("hi", thread_id="", timeout=1)
        self.assertFalse(outcome.ok)
        self.assertIn("thread_id", outcome.error)

    def test_two_turns_share_one_thread_and_start_it_once(self) -> None:
        """Regression: turn 2 used to be sent to a brand-new thread (silent context loss)."""
        client = FakeClient()
        client.notifications = [
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "答一"}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "答二"}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ]
        sess = make_session(client)
        tid, _ = sess.ensure_thread("t-live")
        first = sess.ask("第一问", thread_id=tid, timeout=5)
        tid2, resumed2 = sess.ensure_thread("t-live")
        second = sess.ask("第二问", thread_id=tid2, timeout=5)

        self.assertEqual(first.text, "答一")
        self.assertEqual(second.text, "答二")
        self.assertTrue(resumed2)
        self.assertEqual(first.thread_id, second.thread_id)
        methods = [c[0] for c in client.calls]
        self.assertEqual(methods.count("thread/start"), 0, "an existing thread must never be re-started")
        turn_calls = [c for c in client.calls if c[0] == "turn/start"]
        self.assertEqual([c[1]["threadId"] for c in turn_calls], ["t-live", "t-live"])


class SessionIsolationTests(unittest.TestCase):
    """Two Hermes conversations must never share an advisor thread."""

    def test_two_sessions_get_two_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "A 的第一问")
            a_thread = svc.store.get("session:A").thread_id

            # second conversation starts from scratch: no binding -> new thread expected
            class SecondSession(FakeSession):
                def ensure_thread(self, thread_id):
                    if thread_id:
                        return super().ensure_thread(thread_id)
                    self.thread_id = "thread-B"
                    return self.thread_id, False

            svc2 = build(tmp, SecondSession())
            svc2.ask("session:B", "B 的第一问")
            b_thread = svc2.store.get("session:B").thread_id
            self.assertNotEqual(a_thread, b_thread)
            # and A's binding is untouched by B's traffic (they share one state file)
            self.assertEqual(svc2.store.get("session:A").thread_id, a_thread)
            self.assertEqual(len(svc2.store.get("session:B").turns), 1)


class ThreadProbeTests(unittest.TestCase):
    """probe(): answer "is my history still there?" without spending tokens."""

    def test_probe_reports_live_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "问一句")           # binds a thread
            out = svc.probe("session:A")
            self.assertTrue(out["ok"])
            self.assertTrue(out["thread_ok"])
            self.assertEqual(out["thread_id"], sess.thread_id)
            self.assertIn("no tokens", out["cost"])
            self.assertEqual(len(sess.messages), 1, "probe must not run a turn")

    def test_probe_reports_missing_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess)
            svc.ask("session:A", "问一句")
            sess.resume_ok = False
            out = svc.probe("session:A")
            self.assertFalse(out["thread_ok"])
            self.assertIn("digest", out["reason"])

    def test_probe_without_binding_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc = build(tmp, FakeSession())
            out = svc.probe("session:none")
            self.assertTrue(out["ok"])
            self.assertFalse(out["thread_ok"])
            self.assertIn("没有绑定线程", out["reason"])


class IdleShutdownTests(unittest.TestCase):
    """An app-server kept forever is a process nobody needs."""

    def test_idle_session_is_closed_and_thread_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess, idle_shutdown_seconds=60)
            svc.ask("session:A", "问一句")
            self.assertIs(svc._session, sess)
            svc._last_used = time.monotonic() - 120     # pretend two minutes of silence
            st = svc.status("session:A")
            self.assertIsNone(svc._session, "idle app-server should be closed")
            self.assertTrue(st["has_thread"], "binding must survive reaping")
            self.assertTrue(sess.closed)

    def test_status_never_reaps_under_a_running_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess, idle_shutdown_seconds=1)
            svc.ask("session:A", "问一句")
            svc._last_used = time.monotonic() - 120        # would normally be reaped
            self.assertTrue(svc._lock.acquire(timeout=1))   # simulate an in-flight turn
            try:
                svc.status("session:A")
            finally:
                svc._lock.release()
            self.assertIs(svc._session, sess, "closing the app-server under a live turn would kill it")
            self.assertFalse(sess.closed)

    def test_recent_session_is_not_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess, idle_shutdown_seconds=600)
            svc.ask("session:A", "问一句")
            svc.status("session:A")
            self.assertIs(svc._session, sess)
            self.assertFalse(sess.closed)


class ResilienceTests(unittest.TestCase):
    """M2: busy fail-fast, transport recovery, health verdict."""

    def test_busy_advisor_fails_fast_instead_of_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession()
            svc = build(tmp, sess, busy_wait=0.05)
            self.assertTrue(svc._lock.acquire(timeout=1))
            try:
                res = svc.ask("session:A", "问一句")
            finally:
                svc._lock.release()
            self.assertFalse(res.ok)
            self.assertTrue(res.busy)
            self.assertIn("正忙", res.error)
            self.assertEqual(sess.messages, [])  # nothing ran while busy

    def test_transport_failure_recovers_once_with_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            made: list[FakeSession] = []

            def factory(**kw):
                first = not made
                s = FakeSession(fail_first_with="app-server 无响应：连续 90s 没有事件" if first else "")
                made.append(s)
                return s

            svc = AdvisorService(
                store=StateStore(os.path.join(tmp, "state.json")),
                session_factory=factory,
                model="gpt-6-luna", effort="max", sandbox="read-only",
                codex_bin="/bin/true", codex_home=tmp, cwd=None,
                timeout=5, digest_turns=6, max_answer_chars=12000,
            )
            b = svc.store.get("session:A")
            b.record("更早的问题", "更早的答案")
            svc.store.save(b)

            res = svc.ask("session:A", "新问题")
            self.assertTrue(res.ok, res.error)
            self.assertTrue(res.recovered)
            self.assertEqual(len(made), 2, "must quarantine the wedged app-server and start a fresh one")
            self.assertIn("传输层失败", " ".join(res.notes))
            retry_msg = made[1].messages[0]
            self.assertIn("更早的问题", retry_msg)   # digest replayed into the fresh thread
            self.assertIn("新问题", retry_msg)

    def test_health_declines_with_failures_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = FakeSession(error="turn status=failed: usage limit exceeded")
            svc = build(tmp, sess)
            for _ in range(3):
                svc.ask("session:A", "问")
            st = svc.status("session:A")
            self.assertEqual(st["health"], "failing")
            self.assertEqual(st["consecutive_failures"], 3)
            self.assertIn("usage limit", st["last_error"])

            sess.error = ""
            svc.ask("session:A", "问")
            st2 = svc.status("session:A")
            self.assertEqual(st2["health"], "ok")
            self.assertEqual(st2["consecutive_failures"], 0)
            self.assertTrue(st2["last_success_at"])


class HangDetectionTests(unittest.TestCase):
    def test_silent_app_server_is_declared_dead_not_waited_on(self) -> None:
        from advisor.session import AdvisorSession

        client = FakeClient()
        client.is_alive = lambda: True          # process looks alive...
        # ...but no notification ever arrives: that is a wedge, not slowness.
        sess = AdvisorSession(
            codex_bin="/bin/true", codex_home=None, model="gpt-6-luna", effort="max",
            sandbox="read-only", client_factory=lambda **_: client, idle_timeout=0.2,
        )
        tid, _ = sess.ensure_thread(None)
        outcome = sess.ask("hi", thread_id=tid, timeout=5)
        self.assertFalse(outcome.ok)
        self.assertIn("无响应", outcome.error)
        self.assertFalse(sess.alive(), "a wedged session must be quarantined, not reused")
        self.assertIn("wedged", sess.dead_reason)
        self.assertIn("turn/interrupt", [c[0] for c in client.calls])

    def test_dead_child_is_reported_as_exited(self) -> None:
        from advisor.session import AdvisorSession

        client = FakeClient()
        client.is_alive = lambda: False
        sess = AdvisorSession(
            codex_bin="/bin/true", codex_home=None, model="gpt-6-luna", effort="max",
            sandbox="read-only", client_factory=lambda **_: client, idle_timeout=90,
        )
        tid, _ = sess.ensure_thread(None)
        outcome = sess.ask("hi", thread_id=tid, timeout=5)
        self.assertFalse(outcome.ok)
        self.assertIn("退出", outcome.error)
        self.assertFalse(sess.alive())


class PluginConfigTests(unittest.TestCase):
    """The config reader must degrade to defaults, never take the advisor down."""

    def test_reserved_key_rejection_does_not_crash(self) -> None:
        from advisor.config import effective_config

        class HostileCtx:
            """Mimics the host: reserved roots raise, real keys resolve."""

            def get_config(self, key, default=None):
                if key in {"model", "plugins", "security", "settings"}:
                    raise ValueError("global, cross-plugin, and traversal paths are forbidden")
                return {"advisor_model": "gpt-6-luna", "advisor_effort": "max"}.get(key)

        cfg = effective_config(HostileCtx())
        self.assertEqual(cfg["model"], "gpt-6-luna")
        self.assertEqual(cfg["effort"], "max")
        self.assertEqual(cfg["sandbox"], "read-only")  # default survives

    def test_all_keys_unreadable_falls_back_to_defaults(self) -> None:
        from advisor.config import DEFAULTS, effective_config

        class BrokenCtx:
            def get_config(self, key, default=None):
                raise RuntimeError("config subsystem down")

        self.assertEqual(effective_config(BrokenCtx()), DEFAULTS)

    def test_no_ctx_still_yields_defaults(self) -> None:
        from advisor.config import DEFAULTS, effective_config

        self.assertEqual(effective_config(None), DEFAULTS)


if __name__ == "__main__":
    unittest.main()
