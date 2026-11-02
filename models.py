"""Data models for the local orchestrator worker."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, ConfigDict

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_RESERVED_DEVICES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or ch == "\x7f" for ch in text)


def _is_reserved_component(part: str) -> bool:
    base = part.split(".", 1)[0].lower()
    return base in _RESERVED_DEVICES


def _find_opencode() -> str:
    """Discover the native CLI without embedding a machine-specific user path."""
    configured = os.environ.get("LOCAL_WORKER_OPENCODE")
    if configured:
        return configured
    executable = shutil.which("opencode.exe") or shutil.which("opencode")
    if executable:
        return executable
    appdata = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    candidate = appdata / "npm/node_modules/@opencode/cli/bin/opencode.exe"
    return str(candidate) if candidate.is_file() else "opencode"


@dataclass(frozen=True)
class Settings:
    state_dir: Path = field(default_factory=lambda: Path(__file__).parent / ".state")
    opencode_exe: str = field(default_factory=_find_opencode)
    model: str = "lmstudio/qwen3.8-27b"
    endpoint: str = "http://127.0.0.1:1234/v1"
    max_attempts: int = 3
    max_tools: int = 30
    timeout_seconds: int = 1200
    context: int = 16384

    def __post_init__(self) -> None:
        if self.model != "lmstudio/qwen3.8-27b":
            raise ValueError(f"unsupported model: {self.model!r}")
        if self.endpoint != "http://127.0.0.1:1234/v1":
            raise ValueError(f"unsupported endpoint: {self.endpoint!r}")
        if not (1 <= self.max_attempts <= 3):
            raise ValueError("max_attempts must be between 1 and 3")
        if not (1 <= self.max_tools <= 30):
            raise ValueError("max_tools must be between 1 and 30")
        if not (1 <= self.timeout_seconds <= 1200):
            raise ValueError("timeout_seconds must be between 1 and 1200")
        if self.context != 16384:
            raise ValueError("context must be exactly 16384")

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs: dict = {}
        state_dir = os.environ.get("LOCAL_WORKER_STATE")
        if state_dir:
            kwargs["state_dir"] = Path(state_dir)
        return cls(**kwargs)


def _validate_path(p: str) -> str:
    if not p or any(ord(ch) < 32 for ch in p):
        raise ValueError(f"invalid path: {p!r}")
    norm = p.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:/", norm) or norm.startswith("//"):
        raise ValueError(f"absolute/UNC path not allowed: {p!r}")
    if any(ch in norm for ch in "*?[]<>\"|:"):
        raise ValueError(f"forbidden character in path: {p!r}")
    parts = [part for part in norm.split("/") if part]
    if len(parts) != len(norm.split("/")) or "" in norm.split("/"):
        raise ValueError(f"empty path component: {p!r}")
    if ".." in parts:
        raise ValueError(f"path traversal not allowed: {p!r}")
    for part in parts:
        if part == ".":
            raise ValueError(f"dot path component not allowed: {p!r}")
        if part != part.rstrip(". ") or part.endswith((".", " ")):
            raise ValueError(f"trailing dot/space in path component: {p!r}")
        if _is_reserved_component(part):
            raise ValueError(f"reserved device name in path: {p!r}")
    lowered = [part.lower() for part in parts]
    if ".git" in lowered or ".opencode" in lowered:
        raise ValueError(f"forbidden path component: {p!r}")
    if "opencode.jsonc" in lowered or "opencode.json" in lowered:
        raise ValueError("opencode config files are not allowed")
    if any(part.lower() == ".env" or part.lower().startswith('.env.') or part.lower()=='.ssh' for part in parts):
        raise ValueError(".env files are not allowed")
    name = parts[-1].lower()
    if name.endswith(('.pem','.key','.p12','.pfx')) or name in {'id_rsa','id_ed25519','credentials.json'}:
        raise ValueError('credential/key files are not allowed')
    if re.search(r"(secret|key)", name) and (
        name.endswith((".pem", ".key", ".p12", ".pfx")) or "secret" in name
    ):
        raise ValueError(f"secret/key file not allowed: {p!r}")
    return norm


def _validate_arg(a: str) -> None:
    if any(ord(ch) < 32 for ch in a):
        raise ValueError(f"control character in argv: {a!r}")


class Task(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,40}$")
    objective: str = Field(..., min_length=1, max_length=8000)
    allowed_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(...)
    verification_commands: list[list[str]] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    role: Literal["explore", "implement"] = "implement"

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("invalid task id")
        return v

    @field_validator("objective")
    @classmethod
    def _check_objective(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("objective must be nonempty")
        return v

    @field_validator("allowed_files")
    @classmethod
    def _check_files(cls, v: list[str]) -> list[str]:
        if len(v) > 3:
            raise ValueError("at most 3 allowed files")
        seen = set()
        for p in v:
            norm = _validate_path(p)
            low = norm.lower()
            if low in seen:
                raise ValueError(f"duplicate file: {p!r}")
            seen.add(low)
        return [_validate_path(p) for p in v]

    @field_validator("acceptance_criteria")
    @classmethod
    def _check_criteria(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("acceptance_criteria must be nonempty")
        for c in v:
            if not isinstance(c, str) or not c.strip():
                raise ValueError("each acceptance criterion must be nonblank text")
        return v

    @field_validator("verification_commands")
    @classmethod
    def _check_commands(cls, v: list[list[str]]) -> list[list[str]]:
        if len(v) > 5:
            raise ValueError("at most 5 verification commands")
        for cmd in v:
            if not cmd or any(not isinstance(a, str) or not a.strip() for a in cmd):
                raise ValueError("each command must be nonempty argv of nonblank strings")
            for a in cmd:
                _validate_arg(a)
        return v

    @field_validator("dependencies")
    @classmethod
    def _check_deps(cls, v: list[str]) -> list[str]:
        if any(not d.strip() or _has_control(d) for d in v):
            raise ValueError("invalid dependency id")
        return v


def validate_tasks(raw: list[dict]) -> list[Task]:
    if not raw:
        raise ValueError("batch must contain at least one task")
    tasks = [Task.model_validate(item) for item in raw]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    seen_files: set[str] = set()
    for t in tasks:
        for f in t.allowed_files:
            low = f.lower()
            if low in seen_files:
                raise ValueError(f"overlapping file across tasks: {f!r}")
            seen_files.add(low)
    if any(t.dependencies for t in tasks):
        raise ValueError("dependencies must be empty; put dependent tasks in later batches")
    if len(tasks) > 3:
        raise ValueError("batch size must be at most 3")
    for t in tasks:
        if t.role == "explore":
            if t.allowed_files or t.verification_commands:
                raise ValueError(f"exploration task {t.id!r} must have no files/commands")
        else:
            if not t.allowed_files or not t.verification_commands:
                raise ValueError(
                    f"implementation task {t.id!r} requires allowed_files and verification_commands"
                )
    return tasks
