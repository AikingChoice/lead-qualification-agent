import json
import os
import subprocess
import sys
from pathlib import Path


def test_documented_cli_runs_across_processes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}

    def run(db_name, *args):
        completed = subprocess.run(
            [sys.executable, "-m", "rss_surveillance_agent.demo",
             "--db", str(tmp_path / db_name), *args],
            cwd=root, env=env, capture_output=True, text=True,
            check=True, timeout=15,
        )
        return completed.stdout, json.loads(completed.stdout.splitlines()[-1])

    first, counts = run("normal.sqlite3")
    assert '"decision": "review"' in first
    assert counts == {"articles": 2, "notifications": 1}
    second, counts = run("normal.sqlite3")
    assert second.count('"status": "duplicate"') == 2
    assert counts == {"articles": 2, "notifications": 1}
    filtered, counts = run("filtered.sqlite3", "--allowed-categories", "clinical")
    assert "not allowed" in filtered
    assert counts == {"articles": 2, "notifications": 0}
    failed, counts = run("timeout.sqlite3", "--simulate-timeout")
    assert failed.count('"status": "review_required"') == 2
    assert counts == {"articles": 2, "notifications": 2}
