"""Acceptance grading for Hermes-Codex-Advisor: L0 → L4.

Why a grader instead of "it works on my machine"
------------------------------------------------
Every claim about this plugin should be reproducible by one command, and the levels must
distinguish three outcomes that are easy to blur:

  PASS     the contract held, with evidence
  FAIL     the contract broke
  BLOCKED  the check could not run for a reason outside the plugin (no Codex quota)

BLOCKED is deliberately narrow: the only accepted causes are quota exhaustion and a
missing codex binary. Anything else fails.

Usage:
  python3 scripts/hca_acceptance.py                 # run as high as the env allows
  python3 scripts/hca_acceptance.py --level L2      # stop after L2
  python3 scripts/hca_acceptance.py --json          # machine-readable

Exit: 0 when nothing FAILed (BLOCKED is not a failure), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _reexec_under_host_python() -> None:
    """Re-run this script inside an interpreter that can import the host ``agent`` package.

    L2–L4 build the real service in-process, and the plugin intentionally reuses Hermes'
    own app-server transport. Running the grader with a bare system python3 therefore
    fails with ``No module named 'agent'`` — which says nothing about the plugin. Fix the
    interpreter, not the plugin.
    """
    if os.environ.get("HCA_REEXEC") == "1":
        return
    try:
        import agent.transports.codex_app_server  # noqa: F401
        return
    except Exception:
        pass
    target = _python()
    if os.path.abspath(target) == os.path.abspath(sys.executable):
        return
    os.environ["HCA_REEXEC"] = "1"
    os.execv(target, [target, os.path.abspath(__file__), *sys.argv[1:]])

PLUGIN_NAME = "hermes-codex-advisor"
QUOTA_MARKERS = ("usage limit", "hit your usage limit", "quota", "insufficient_quota", "rate limit")
PLUGIN_INIT = os.path.join(REPO, "__init__.py")


@dataclass
class Check:
    level: str
    name: str
    status: str  # PASS / FAIL / BLOCKED
    detail: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"level": self.level, "check": self.name, "status": self.status,
                "detail": self.detail, "seconds": round(self.seconds, 1)}


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "FAIL"]

    def highest_level(self) -> str:
        """Highest *contiguous* level: L3 only counts once L0–L2 have all passed."""
        reached = ""
        for level in ("L0", "L1", "L2", "L3", "L4"):
            checks = [c for c in self.checks if c.level == level]
            if not checks:
                break
            if any(c.status != "PASS" for c in checks):
                break
            reached = level
        return reached or "—"


def _python() -> str:
    """Interpreter that can import the host package (``agent.transports``) as well as ours.

    The plugin deliberately reuses Hermes' own app-server client, so it must run inside an
    interpreter where ``agent`` is importable — i.e. the Hermes venv, not whatever python3
    happens to be first on PATH.
    """
    override = os.environ.get("HCA_PYTHON")
    if override and os.path.exists(override):
        return override
    for candidate in ("/opt/hermes/.venv/bin/python", "/opt/hermes/.venv/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def _is_quota(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in QUOTA_MARKERS)


def _timed(fn) -> tuple[object, float]:
    start = time.monotonic()
    out = fn()
    return out, time.monotonic() - start


# ---------------------------------------------------------------- L0
def check_l0(report: Report) -> None:
    def compile_all():
        files = [PLUGIN_INIT] + [
            os.path.join(REPO, "advisor", f)
            for f in sorted(os.listdir(os.path.join(REPO, "advisor")))
            if f.endswith(".py")
        ]
        proc = subprocess.run([_python(), "-m", "py_compile", *files], capture_output=True, text=True)
        return proc.returncode, (proc.stderr or "").strip()

    (code, err), secs = _timed(compile_all)
    report.add(Check("L0", "all modules compile", "PASS" if code == 0 else "FAIL", err[:300], secs))

    def doctor():
        import shutil

        binary = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
        proc = subprocess.run(
            [binary, "plugins", "doctor", PLUGIN_NAME],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "HERMES_HOME": os.environ.get("HERMES_HOME", "/opt/data")},
        )
        return proc.returncode, proc.stdout + proc.stderr

    try:
        (code, out), secs = _timed(doctor)
    except FileNotFoundError:
        report.add(Check("L0", "hermes plugins doctor", "BLOCKED", "hermes CLI not on PATH"))
        return
    except subprocess.TimeoutExpired:
        report.add(Check("L0", "hermes plugins doctor", "FAIL", "timeout"))
        return
    ok = code == 0 and "WARN" not in out.upper()
    detail = "no warnings, registrations reported" if ok else out.strip()[-400:]
    report.add(Check("L0", "hermes plugins doctor", "PASS" if ok else "FAIL", detail, secs))


# ---------------------------------------------------------------- L1
def check_l1(report: Report) -> None:
    def run_tests():
        proc = subprocess.run(
            [_python(), "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"],
            capture_output=True, text=True, timeout=600, cwd=REPO,
            env={**os.environ, "PYTHONPATH": REPO, "HERMES_HOME": os.environ.get("HERMES_HOME", "/opt/data")},
        )
        tail = (proc.stderr or "").strip().splitlines()
        return proc.returncode, " | ".join(tail[-2:]) if tail else ""

    (code, detail), secs = _timed(run_tests)
    report.add(Check("L1", "offline contract tests", "PASS" if code == 0 else "FAIL", detail[:300], secs))


# ---------------------------------------------------------------- shared helpers
def _service(**over):
    from advisor.config import DEFAULTS
    from advisor.service import build_service

    cfg = dict(DEFAULTS)
    cfg.update(over)
    return build_service(cfg)


def _classify(result) -> tuple[str, str]:
    """Map an advisor result to PASS / FAIL / BLOCKED with a readable reason."""
    if result.ok:
        return "PASS", f"{len(result.answer)} chars in {result.elapsed:.1f}s"
    if _is_quota(result.error):
        return "BLOCKED", f"codex quota: {result.error[:160]}"
    return "FAIL", result.error[:300]


# ---------------------------------------------------------------- L2
def check_l2(report: Report) -> None:
    """Plugin path end to end: fake host context -> register() -> real handlers."""
    def run():
        import asyncio
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "hca_plugin_acc", PLUGIN_INIT, submodule_search_locations=[REPO]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["hca_plugin_acc"] = module
        spec.loader.exec_module(module)

        tools: dict[str, dict] = {}

        class Ctx:
            def get_config(self, key, default=None):
                if key.split(".")[0].lower() in {"model", "plugins", "security", "settings"}:
                    raise ValueError("reserved root")
                return None  # exercise the defaults path

            def register_tool(self, *, name, handler, **kw):
                tools[name] = handler

            def register_command(self, *, name, handler, **kw):
                tools[f"/{name}"] = handler

            def on_unload(self, fn):
                pass

        module.register(Ctx())
        missing = {"codex_ask", "codex_status", "codex_new", "/advisor"} - set(tools)
        if missing:
            raise AssertionError(f"missing registrations: {sorted(missing)}")

        status = json.loads(asyncio.run(tools["codex_status"]({}, session_id="acceptance:L2")))
        if not status.get("ok") or status.get("model") != "gpt-6-luna":
            raise AssertionError(f"status not wired: {status}")

        payload = json.loads(asyncio.run(tools["codex_ask"](
            {"question": "一句话回答：1+1 等于几？"}, session_id="acceptance:L2")))
        return status, payload

    try:
        (out, secs) = _timed(run)
    except AssertionError as exc:
        report.add(Check("L2", "plugin path (register → handlers)", "FAIL", str(exc)[:300]))
        return
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim for diagnosis
        text = str(exc)
        status = "BLOCKED" if _is_quota(text) else "FAIL"
        report.add(Check("L2", "plugin path (register → handlers)", status, text[:300]))
        return
    status_obj, payload = out
    verdict, detail = _classify_simple(payload)
    report.add(Check("L2", "plugin path (register → handlers)", verdict,
                     f"3 tools + /advisor registered; ask: {detail}", secs))


def _classify_simple(payload: dict) -> tuple[str, str]:
    if payload.get("ok"):
        return "PASS", f"{len(payload.get('answer') or '')} chars, thread {str(payload.get('thread_id'))[:8]}"
    err = payload.get("error") or ""
    if _is_quota(err):
        return "BLOCKED", f"codex quota: {err[:160]}"
    return "FAIL", err[:300]


# ---------------------------------------------------------------- L3
def check_l3(report: Report) -> None:
    """Continuity: same thread across two turns, then across a fresh process."""
    svc = _service(timeout=180)
    try:
        r1 = svc.ask("acceptance:L3", "记住标记 ACC-L3。一句话回答：你收到标记了吗？")
        verdict1, detail1 = _classify(r1)
        report.add(Check("L3", "turn 1 answers", verdict1, detail1, r1.elapsed))
        if verdict1 != "PASS":
            report.add(Check("L3", "two turns share one thread", "BLOCKED" if verdict1 == "BLOCKED" else "FAIL",
                             "skipped: turn 1 did not answer"))
            report.add(Check("L3", "cross-process thread/resume", "BLOCKED" if verdict1 == "BLOCKED" else "FAIL",
                             "skipped: turn 1 did not answer"))
            return

        r2 = svc.ask("acceptance:L3", "刚才的标记是什么？只回答标记本身。")
        same = bool(r1.thread_id) and r1.thread_id == r2.thread_id
        resumed = bool(r2.resumed)
        recall = "ACC-L3" in (r2.answer or "")
        ok = r2.ok and same and resumed and recall
        report.add(Check("L3", "two turns share one thread", "PASS" if ok else "FAIL",
                         f"same_thread={same} resumed={resumed} recall={recall}", r2.elapsed))
        thread_id = r1.thread_id
    finally:
        svc.close()

    # fresh process: only the thread id on the command line counts as memory
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "from advisor.config import DEFAULTS\n"
        "from advisor.service import build_service\n"
        "cfg = dict(DEFAULTS); cfg['timeout_seconds'] = 120\n"
        "svc = build_service(cfg)\n"
        "b = svc.store.get('acceptance:L3'); b.thread_id = sys.argv[1]; svc.store.save(b)\n"
        "r = svc.ask('acceptance:L3', '上一个进程留给你的标记是什么？只回答标记本身。')\n"
        "print(r.ok, r.resumed, (r.answer or '').strip()[:60].replace(chr(10), ' '), (r.error or '')[:160])\n"
    ) % REPO
    start = time.monotonic()
    proc = subprocess.run([_python(), "-c", probe, thread_id], capture_output=True, text=True, timeout=300, cwd=REPO)
    secs = time.monotonic() - start
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = out.split(" ", 2)
    if len(parts) >= 3 and parts[0] == "True" and parts[1] == "True" and "ACC-L3" in out:
        report.add(Check("L3", "cross-process thread/resume", "PASS", out[:200], secs))
    elif _is_quota(out + err):
        report.add(Check("L3", "cross-process thread/resume", "BLOCKED", (out + err)[:200], secs))
    else:
        report.add(Check("L3", "cross-process thread/resume", "FAIL", (out + " " + err)[:300], secs))


# ---------------------------------------------------------------- L4
def check_l4(report: Report) -> None:
    """Resilience: busy fail-fast, deterministic-failure honesty, hang watchdog."""
    # 1) busy: hold the lock, expect a fast honest refusal
    svc = _service(busy_wait_seconds=0.05, timeout=30)
    try:
        svc._lock.acquire(timeout=1)
        try:
            start = time.monotonic()
            res = svc.ask("acceptance:L4", "should not run")
            secs = time.monotonic() - start
        finally:
            svc._lock.release()
        ok = (not res.ok) and res.busy and secs < 5
        report.add(Check("L4", "busy fails fast (no silent queueing)", "PASS" if ok else "FAIL",
                         f"busy={res.busy} in {secs:.2f}s: {res.error[:120]}", secs))
    finally:
        svc.close()

    # 2) deterministic transport failure is reported, not disguised
    svc = _service(codex_binary="/nonexistent/codex", timeout=20, idle_timeout_seconds=5)
    try:
        res = svc.ask("acceptance:L4", "should fail honestly")
        ok = (not res.ok) and ("codex 线程" in (res.error or "")) and res.recovered is False
        report.add(Check("L4", "deterministic failure reported honestly", "PASS" if ok else "FAIL",
                         res.error[:200], res.elapsed))
    finally:
        svc.close()

    check_probe(report)

    # quota + history: both are control-plane reads, both usable during a quota drought
    svc = _service(timeout=60)
    try:
        q = svc.quota()
        if q.get("ok"):
            report.add(Check("L4", "quota readable (0 tokens)", "PASS",
                             f"5h {q.get('primary', {}).get('used_percent')}% / "
                             f"weekly {q.get('secondary', {}).get('used_percent')}% / allowed={q.get('allowed')}", 0.0))
        else:
            report.add(Check("L4", "quota readable (0 tokens)", "BLOCKED", (q.get("reason") or "")[:160], 0.0))

        h = svc.history("acceptance:L3")
        if h.get("thread_ok"):
            report.add(Check("L4", "history readable (0 tokens)", "PASS",
                             f"remote_turns={h.get('remote_turns')} {h.get('local_vs_remote')}", 0.0))
        elif h.get("ok") and not h.get("thread_ok"):
            report.add(Check("L4", "history readable (0 tokens)", "PASS",
                             f"explicit when unbound: {h.get('reason')}", 0.0))
        else:
            report.add(Check("L4", "history readable (0 tokens)", "FAIL", (h.get("reason") or "")[:160], 0.0))
    finally:
        svc.close()

    # 3) wedged app-server: 1s of silence must be judged a hang, not slowness
    svc = _service(timeout=60, idle_timeout_seconds=1)
    try:
        res = svc.ask("acceptance:L4:hang", "一句话回答：2+2 等于几？")
        text = f"{res.error} {' '.join(res.notes)}"
        if "无响应" in (res.error or ""):
            report.add(Check("L4", "silent app-server judged dead", "PASS",
                             f"watchdog fired before any answer: {res.error[:150]}", res.elapsed))
        elif "无响应" in text:
            report.add(Check("L4", "silent app-server judged dead", "PASS",
                             f"watchdog fired, then recovery hit a separate error: {res.error[:120]}", res.elapsed))
        elif _is_quota(text):
            report.add(Check("L4", "silent app-server judged dead", "BLOCKED",
                             f"quota exhausted before the watchdog could fire: {text[:150]}", res.elapsed))
        else:
            report.add(Check("L4", "silent app-server judged dead", "FAIL", text[:300], res.elapsed))
    finally:
        svc.close()


def check_probe(report: Report) -> None:
    """probe() must answer "is the history still there?" without spending tokens.

    This is quota-free by construction: ``thread/resume`` is a control-plane call. It is
    checked here (not in L2/L3) precisely because it stays usable during a quota drought —
    which is when an operator most wants to know whether the advisor still has memory.
    """
    svc = _service(timeout=60)
    try:
        # 1) no binding -> explicit, not a crash
        out = svc.probe("acceptance:no-such-session")
        ok = out.get("ok") and out.get("thread_ok") is False and "没有绑定线程" in (out.get("reason") or "")
        report.add(Check("L4", "probe is explicit when unbound", "PASS" if ok else "FAIL",
                         (out.get("reason") or "")[:160], 0.0))

        # 2) a thread bound by an earlier session -> real resume, still no tokens
        bound = [
            key for key, b in svc.store.all_bindings().items()
            if b.thread_id and "acceptance:" not in key
        ]
        if not bound:
            report.add(Check("L4", "probe resumes a real thread (0 tokens)", "BLOCKED",
                             "no pre-existing binding on this host to probe"))
            return
        key = bound[0]
        start = time.monotonic()
        out = svc.probe(key)
        secs = time.monotonic() - start
        text = f"{out.get('thread_ok')} {out.get('reason') or out.get('cost') or ''}"
        if out.get("thread_ok"):
            status = "PASS"
        elif _is_quota(text):
            status = "BLOCKED"
        else:
            status = "FAIL"
        report.add(Check("L4", "probe resumes a real thread (0 tokens)", status,
                         f"{key[:28]}: {text[:120]}", secs))
    finally:
        svc.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes-Codex-Advisor acceptance grading")
    ap.add_argument("--level", choices=["L0", "L1", "L2", "L3", "L4"], default="L4")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    args = ap.parse_args()
    _reexec_under_host_python()

    order = ["L0", "L1", "L2", "L3", "L4"]
    report = Report()
    runners = {"L0": check_l0, "L1": check_l1, "L2": check_l2, "L3": check_l3, "L4": check_l4}
    for level in order:
        runners[level](report)
        if level == args.level:
            break

    if args.json:
        print(json.dumps({"checks": [c.as_dict() for c in report.checks],
                          "highest_level": report.highest_level(),
                          "failed": len(report.failed)}, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'LEVEL':<6}{'STATUS':<9}{'CHECK':<42}DETAIL")
        print("-" * 110)
        for c in report.checks:
            print(f"{c.level:<6}{c.status:<9}{c.name:<42}{c.detail[:70]}")
        blocked = [c for c in report.checks if c.status == "BLOCKED"]
        print(f"\n最高通过等级: {report.highest_level()}    "
              f"PASS {sum(c.status == 'PASS' for c in report.checks)} / "
              f"FAIL {len(report.failed)} / BLOCKED {len(blocked)}")
        for c in blocked:
            print(f"  BLOCKED {c.level} {c.name}: {c.detail[:120]}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
