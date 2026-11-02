"""Git worktree helpers for the local orchestrator (stdlib only).

Security model: every path that crosses a trust boundary is checked as a RAW
path (and its ancestors) for symlinks/junctions BEFORE any resolve(), and then
re-checked after resolve() for containment.  Git output is decoded UTF-8 before
it is handed to Path().  No commit/reset/remove is ever issued here.
"""

from __future__ import annotations

import subprocess
import os
from pathlib import Path

_MAX_PATCH = 1024 * 1024


def _is_link(p: Path) -> bool:
    """True if p is a symlink or (Windows) junction."""
    try:
        return p.is_symlink() or p.is_junction()
    except AttributeError:
        # Python < 3.12 has no is_junction(); is_symlink covers the common case.
        return p.is_symlink()


def _reject_link(p: Path) -> None:
    if _is_link(p):
        raise ValueError(f"symlink/junction not allowed: {p}")


def _check_ancestors(raw: Path, stop: Path | None = None) -> None:
    """Reject symlinks/junctions on raw and every ancestor up to (and incl.) stop."""
    seen = set()
    cur = raw
    while True:
        if cur not in seen:
            _reject_link(cur)
            seen.add(cur)
        if stop is not None and cur == stop:
            break
        parent = cur.parent
        if parent == cur:  # reached filesystem root
            break
        cur = parent


def run_git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Run git in repo.  When input is supplied ONLY the input kwarg is passed
    (subprocess.run forbids combining stdin=PIPE with input)."""
    cmd = ["git", "-c", "core.longpaths=true", "-C", str(repo), *args]
    kwargs: dict = {
        "shell": False,
        "check": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 60,
    }
    if input_bytes is not None:
        kwargs["input"] = input_bytes
    else:
        # MCP stdin is a live protocol pipe. Git must never inherit it.
        kwargs["stdin"] = subprocess.DEVNULL
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError as e:
        raise RuntimeError("git executable not found") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git timed out: {' '.join(args[:3])}") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="replace").strip()[:200]
        raise RuntimeError(f"git failed: {detail}") from e
    return proc.stdout


def _to_path(raw: bytes, what: str) -> Path:
    """Decode git's UTF-8 name output before handing it to Path()."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"non-UTF8 {what} from git: {e}") from e
    return Path(text)


def clean_base(repo: Path) -> tuple[Path, str]:
    """Return (resolved repo root, HEAD sha).  Rejects dirty trees, submodules,
    and any symlink/junction on the requested root or its ancestors."""
    raw_root = Path(repo)
    _check_ancestors(raw_root)
    resolved = raw_root.resolve()
    try:
        toplevel = run_git(resolved, "rev-parse", "--show-toplevel")
    except RuntimeError as e:
        raise ValueError(f"not a git repository: {e}") from e
    root = _to_path(toplevel.strip(), "toplevel")
    # The requested root (before resolution) must BE the git root; reject nested.
    if resolved != root.resolve():
        raise ValueError("requested path is not the repository root")
    if not (root / ".git").exists():  # .git may be a FILE for linked worktrees
        raise ValueError("not a git repository")
    _check_ancestors(root)
    if (root / ".gitmodules").exists():
        raise ValueError("submodules are not allowed")
    stage = run_git(root, "ls-files", "--stage").decode()
    for line in stage.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0].startswith("160000"):
            raise ValueError(f"submodule entry not allowed: {fields[-1]}")
    try:
        head = run_git(root, "rev-parse", "HEAD").decode().strip()
    except RuntimeError as e:
        raise ValueError(f"no HEAD commit: {e}") from e
    status = run_git(root, "status", "--porcelain").decode()
    if status.strip():
        raise ValueError("repository is not clean")
    return root, head


def create_worktree(repo: Path, dest: Path, base: str) -> None:
    """Create a detached worktree.  The destination MUST be outside the repo and
    must not already exist; raw dest/ancestors are checked for links first."""
    resolved_repo = Path(repo).resolve()
    raw_dest = Path(dest).absolute()
    _check_ancestors(raw_dest)
    if raw_dest.exists():
        raise ValueError(f"worktree destination already exists: {raw_dest}")
    dest_resolved = raw_dest.resolve()
    try:
        toplevel = run_git(resolved_repo, "rev-parse", "--show-toplevel")
    except RuntimeError as e:
        raise ValueError(f"not a git repository: {e}") from e
    root = _to_path(toplevel.strip(), "toplevel").resolve()
    # Containment: relative_to succeeds only when dest is INSIDE the repo.
    try:
        dest_resolved.relative_to(root)
    except ValueError:
        pass  # outside -> allowed, proceed
    else:
        raise ValueError("worktree destination must be outside the repository")
    dest_resolved.parent.mkdir(parents=True, exist_ok=True)
    run_git(resolved_repo, "worktree", "add", "--detach", str(dest_resolved), base)


def _safe_rel(wt: Path, name: str) -> Path | None:
    """Validate a git-reported relative path against the worktree root.

    Returns the resolved target if safe, else None.  The RAW target and its
    ancestors are checked for symlinks/junctions BEFORE resolve(); deleted
    regular files (nonexistent targets) are allowed through to containment."""
    if not name or "\x00" in name:
        return None
    norm = name.replace("\\", "/")
    if norm.startswith("/") or (len(norm) >= 2 and norm[1] == ":"):
        return None
    parts = [p for p in norm.split("/") if p]
    if len(parts) != len(norm.split("/")) or any(p in (".", "..") for p in parts):
        return None
    raw_target = wt.joinpath(*parts)
    _check_ancestors(raw_target, stop=wt)  # existing ancestors + raw target
    resolved_wt = wt.resolve()
    try:
        target = raw_target.resolve()
        target.relative_to(resolved_wt)
    except ValueError:
        return None
    _reject_link(target)
    return target


def changed_files(wt: Path) -> list[str]:
    """Tracked changes vs HEAD plus untracked files, all validated & UTF-8."""
    _check_ancestors(Path(wt))
    wt = Path(wt).resolve()
    out: set[str] = set()
    diff_args = ["-c", "core.quotepath=false", "diff", "--no-ext-diff",
                 "--no-textconv", "--name-only", "-z", "HEAD"]
    for raw in run_git(wt, *diff_args).split(b"\0"):
        if not raw:
            continue
        name = _to_path(raw, "changed file").as_posix()
        target = _safe_rel(wt, name)
        if target is None:
            raise ValueError(f"unsafe path in diff: {name}")
        out.add(name)
    ls_args = ["-c", "core.quotepath=false", "ls-files", "--others",
               "--exclude-standard", "-z"]
    for raw in run_git(wt, *ls_args).split(b"\0"):
        if not raw:
            continue
        name = _to_path(raw, "untracked file").as_posix()
        target = _safe_rel(wt, name)
        if target is None:
            raise ValueError(f"unsafe untracked path: {name}")
        out.add(name)
    return sorted(out)


def capture_patch(wt: Path, allowed_files: list[str]) -> bytes:
    """Capture a bounded UTF-8 patch of approved changes only.

    Changed existing files are read (size/UTF-8/null-byte checked) BEFORE any
    staging; symlinks are never read; intent-to-add is used for untracked files
    only so the user's original checkout is never staged."""
    _check_ancestors(Path(wt))
    wt = Path(wt).resolve()
    changed = changed_files(wt)
    allowed_cf = {f.replace("\\", "/").casefold() for f in allowed_files}
    extra = [c for c in changed if c.casefold() not in allowed_cf]
    if extra:
        raise ValueError(f"changes outside approved files: {extra[:3]}")

    untracked: list[str] = []
    for name in changed:
        target = wt / name
        _reject_link(target)  # never read a symlink/junction
        if not target.exists():
            continue  # deleted file; binary-ness comes from the diff header
        if target.is_file() and target.stat().st_size > _MAX_PATCH:
            raise ValueError(f"changed file too large: {name}")
        try:
            data = target.read_bytes()
        except OSError as e:
            raise ValueError(f"cannot read changed file {name}: {e}") from e
        if b"\x00" in data:
            raise ValueError(f"binary (null byte) changed file: {name}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"non-UTF8 changed file: {name}: {e}") from e
        if not target.is_file():
            continue
        # Only stage files that are genuinely untracked (not in the index).
        ls = run_git(wt, "-c", "core.quotepath=false", "ls-files", "--others",
                     "--exclude-standard", "-z", "--", name)
        if any(part == name.encode("utf-8") for part in ls.split(b"\0")):
            untracked.append(name)

    if untracked:
        run_git(wt, "-c", "core.quotepath=false", "add", "--intent-to-add",
                "--", *untracked)

    diff_args = ["-c", "core.quotepath=false", "diff", "--no-ext-diff",
                 "--no-textconv", "HEAD"]
    patch = run_git(wt, *diff_args)
    if len(patch) > _MAX_PATCH:
        raise ValueError("patch too large")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"non-UTF8 changed content in patch: {e}") from e
    for line in text.splitlines():
        if line.startswith(("Binary files ", "GIT binary patch")):
            raise ValueError(f"binary file changed: {line[:60]}")
    return patch


def apply_patch(wt: Path, patch: bytes) -> None:
    """Apply a captured patch.  No --no-ext-diff flags on git apply."""
    wt = Path(wt).resolve()
    if not patch:
        return
    run_git(wt, "apply", "--check", "-", input_bytes=patch)
    run_git(wt, "apply", "-", input_bytes=patch)
