"""pytest as a tool.

Syntax checking is necessary and demonstrably not sufficient. A model rewrote
one line of a file, changing `join(*lines)` to `join(lines)`. pylint scored the
result 9.57, `compile()` accepted it, and it died at runtime with

    TypeError: sequence item 0: expected str instance, tuple found

Nothing short of *executing* the code told the two versions apart. That is what
this plugin is for.

Two facts it produces, neither of them a judgement:

  - do the tests pass, aggregated to a handful of tokens rather than the
    thousands raw pytest output costs
  - which tests passed before a change and fail after it — a regression, which
    is the fact a caller needs before deciding to revert

It does not decide anything. It reports, and whoever asked decides.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

MAX_FAILURES = 5        # failures listed individually in a tool result
MAX_MESSAGE = 200       # chars kept from one failure message
TIMEOUT = 300           # seconds before a run is abandoned

BASELINE_FILE = "baseline.json"

# `pytest -v` prints one line per test, which is how the per-test outcome map
# is built. That map stays inside this plugin — only counts and a few failures
# are ever returned, because every byte returned is paid for out of the model's
# capacity to think.
_OUTCOME_RE = re.compile(
    r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", re.M)

# `-rfE` short summary: "FAILED test_a.py::test_one - assert 1 == 2"
_SHORT_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", re.M)

# The final "=== 1 failed, 2 passed in 0.04s ===" banner.
_SUMMARY_LINE_RE = re.compile(r"^=+.*\bin\s[\d.]+s.*=+$", re.M)
_COUNT_RE = re.compile(r"(\d+)\s+(\w+)")

FAILING = ("FAILED", "ERROR")

# pytest's exit codes. 5 is the one that reads as success and is not: no tests
# were collected, so "nothing failed" means nothing ran.
EXIT_NO_TESTS = 5
EXIT_USAGE = 4


def _abs(filename: str) -> Path:
    """The harness injects resolve_abs_path so relative paths follow the
    agent's `cd`. Fall back to the process cwd when imported standalone."""
    injected = globals().get("resolve_abs_path")
    return injected(filename) if injected else Path(filename).expanduser().resolve()


def _plugin_dir() -> Path:
    """Where this plugin keeps its own files. PLUGIN_DIR is injected."""
    return Path(globals().get("PLUGIN_DIR") or Path(__file__).parent)


def _parse(text: str) -> Dict[str, Any]:
    """Turn pytest's output into counts, per-test outcomes, and failures."""
    outcomes = {nodeid: verdict for nodeid, verdict in _OUTCOME_RE.findall(text)}

    messages = {}
    for nodeid, msg in _SHORT_RE.findall(text):
        messages[nodeid] = (msg or "").strip()[:MAX_MESSAGE]

    counts: Dict[str, int] = {}
    banners = _SUMMARY_LINE_RE.findall(text)
    if banners:
        for n, word in _COUNT_RE.findall(banners[-1]):
            counts[word.rstrip("s") if word != "s" else word] = int(n)

    failures = [{"test": nodeid, "error": messages.get(nodeid, "")}
                for nodeid, verdict in outcomes.items() if verdict in FAILING]
    # A collection error produces a short-summary line with no matching -v line,
    # so it would otherwise vanish. Those are exactly the ones worth seeing.
    for nodeid in messages:
        if nodeid not in outcomes:
            failures.append({"test": nodeid, "error": messages[nodeid]})

    return {"counts": counts, "outcomes": outcomes, "failures": failures}


def _run(target: str, k: str = "") -> Dict[str, Any]:
    """Run pytest once. Returns the parsed result or an error dict."""
    # `target or "."` goes through _abs so an empty target follows the agent's
    # `cd`, which Path.cwd() would not — the harness changes its own notion of
    # the working directory without changing the process's.
    path = _abs(target or ".")
    if not path.exists():
        return {"error": "path_not_found", "path": str(path)}

    # Run *from* the target, not at it. Invoked from elsewhere pytest emits
    # nodeids relative to its rootdir — "../../../../../tmp/…/test_a.py::test_one"
    # — which are unreadable, and long enough that pytest truncates the short
    # summary line to terminal width and drops the failure message off the end.
    # It also makes a baseline useless, since the same test gets a different key
    # depending on where the run was started from.
    where = path if path.is_dir() else path.parent
    arg = "." if path.is_dir() else path.name

    cmd = ["pytest", "-v", "--tb=line", "-rfE", "-p", "no:cacheprovider", arg]
    if k:
        cmd += ["-k", k]

    # pytest wraps the short summary at terminal width. Under capture there is
    # no terminal, so it falls back to 80 and truncates the failure message on
    # any moderately long nodeid.
    env = {**os.environ, "COLUMNS": "200"}

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=TIMEOUT,
                              cwd=str(where), env=env)
    except FileNotFoundError:
        return {"error": "pytest_not_installed", "hint": "pip install pytest"}
    except subprocess.TimeoutExpired:
        # Deliberately its own state, not a failure. A run killed by a timeout
        # loses buffered output, and reporting that as "tests failed" would be
        # a false negative that a caller might act on by reverting good code.
        return {"error": "timeout", "seconds": TIMEOUT, "path": str(path)}

    out = proc.stdout + proc.stderr
    if proc.returncode == EXIT_NO_TESTS:
        return {"error": "no_tests_collected", "path": str(path),
                "hint": "pytest found no tests here — check the path or naming."}
    if proc.returncode == EXIT_USAGE:
        return {"error": "pytest_usage_error", "detail": out[-300:]}

    result = _parse(out)
    result["path"] = str(path)
    result["exit_code"] = proc.returncode
    return result


def _load_baseline() -> Dict[str, Any]:
    f = _plugin_dir() / BASELINE_FILE
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_baseline(path: str, outcomes: Dict[str, str]) -> None:
    f = _plugin_dir() / BASELINE_FILE
    f.write_text(json.dumps({"path": path, "outcomes": outcomes}, indent=1),
                 encoding="utf-8")


def _regressions(before: Dict[str, str], after: Dict[str, str]) -> Dict[str, Any]:
    """Tests that passed before and do not now, and tests that have vanished.

    A test that was already failing is not a regression — it is the status quo,
    and treating it as one would block every change until the suite is green.
    """
    broke = [n for n, v in after.items()
             if v in FAILING and before.get(n) == "PASSED"]
    missing = [n for n, v in before.items() if v == "PASSED" and n not in after]
    fixed = [n for n, v in after.items()
             if v == "PASSED" and before.get(n) in FAILING]
    return {"regressions": broke, "disappeared": missing, "repaired": fixed}


def run_tests_tool(path: str = "", k: str = "") -> Dict[str, Any]:
    """Run pytest. Returns pass/fail counts and the first failures.
    path narrows to a file or dir, k to a name expression."""
    result = _run(path, k)
    if "error" in result:
        return result

    counts = result["counts"]
    failures = result["failures"]
    passed = counts.get("passed", 0)
    return {
        "path": result["path"],
        "passed": passed,
        "failed": counts.get("failed", 0) + counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
        "total": len(result["outcomes"]),
        "ok": result["exit_code"] == 0,
        "failures": failures[:MAX_FAILURES],
        "truncated": max(0, len(failures) - MAX_FAILURES),
        "next": "/runtests verify — compare against the recorded baseline "
                "to see whether a change broke anything that used to pass",
    }


REQUIRES = {
    "pytest": {"pip": "pytest", "fedora": "python3-pytest", "debian": "python3-pytest"},
}

# Defined only when pytest is present. Without it the command is never
# registered, so /runtests falls through to the model like any unknown word
# rather than existing in a broken state.
if shutil.which("pytest"):

    def runtests_command(ctx, args: str) -> None:
        """Run the test suite, or verify a change against a recorded baseline."""
        if args.strip().lower() in ("help", "-h", "--help"):
            print("""/runtests [path] [k=expr]        run the suite, show a summary
/runtests baseline [path]        record the current results as the baseline
/runtests verify [path]          run, then report what broke since the baseline

  path     a file or directory. Default: the working directory
  k=expr   only tests whose name matches, e.g. k=parser

`verify` reports three things against the baseline: tests that passed before
and fail now (regressions), tests that have disappeared, and tests that were
failing and now pass. A test that was already failing is not a regression.

Nothing is reverted or modified here — this reports facts, you decide.
Requires: pytest.""")
            return

        parts = args.split()
        mode = parts[0] if parts and parts[0] in ("baseline", "verify") else ""
        rest = parts[1:] if mode else parts
        target = next((t for t in rest if "=" not in t), "")
        k = next((t.split("=", 1)[1] for t in rest if t.startswith("k=")), "")

        print(f"[RunTests] pytest {target or ctx.cwd}{' -k ' + k if k else ''} …")
        result = _run(target, k)
        if "error" in result:
            print(f"[RunTests] {result['error']}: "
                  f"{result.get('hint') or result.get('detail') or result.get('path', '')}")
            return

        counts = result["counts"]
        passed, failed = counts.get("passed", 0), counts.get("failed", 0) + counts.get("error", 0)
        colour = ctx.colour("green" if failed == 0 else "red") if callable(getattr(ctx, "colour", None)) else ""
        print(f"{colour}{passed} passed, {failed} failed, "
              f"{counts.get('skipped', 0)} skipped{ctx.reset if colour else ''}")
        for f in result["failures"][:MAX_FAILURES]:
            print(f"  {f['test']}")
            if f["error"]:
                print(f"      {f['error']}")
        extra = len(result["failures"]) - MAX_FAILURES
        if extra > 0:
            print(f"  … and {extra} more")

        if mode == "baseline":
            _save_baseline(result["path"], result["outcomes"])
            print(f"[RunTests] Baseline recorded: {len(result['outcomes'])} tests.")
            return

        if mode == "verify":
            base = _load_baseline()
            if not base:
                print("[RunTests] No baseline recorded. Run /runtests baseline first.")
                return
            delta = _regressions(base.get("outcomes", {}), result["outcomes"])
            if delta["regressions"]:
                print(f"\n[RunTests] {len(delta['regressions'])} REGRESSION(S) — "
                      f"passed before, failing now:")
                for n in delta["regressions"]:
                    print(f"  {n}")
            if delta["disappeared"]:
                print(f"\n[RunTests] {len(delta['disappeared'])} test(s) that used to "
                      f"pass are no longer collected:")
                for n in delta["disappeared"]:
                    print(f"  {n}")
            if delta["repaired"]:
                print(f"\n[RunTests] {len(delta['repaired'])} test(s) now pass that "
                      f"were failing.")
            if not any(delta.values()):
                print("\n[RunTests] No change against the baseline.")


def demo() -> None:
    """Self-check for the parser — the only non-trivial logic here."""
    sample = """
test_a.py::test_one PASSED                                               [ 25%]
test_a.py::test_two FAILED                                               [ 50%]
test_b.py::test_three PASSED                                             [ 75%]
test_b.py::test_four SKIPPED                                             [100%]
=================================== FAILURES ===================================
test_a.py:9: assert 1 == 2
=========================== short test summary info ============================
FAILED test_a.py::test_two - assert 1 == 2
ERROR test_c.py - ImportError: no module named nope
=================== 1 failed, 2 passed, 1 skipped in 0.04s ====================
"""
    r = _parse(sample)
    assert r["counts"] == {"failed": 1, "passed": 2, "skipped": 1}, r["counts"]
    assert r["outcomes"]["test_a.py::test_one"] == "PASSED"
    assert r["outcomes"]["test_a.py::test_two"] == "FAILED"
    assert len(r["outcomes"]) == 4, r["outcomes"]

    tests = {f["test"] for f in r["failures"]}
    assert "test_a.py::test_two" in tests
    # A collection error has no -v line; it must survive anyway.
    assert "test_c.py" in tests, tests
    assert next(f["error"] for f in r["failures"]
                if f["test"] == "test_a.py::test_two") == "assert 1 == 2"

    # A test that was already failing is not a regression.
    before = {"a": "PASSED", "b": "FAILED", "c": "PASSED"}
    after = {"a": "FAILED", "b": "FAILED"}
    d = _regressions(before, after)
    assert d["regressions"] == ["a"], d
    assert d["disappeared"] == ["c"], d
    assert d["repaired"] == [], d

    # Nothing collected reads as exit 0 nowhere: it is its own error.
    assert _parse("")["counts"] == {}
    print("demo: all assertions passed")


if __name__ == "__main__":
    demo()
