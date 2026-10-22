"""Landing a declaration change set. unbake lands a candidate that replaces an open stand-in; a set that only moves
declarations has none, and its proof is the whole build: every version's ROM still equal to its base. So the set is
written, `make check` builds the ROMs through the host's slot pool (`unbake-run`, the build every proof build uses),
and the set is committed when they match; when they do not, the old bytes go back and nothing is committed."""

import sys
from collections.abc import Mapping
from pathlib import Path

from unbake.contracts import Config, Json

from recipemaker import host

TAIL = 6  # lines of make's output a failure keeps


def _restore(old: Mapping[str, bytes | None], root: Path) -> None:
    for path, data in old.items():
        if data is not None:
            host.write(root / path, data)


def land(config: Config, old: Mapping[str, bytes | None], edits: Mapping[str, bytes], message: str) -> Json:
    """Write `edits`, prove the ROMs, commit. `old` is each path's bytes when the set was planned: a file that moved
    since, or that has uncommitted changes, refuses the set. Returns {commit} or {problem}."""
    root = config.project.root
    paths = sorted(edits)
    with host.exclusive(config, "journal", wait=True):  # the lock unbake's own landings take
        moved = [p for p in paths if _read(root / p) != old.get(p)]
        dirty = host.git(config, "status", "--porcelain", "--", *paths).stdout.decode().strip()
        if moved or dirty:
            return {"problem": f"the tree moved under the set: {', '.join(moved or dirty.split())[:200]}"}
        for path in paths:
            host.write(root / path, edits[path])
        done = host.run([str(Path(sys.executable).parent / "unbake-run"), "--", "make", "-j{j}", "check"], root, config)
        if done.exit != 0:
            said = (done.stdout + done.stderr).decode("utf-8", errors="replace").splitlines()
            lines = [t.strip() for t in said if t.strip()]
            failed = next((i for i, t in enumerate(lines) if "***" in t), len(lines))  # make's first failed job
            tail = lines[max(0, failed - TAIL) : failed + 1]
            _restore(old, root)
            return {"problem": "the ROMs differ or the build failed: " + " | ".join(t.strip() for t in tail)}
        host.git(config, "add", "--", *paths)
        name, email = config.host.author
        host.git(config, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message)
        return {"commit": host.git(config, "rev-parse", "HEAD").stdout.decode().strip()}


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
