"""Run offline synthetic regressions without HOME credentials or model downloads.

Usage: python scripts/run_offline_tests.py [pytest selectors/options]
Install .[dev] into a virtualenv first. Real provider quality is a separate test.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="openhippo-offline-") as temp:
    home = Path(temp)
    (home / ".openhippo-offline-test").touch()
    (home / "tmp").mkdir()
    env = {key: value for key, value in os.environ.items()
           if not any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
           and not key.startswith(("HIPPO_", "OPENHIPPO_", "HERMES_"))}
    env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"), TMPDIR=str(home / "tmp"),
               PYTHONPATH=os.pathsep.join((str(root / "src"), str(root / "tests"))),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1",
               OPENHIPPO_DREAM_AUTO="0")
    command = [sys.executable, "-m", "pytest", "-q", "-ra", "-p", "no:cacheprovider",
               "-p", "pytest_asyncio.plugin", "-p", "offline_provider", *(sys.argv[1:] or ["tests"])]
    started = time.monotonic()
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=900)
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    output = os.environ.get("OPENHIPPO_TEST_RESULT")
    if output:
        Path(output).write_text(json.dumps({"exit_code": result.returncode,
            "elapsed_seconds": time.monotonic() - started, "stdout": result.stdout,
            "stderr": result.stderr, "provider": "deterministic synthetic", "tcp": "denied"}, indent=2))
    sys.exit(result.returncode)
