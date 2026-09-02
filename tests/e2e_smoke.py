"""End-to-end smoke test: install / dashboard / report."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _run(cmd, env):
    """Run a subprocess and print its combined output."""
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    if r.stdout:
        print(r.stdout, end="")
    if r.stderr:
        print("STDERR:", r.stderr, file=__import__("sys").stderr, end="")
    return r.returncode


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    venv_python = repo / ".venv" / "Scripts" / "python.exe"
    hermes_home = Path(os.environ.get("HERMES_CHECKER_E2E_HOME",
                                       repo / "e2e_home"))

    # Set HOME for the test
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_CHECKER_HOME"] = str(repo / "e2e_data")
    env["HERMES_CHECKER_EXPERIMENT"] = "e2e-test"
    env["PYTHONUNBUFFERED"] = "1"

    print("== doctor (before install) ==")
    _run([str(venv_python), "-m", "hermes_checker.cli", "doctor"], env)

    print("\n== install ==")
    rc = _run([str(venv_python), "-m", "hermes_checker.cli", "install"], env)
    assert rc == 0, "install failed"

    print("\n== doctor (after install) ==")
    _run([str(venv_python), "-m", "hermes_checker.cli", "doctor"], env)

    # Simulate a session by calling the collector directly
    print("\n== simulate session ==")
    sys.path.insert(0, str(repo / "src"))
    from hermes_checker.collector import HookCollector
    from hermes_checker.collector.config import CollectorConfig
    from hermes_checker.storage import Database, DatabasePaths

    db_path = repo / "e2e_data" / "hermes-checker.db"
    paths = DatabasePaths.from_path(db_path)
    db = Database(paths)
    collector = HookCollector(database=db, config=CollectorConfig(
        database_path=str(paths.database),
        experiment_label="e2e-test",
    ))

    sid = "e2e-session-1"
    collector.on_session_start(session_id=sid, profile="e2e", platform="cli")
    collector.pre_api_request(
        session_id=sid, api_request_id="a1", api_call_count=1,
        provider="opencode", model="MiniMax-m3", started_at=1.0,
        messages=[
            {"role": "system", "content": "You are a coding assistant." * 50},
            {"role": "user", "content": "Help me write a function." * 30},
        ],
    )
    collector.post_api_request(
        session_id=sid, api_request_id="a1", api_call_count=1,
        provider="opencode", model="MiniMax-m3",
        started_at=1.0, ended_at=3.0, api_duration=2.0,
        first_chunk_at=1.2,
        usage={"input_tokens": 800, "output_tokens": 150,
               "cache_read_tokens": 200, "reasoning_tokens": 30},
    )
    for n in range(3):
        collector.pre_tool_call(
            session_id=sid, tool_call_id=f"tc-{n}", tool_name="terminal",
            args={"command": f"echo {n}"}, started_at=4.0 + n,
        )
        collector.post_tool_call(
            session_id=sid, tool_call_id=f"tc-{n}", tool_name="terminal",
            result=f"line {n}\n" * 100,
            started_at=4.0 + n, ended_at=4.1 + n, duration_ms=100.0,
            status="ok",
        )

    db.close()

    print("\n== status ==")
    _run([str(venv_python), "-m", "hermes_checker.cli", "status"], env)

    print("\n== report ==")
    _run([str(venv_python), "-m", "hermes_checker.cli",
          "report", "--session", sid,
          "--pricing-file", str(repo / "config" / "pricing.example.yaml"),
          "--pricing-profile", "openrouter-2026-09"], env)

    print("\n== export json ==")
    _run([str(venv_python), "-m", "hermes_checker.cli",
          "export", "--session", sid, "--format", "json"], env)

    print("\n== pricing show ==")
    _run([str(venv_python), "-m", "hermes_checker.cli", "pricing",
          str(repo / "config" / "pricing.example.yaml"),
          "--profile", "openrouter-2026-09"], env)

    print("\nE2E OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())