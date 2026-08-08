from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True, slots=True)
class SourceBinding:
    status: str
    repository_root: str
    commit: str | None
    tree: str | None
    clean: bool
    detached_head: bool | None
    reason: str | None

    def as_document(self, *, source_revision: str) -> dict[str, object]:
        return {
            **asdict(self),
            "source_revision": source_revision,
            "verification": {
                "repository_root_matches": self.reason != "Git worktree root does not match the release source root",
                "commit_object_exists": self.commit is not None,
                "tree_object_exists": self.tree is not None,
                "worktree_clean": self.clean,
            },
        }


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git") or "/usr/bin/git"
    return subprocess.run(  # noqa: S603 - fixed git executable and internally controlled arguments
        [executable, "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def inspect_git_source(root: Path) -> SourceBinding:
    """Verify that *root* is exactly a clean Git worktree bound to immutable objects."""

    resolved = root.resolve()
    try:
        top = _git(resolved, "rev-parse", "--show-toplevel")
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return SourceBinding("FAIL", str(resolved), None, None, False, None, f"Git unavailable: {error}")
    if top.returncode != 0:
        return SourceBinding("FAIL", str(resolved), None, None, False, None, "release source is not a Git worktree")
    try:
        observed_root = Path(top.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        observed_root = Path(top.stdout.strip())
    if observed_root != resolved:
        return SourceBinding(
            "FAIL",
            str(observed_root),
            None,
            None,
            False,
            None,
            "Git worktree root does not match the release source root",
        )

    commit_result = _git(resolved, "rev-parse", "--verify", "HEAD^{commit}")
    tree_result = _git(resolved, "rev-parse", "--verify", "HEAD^{tree}")
    commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None
    tree = tree_result.stdout.strip() if tree_result.returncode == 0 else None
    if not commit or not GIT_OBJECT_ID.fullmatch(commit):
        return SourceBinding("FAIL", str(resolved), None, None, False, None, "immutable Git commit is unavailable")
    if not tree or not GIT_OBJECT_ID.fullmatch(tree):
        return SourceBinding("FAIL", str(resolved), commit, None, False, None, "immutable Git tree is unavailable")

    status_result = _git(resolved, "status", "--porcelain=v1", "--untracked-files=all")
    if status_result.returncode != 0:
        return SourceBinding("FAIL", str(resolved), commit, tree, False, None, "Git worktree status is unavailable")
    clean = not status_result.stdout.strip()
    branch_result = _git(resolved, "symbolic-ref", "-q", "HEAD")
    detached = branch_result.returncode != 0
    return SourceBinding(
        "PASS" if clean else "FAIL",
        str(resolved),
        commit,
        tree,
        clean,
        detached,
        None if clean else "Git worktree contains tracked or untracked release-source changes",
    )


def fallback_source_revision(root: Path) -> str:
    """Return a deterministic, explicitly non-certifiable identifier for loose source."""

    digest = hashlib.sha256()
    source_paths = [path for path in (root / "Makefile", root / "pyproject.toml") if path.is_file()]
    source_paths.extend(root.glob("requirements*.lock"))
    for directory in ("apps", "config", "deploy", "migrations", "ops", "packages", "scripts", "tests"):
        candidate = root / directory
        if not candidate.is_dir():
            continue
        source_paths.extend(
            path
            for path in candidate.rglob("*")
            if path.is_file()
            and "node_modules" not in path.parts
            and "dist" not in path.parts
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    for path in sorted(set(source_paths)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return f"UNVERSIONED-SOURCE-{digest.hexdigest()[:16]}"


def source_revision(root: Path) -> tuple[str, bool, SourceBinding]:
    binding = inspect_git_source(root)
    if binding.status == "PASS" and binding.commit:
        return binding.commit, True, binding
    return fallback_source_revision(root), False, binding
