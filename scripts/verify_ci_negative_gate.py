from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="computeweaver-negative-gate-") as temporary:
        test_path = Path(temporary) / "test_deliberate_failure.py"
        test_path.write_text(
            "def test_deliberate_failure():\n    assert False, 'merge gate probe'\n",
            encoding="utf-8",
        )
        process = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test_path)],
            text=True,
            capture_output=True,
            check=False,
        )
    if process.returncode == 0:
        raise RuntimeError("CI negative gate failed: a broken test returned success")
    if "merge gate probe" not in process.stdout:
        raise RuntimeError("CI negative gate did not execute the deliberate failure")
    print("CI negative gate: PASS (deliberate test failure blocked the command)")


if __name__ == "__main__":
    main()
