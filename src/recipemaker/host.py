"""The project-file, stream, lock and git helpers Recipe Maker needs, kept local so it imports only `unbake.api`."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal, digest


def write(path: Path, content: bytes) -> bool:
    """Write atomically and only when the bytes differ (mtime kept); True when the file changed."""
    try:
        if path.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{uuid.uuid4().hex}.part")
    part.write_bytes(content)
    part.replace(path)
    return True


def clear(folder: Path) -> None:
    """Remove a work folder of ours (copies from an earlier lease must not read as this lease's edits)."""
    shutil.rmtree(folder, ignore_errors=True)


def stem(member: str) -> str:
    """One file-name segment per member: slashes become dots, a long name keeps head and digest."""
    flat = member.replace("/", ".")
    return flat if len(flat) <= 120 else f"{flat[:80]}-{digest(member)[:16]}"


def _stream(config: Config, stream: str) -> Path:
    if any(part in {"", ".", ".."} for part in stream.split("/")) or len(stream.split("/")) > 2:
        raise Refusal(Finding("store.corrupt", f"bad stream name {stream!r}"))
    return config.project.root / ".unbake" / f"{stream}.jsonl"


def append(config: Config, stream: str, row: Json) -> None:
    """Append one JSON line to .unbake/<stream>.jsonl as one O_APPEND write under an exclusive lock."""
    path = _stream(config, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    with _lock(path.with_name(path.name + ".lock")), path.open("ab") as handle:
        handle.write(line.encode())


def rows(config: Config, stream: str) -> list[Json]:
    path = _stream(config, stream)
    if not path.exists():
        return []
    found: list[Json] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if raw.strip():
            try:
                found.append(json.loads(raw))
            except ValueError as error:
                raise Refusal(Finding("store.corrupt", f"{path}:{number}: {error}", path=str(path))) from error
    return found


def listing(config: Config, folder: str) -> frozenset[str]:
    path = config.project.root / ".unbake" / folder
    return frozenset(os.listdir(path)) if path.is_dir() else frozenset()


@contextmanager
def _lock(path: Path, *, wait: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def exclusive(config: Config, name: str, *, wait: bool) -> Iterator[bool]:
    """Take the named project lock; yield False at once when not waiting and another process holds it."""
    with _lock(config.project.root / ".unbake" / f"{name}.lock", wait=wait) as held:
        yield held


def scratch(config: Config) -> Path:
    path = config.project.root / ".unbake" / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def work(config: Config) -> Iterator[Path]:
    """A fresh scratch directory inside the project's .unbake/tmp, removed on exit."""
    path = Path(tempfile.mkdtemp(dir=scratch(config)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def run(argv: list[str], cwd: Path, config: Config, *, stdin: bytes = b"", timeout: float | None = None) -> Any:
    """Run a tool with TMPDIR inside the project; the result has exit (None on timeout), stdout and stderr."""
    env = {**os.environ, "TMPDIR": str(scratch(config))}
    try:
        done = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired as late:
        return SimpleNamespace(exit=None, stdout=late.stdout or b"", stderr=late.stderr or b"")
    return SimpleNamespace(exit=done.returncode, stdout=done.stdout, stderr=done.stderr)


def git(config: Config, *args: str) -> Any:
    """Run git in the project; a non-zero exit refuses with the last stderr line."""
    done = run(["git", "-c", "core.fsmonitor=false", *args], config.project.root, config)
    if done.exit != 0:
        lines = done.stderr.decode("utf-8", errors="replace").splitlines()
        raise Refusal(Finding("native.exit", reason=lines[-1] if lines else "git failed"))
    return done


@contextmanager
def stage(name: str) -> Iterator[None]:
    """A named, timed step in unbake's stage tree."""
    with api.stage(name):
        yield
