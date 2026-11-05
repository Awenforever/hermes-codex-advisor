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
import unittest
from typing import Any, Optional

from advisor.service import AdvisorService, ADVISOR_PREAMBLE
from advisor.session import TurnOutcome
from advisor.state import StateStore


class FakeSession:
    """Stand-in for AdvisorSession with scripted behaviour."""

    def __init__(self, *, resume_ok: bool = True, answer: str = "答案 TEXT", error: str = ""):
        self.resume_ok = resume_ok
        self.answer = answer
        self.error = error
        self._alive = True
        self.messages: list[str] = []
        self.thread_id = ""
        self.closed = False

    # -- advisor session protocol
    def alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        self.closed = True
        self._alive = False

    def ensure_thread(self, thread_id: Optional[str]) -> tuple[str, bool]:
        if thread_id and self.resume_ok:
            self.thread_id = thread_id
            return thread_id, True
        self.thread_id = "thread-new-0001"
        return self.thread_id, False

    def ask(self, text: str, *, timeout: float = 300.0) -> TurnOutcome:
        self.messages.append(text)
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


if __name__ == "__main__":
    unittest.main()
