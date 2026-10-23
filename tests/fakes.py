"""Fakes for Recipe Maker's seams onto unbake: the tree (`world`), the verdicts (`judge`), the pool, processes and the
header readers. Every fake counts its calls; tests assert counts, never timing."""

from __future__ import annotations

import hashlib
import pickle
import threading
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from unbake import api
from unbake.contracts import Config, Host, Json, Project, Proof, Snapshot, Version

from recipemaker import headers, host, index, world
from recipemaker.world import Group, Layout, Member, Placement, Unit

# (module, function, method) seams this suite replaces
SEAMS = (
    (world, "capture", "capture"),
    (world, "of", "of"),
    (world, "unit_of", "unit_of"),
    (host, "run", "process_run"),
    (host, "git", "git"),
    (headers, "catalog", "catalog"),
    (headers, "sources", "sources"),
    (api, "pool_map", "map"),
    (api, "symbols", "symbols"),
    (world, "asm_path", "asm_path"),
)


def native_result(exit: int | None = 0, stdout: bytes = b"", stderr: bytes = b"") -> SimpleNamespace:
    """A finished process as `host.run` returns it."""
    return SimpleNamespace(exit=exit, stdout=stdout, stderr=stderr)


def proof(member: str, version: str, score: float, missing: Sequence[str] = ()) -> Proof:
    """A proof with the given score; exact exactly when the score is 1."""
    exact = score >= 1.0
    sha = hashlib.sha256(f"{member}:{version}".encode()).hexdigest()
    left = () if exact else (tuple(missing) or ("bytes",))
    facts: Json = {} if exact else {"bytes_differ": True, "score": score}
    if any("compile.error" in m for m in left):
        facts = {**facts, "compile_failed": True}
    return Proof(f"{member}.c", member, version, "recipe", sha, sha, sha, sha, exact, left, score, facts)


def unit(path: str, members: Sequence[str], group: str = "main") -> Unit:
    return Unit(path, "c", group, tuple(members), "gcc-test", {})


class Fakes:
    """One installed set of seam fakes over a two-version project with functions `func_a` `func_b` and data `data_d`.

    `calls` counts by "module.function"; `pool_jobs` records the length of every job list given to `api.pool_map` as
    (stage name, length); `scores` is the queue the measuring fakes answer from."""

    def __init__(self, root: Path, workers: int = 4) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.workers = workers
        self.calls: Counter[str] = Counter()
        self.pool_jobs: list[tuple[str, int]] = []
        self.scores: deque[float] = deque()
        self.screens: deque[float] = deque()  # what `score_many` answers, after which the default score
        self.default_score = 0.5
        self.run_result: dict[str, Any] = {}
        self.operation = "publish"
        self.dirty = ""
        self.compile_error = ""
        self.items: list[Json] = []
        self.landed: list[str] = []
        self.units: dict[str, Unit] = {}
        self.split: dict[str, str] = {}
        self.regions: list[Json] = []
        self.extras: dict[str, bytes] = {}
        self.fuzzy: dict[str, dict[str, float]] = {}
        self.shapes: dict[str, tuple[str, ...]] = {}
        self.m2c_fail = ""
        self.m2c_text = "void {name}(void) {{\n}}\n"
        self.config = self._config()
        self.layout = self._layout()
        self.snapshot = self._snapshot()
        (root / "tools").mkdir(parents=True, exist_ok=True)
        (root / "tools" / "m2c").write_text("")

    # -- the world
    def _config(self) -> Config:
        project = Project(
            self.root,
            "fixture",
            "Fixture",
            "gcc-test",
            "a",
            (),
            (),
            (),
            (),
            None,
            None,
            None,
            ("a", "b"),
            {},
            {},
            "0" * 64,
        )
        host_ = Host(
            self.workers, self.workers, 1 << 30, 1 << 30, self.root / "cache", 1 << 30, self.root / "tools", {}, None,
            2, 2, 0.8, 2, ("test", "test@example.invalid"), {}, "0" * 64,
        )  # fmt: skip
        return Config(project, host_, "0" * 64)

    def _layout(self) -> Layout:
        places = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in ("a", "b"))
        data = tuple(replace(p, section=".data") for p in places)
        members = {
            "func_a": Member("func_a", "function", "src/main.c", places, False),
            "func_b": Member("func_b", "function", "src/main.c", places, False),
            "data_d": Member("data_d", "data", "src/main.c", data, False),
        }
        group = Group("src/main.c", "main", tuple(members), False, "src/main.c", "include/main.h")
        return Layout({"src/main.c": group}, members, {}, {})

    def _snapshot(self) -> Snapshot:
        versions = {v: Version(v, self.root / f"{v}.z64", "0" * 64, "", "", {}, ()) for v in ("a", "b")}
        for name in ("func_a", "func_b"):
            for v in ("a", "b"):
                path = self.root / "asm" / v / f"{name}.s"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"glabel {name}\n jal callee\n")
        return Snapshot(self.config, "0" * 40, None, versions, {}, "0" * 64)

    def with_units(self, units: Mapping[str, Unit]) -> None:
        """Make some members already decompiled (their source lives in these units)."""
        self.units = dict(units)
        landed = {m for u in units.values() for m in u.members}
        members = {n: replace(m, landed=n in landed) for n, m in self.layout.members.items()}
        groups = {**self.layout.groups, **{p: Group(p, "main", u.members, False, p) for p, u in units.items()}}
        self.layout = replace(self.layout, members=members, groups=groups, units=dict(units))

    # -- installation
    def install(self, patch: pytest.MonkeyPatch) -> None:
        """Replace every seam in `SEAMS` with the matching method of this object, plus the index."""
        self.real_index_get = index.get
        for module, name, method in SEAMS:
            label = f"{module.__name__.split('.')[-1]}.{name}"
            patch.setattr(module, name, self._counted(label, getattr(self, method)))
        patch.setattr(
            index,
            "get",
            lambda snapshot: SimpleNamespace(
                declarations={},
                identities=(),
                callers={},
                shapes=self.shapes,
                extents={},
                globals={},
                addresses={},
                uses={},
            ),
        )

    def _counted(self, name: str, function: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            with self.lock:
                self.calls[name] += 1
            return function(*args, **kwargs)

        return call

    # -- world
    def capture(self, config: Config) -> Snapshot:
        return self.snapshot

    def of(self, snapshot: Snapshot) -> Layout:
        return self.layout

    def unit_of(self, snapshot: Snapshot, member: str) -> Unit | None:
        return next((u for u in self.layout.units.values() if member in u.members), None)

    # -- processes
    def process_run(self, argv: Sequence[str], cwd: Path, config: Config, **kwargs: Any) -> SimpleNamespace:
        function = argv[list(argv).index("--function") + 1]
        if self.m2c_fail:
            return native_result(1, self.m2c_fail.encode())
        body = self.m2c_text.format(name=function)
        return native_result(0, ("/* stack */\n" * ("--stack-structs" in argv) + body).encode())

    def git(self, config: Config, *args: str) -> SimpleNamespace:
        return native_result(stdout=self.dirty.encode())

    def symbols(self, config: Config, version: str) -> dict[str, tuple[int, int | None, int | None, str]]:
        return {}

    def asm_path(self, config: Config, version: str, name: str) -> Path:
        return self.root / "asm" / version / f"{name}.s"

    # -- headers
    def catalog(self, snapshot: Snapshot) -> dict[str, tuple[str, int, str]]:
        return {}

    def sources(self, snapshot: Snapshot, place: str = "include", suffix: str = ".h") -> list[str]:
        return []

    # -- the pool runs inline and records the size of every job list
    def map(self, config: Config, name: str, function: Any, items: Sequence[Any], key: Any = None) -> list[Any]:
        pickle.dumps((function, key))  # unbake's workers compute keys: a lambda or closure cannot cross
        self.pool_jobs.append((name, len(items)))
        return [function(item) for item in items]
