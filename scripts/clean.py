from __future__ import annotations

import shutil
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = (
        root / ".hypothesis",
        root / ".mypy_cache",
        root / ".pytest_cache",
        root / ".ruff_cache",
        root / "apps" / "web" / "dist",
    )
    for target in targets:
        if target.exists() and target.is_dir() and root in target.parents:
            shutil.rmtree(target)
            print(f"removed regenerable directory {target.relative_to(root)}")


if __name__ == "__main__":
    main()
