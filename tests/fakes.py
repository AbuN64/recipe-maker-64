"""Fakes for unbake's public function seams. Every fake counts its calls; tests assert counts, never timing."""

from __future__ import annotations

import hashlib
import importlib.resources as resources
import json
import pickle
import threading
import time
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fixture import group as make_group
from unbake import compare, headers, infer, land, layout, native, ownership, pool, process, recipes, report, types
from unbake.contracts import (
    Config,
    Finding,
    Host,
    Json,
    LayoutMap,
    Member,
    NativeResult,
    Placement,
    Project,
    Proof,
    Recipe,
    Refusal,
    Snapshot,
    Submission,
    UnitSpec,
    Version,
)

from recipemaker import crack, draft, index, signatures, state

# (module, function) pairs this suite replaces. test_boundary.py checks them against unbake's public API table.
SEAMS = (
    (layout, "capture", "capture"),
    (layout, "unit_of", "unit_of"),
    (layout, "overlay", "overlay"),
    (layout, "unit_options", "unit_options"),
    (compare, "bind", "bind"),
    (compare, "measure", "measure"),
    (compare, "measure_many", "measure_many"),
    (compare, "run", "compare_run"),
    (ownership, "derive", "derive"),
    (land, "submit", "submit"),
    (land, "drain", "drain"),
    (process, "run", "process_run"),
    (process, "git", "git"),
    (native, "prove", "prove"),
    (infer, "pairs", "infer_pairs"),
    (infer, "correspondences", "correspondences"),
    (headers, "catalog", "catalog"),
    (headers, "sources", "sources"),
    (types, "load", "types_load"),
    (recipes, "resolve", "resolve"),
    (report, "items", "report_items"),
    (pool, "map", "map"),
)


def native_result(
    argv: Sequence[str] = ("tool",),
    exit: int | None = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> NativeResult:
    """A finished native process, for tests that fake `process.run`."""
    return NativeResult(tuple(argv), "/", exit, None, stdout, stderr, 0.0, 0.0, 0, {})


def proof(member: str, version: str, score: float, missing: Sequence[str] = ()) -> Proof:
    """A proof with the given score; exact exactly when the score is 1."""
    exact = score >= 1.0
    sha = hashlib.sha256(f"{member}:{version}".encode()).hexdigest()
    left = () if exact else (tuple(missing) or ("bytes",))
    facts: Json = {} if exact else {"bytes_differ": True, "score": score}
    if any("compile.error" in m for m in left):
        facts = {**facts, "compile_failed": True}
    return Proof(f"{member}.c", member, version, "recipe", sha, sha, sha, sha, exact, left, score, facts)


class Fakes:
    """One installed set of seam fakes over a two-version project with functions `func_a` `func_b` and data `data_d`.

    `calls` counts by "module.function"; `workers` is the fake pool width; `pool_jobs` records the length of every
    job list given to `pool.map` as (stage name, length); `scores` is the queue `compare.measure` answers from."""

    def __init__(self, root: Path, workers: int = 4) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.barrier: threading.Barrier | None = None
        self.real_index_get: Any = None
        self.workers = workers
        self.calls: Counter[str] = Counter()
        self.pool_jobs: list[tuple[str, int]] = []
        self.scores: deque[float] = deque()
        self.default_score = 0.5
        self.run_result: dict[str, Any] = {}
        self.withhold = False
        self.operation = "publish"
        self.dirty = ""
        self.compile_error = ""
        self.pairs: dict[tuple[str, str], dict[str, str]] = {}
        self.items: list[Json] = []
        self.landed: list[str] = []
        self.units: dict[str, UnitSpec] = {}
        self.split: dict[str, str] = {}
        self.permutable = False
        self.regions: list[Json] = []
        self.extras: dict[str, bytes] = {}
        self.m2c_fail = ""
        self.m2c_text = "void {name}(void) {{\n}}\n"
        self.config = self._config()
        self.snapshot = self._snapshot()
        (root / "tools").mkdir(parents=True, exist_ok=True)
        (root / "tools" / "m2c").write_text("")

    # -- the world
    def _config(self) -> Config:
        build = {k: [] for k in ("cppflags", "cflags", "asflags", "gnu_asflags")}
        project = Project(
            self.root,
            "fixture",
            "fixture",
            "Fixture",
            ("a", "b"),
            "a",
            "gcc-test",
            build,
            {},
            {"a": (), "b": ()},
            {},
            {},
            "0" * 64,
        )
        host = Host(
            self.workers,
            self.workers,
            1 << 30,
            1 << 30,
            self.root / "cache",
            1 << 30,
            self.root / "tools",
            {},
            None,
            2,
            2,
            0.8,
            2,
            ("test", "test@example.invalid"),
            {},
            "0" * 64,
        )
        return Config(project, host, "0" * 64)

    def _snapshot(self) -> Snapshot:
        places = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in ("a", "b"))
        members = {
            "func_a": Member("func_a", "function", "asm", "main", places),
            "func_b": Member("func_b", "function", "asm", "main", places),
            "data_d": Member(
                "data_d", "data", "data", "main", tuple(replace(p, section=".data", rom_end=0x1008) for p in places)
            ),
        }
        group = make_group("main", "main", tuple(members))
        mapping = LayoutMap({"main": group}, members, {}, "0" * 64, (), {})
        versions = {v: Version(v, self.root / f"{v}.z64", "0" * 64, "", "", {}, ()) for v in ("a", "b")}
        for name in ("func_a", "func_b"):
            for v in ("a", "b"):
                path = self.root / "asm" / v / f"{name}.s"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"glabel {name}\n jal callee\n")
        return Snapshot(self.config, "0" * 40, mapping, versions, {}, "0" * 64)

    def with_units(self, units: dict[str, UnitSpec]) -> None:
        """Make some members already decompiled (their source lives in these units)."""
        self.units = units
        self.snapshot = replace(self.snapshot, layout=replace(self.snapshot.layout, units=units))

    # -- installation
    def install(self, patch: pytest.MonkeyPatch) -> None:
        """Replace every seam in `SEAMS` with the matching method of this object, plus the recipemaker seams that
        reach other lanes (index) and the heavy packet and permuter steps."""
        self.real_index_get = index.get
        for module, name, method in SEAMS:
            label = f"{module.__name__.split('.')[-1]}.{name}"
            patch.setattr(module, name, self._counted(label, getattr(self, method)))
        patch.setattr(draft, "m2c_target", lambda toolchain: "mips-test")
        patch.setattr(state, "tool", lambda name, path=None: self.root / "tools" / name)
        patch.setattr(
            index,
            "get",
            lambda snapshot: SimpleNamespace(
                declarations={}, identities=(), callers={}, shapes={}, extents={}, globals={}, addresses={}
            ),
        )
        patch.setattr(index, "usage", lambda snapshot, member: {"addresses": [], "args": {}, "calls": []})
        patch.setattr(signatures, "context", lambda snapshot, version: self.root / "context.c")
        patch.setattr(signatures, "declarations", lambda snapshot, names: {})
        patch.setattr(crack, "permute", self._counted("crack.permute", lambda *a: None))
        patch.setattr(crack, "packet", self._packet)
        patch.setattr(report, "split_slot", lambda snapshot, member: self.split.get(member))
        patch.setattr(draft, "data_text", lambda snapshot, member: "unsigned int data_d[] = {\n    0x0,\n};\n")

    def _counted(self, name: str, function: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            with self.lock:
                self.calls[name] += 1
            return function(*args, **kwargs)

        return call

    # -- layout
    def capture(self, config: Config) -> Snapshot:
        return self.snapshot

    def unit_of(self, snapshot: Snapshot, member: str) -> UnitSpec | None:
        return next((u for u in snapshot.layout.units.values() if member in u.members), None)

    def overlay(self, snapshot: Snapshot, writes: Any) -> Snapshot:
        return replace(snapshot, overlays={**snapshot.overlays, **writes})

    def unit_options(self, snapshot: Snapshot, member: str, source: bytes) -> list[tuple[UnitSpec, dict[str, bytes]]]:
        self._grouped(snapshot, member)
        unit = UnitSpec(f"src/{member}.c", "c", "main", (member,), "gcc-test", {})
        return [(unit, {unit.path: source})]

    # -- compare
    def _grouped(self, snapshot: Snapshot, member: str) -> None:
        """unbake writes a unit with the member's group, and its layout schema refuses an empty one."""
        if not snapshot.layout.members[member].group:
            raise Refusal(Finding("config.schema", "'' should be non-empty", path="layout.toml:unit.2167.group"))

    def bind(self, snapshot: Snapshot, file: Path, function: str | None) -> tuple[UnitSpec, Snapshot]:
        member = function or file.stem
        self._grouped(snapshot, member)
        return UnitSpec(f".unbake/recipe/work/{member}.c", "c", "main", (member,), "gcc-test", {}), snapshot

    def recipe(self, config: Config, unit: UnitSpec, overrides: Json) -> Recipe:
        flags = tuple(overrides.get("add", ()))
        return Recipe(
            "gcc-test", (), ("-O2", *flags), (), hashlib.sha256(json.dumps(dict(overrides)).encode()).hexdigest()
        )

    resolve = recipe

    def _proofs(self, unit: UnitSpec) -> tuple[Proof, ...]:
        score = self.scores.popleft() if self.scores else self.default_score
        error = (f"compile.error: {self.compile_error}",) if self.compile_error else ()
        return tuple(proof(unit.members[0], v, 0.0 if error else score, error) for v in ("a", "b"))

    def _busy(self, wait: bool) -> None:
        """Count the measures in flight; with a barrier set, hold each until `parties` of them are in flight."""
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            if wait and self.barrier is not None:
                self.barrier.wait(timeout=5)
            else:
                time.sleep(0.01)  # give a second thread the chance to overlap
        finally:
            with self.lock:
                self.inflight -= 1

    def measure(self, snapshot: Snapshot, unit: UnitSpec, overrides: Json, out: Any) -> tuple[Proof, ...]:
        self._busy(True)
        if self.run_result:  # a test that canned what proving the candidate answers
            return tuple(Proof(**{**p, "missing": tuple(p["missing"])}) for p in self.run_result["proofs"])
        return self._proofs(unit)

    def derive(self, snapshot: Snapshot, unit: UnitSpec) -> tuple[Snapshot, UnitSpec]:
        return snapshot, unit

    def measure_many(self, snapshot: Snapshot, unit: UnitSpec, variants: Sequence[Json], out: Any = None) -> list[Any]:
        self._busy(False)
        return [self._proofs(unit) for _ in variants]

    def compare_run(self, config: Config, params: Json) -> Json:
        self.withhold = params["withhold"]
        return dict(self.run_result)

    # -- landing
    def submit(
        self,
        config: Config,
        request: Json,
        unit: UnitSpec,
        proofs: Any,
        source: bytes,
        origin: str,
        extras: Any = None,
        withhold: bool = False,
    ) -> Any:
        schema = json.loads(resources.files("unbake").joinpath("resources/schemas/submission.schema.json").read_text())
        assert origin in schema["properties"]["origin"]["enum"], f"unbake refuses the origin {origin!r}"
        if self.run_result and self.run_result.get("drain") is None:
            return None
        self.withhold = withhold
        self.landed.append(request["function"])
        self.extras = dict(extras or {})
        values: dict[str, Any] = {"operation": "publish", "member": request["function"], "function": None}
        values |= {"source_sha256": "0" * 64, "overrides": request["overrides"], "base": "0" * 40, "origin": origin}
        values |= {
            "note": "",
            "invocation": "test",
            "proofs": tuple(proofs),
            "extras": {},
            "extra_bases": {},
            "withheld": (),
            "id": f"id-{request['function']}",
        }
        return Submission(source=".unbake/inbox/x.c", **values)

    def drain(self, config: Config) -> Json:
        if self.run_result and self.run_result.get("drain") is not None:
            return dict(self.run_result["drain"])
        rows = [{"id": f"id-{m}", "member": m, "operation": self.operation, "commit": "abc1234"} for m in self.landed]
        return {"running": False, "landed": rows, "refused": []}

    # -- processes
    def process_run(self, name: str, argv: Sequence[str], cwd: Path, **kwargs: Any) -> NativeResult:
        function = argv[list(argv).index("--function") + 1]
        if self.m2c_fail:
            return native_result(argv, 1, self.m2c_fail.encode())
        body = self.m2c_text.format(name=function)
        return native_result(argv, 0, ("/* stack */\n" * ("--stack-structs" in argv) + body).encode())

    def git(self, config: Config, *args: str) -> NativeResult:
        return native_result(stdout=self.dirty.encode())

    # -- native and analysis
    def prove(self, snapshot: Snapshot, unit: UnitSpec, version: str, recipe: Recipe, work: Path) -> tuple[Proof, ...]:
        error = (f"compile.error: {self.compile_error}",) if self.compile_error else ()
        return (proof(unit.members[0], version, 0.0 if error else 0.5, error),)

    def infer_pairs(
        self, snapshot: Snapshot, directions: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, str]]:
        return {d: self.pairs.get(d, {}) for d in directions}

    def correspondences(self, snapshot: Snapshot) -> tuple[tuple[str, str, str, str], ...]:
        return ()

    def catalog(self, snapshot: Snapshot) -> dict[str, tuple[str, int, str]]:
        return {}

    def sources(self, snapshot: Snapshot, place: str = "include", suffix: str = ".h") -> list[str]:
        return []

    def types_load(self, snapshot: Snapshot) -> Json:
        return {"function": {}, "global": {}, "struct": {}}

    def report_items(self, snapshot: Snapshot, params: Json) -> list[Json]:
        return list(self.items)

    # -- the pool runs inline and records the size of every job list
    def map(self, config: Config, name: str, function: Any, items: Sequence[Any], key: Any = None) -> list[Any]:
        pickle.dumps((function, key))  # unbake's workers compute keys: a lambda or closure cannot cross
        self.pool_jobs.append((name, len(items)))
        return [function(item) for item in items]

    # -- the packet stands in for the real one, which reads ROM bytes and assembly
    def _packet(self, snapshot: Snapshot, member: str) -> Json:
        return {
            "member": member,
            "kind": snapshot.layout.members[member].kind,
            "target": {
                "a": {"asm": "" if snapshot.layout.members[member].kind == "data" else f"asm/a/{member}.s", "size": 8}
            },
            "best": {"source": f".unbake/recipe/work/{member}.c", "score": {"a": 0.5}, "errors": []},
            "diff": "- a\n+ b",
            "worst": "a",
            "regions": self.regions,
            "why": [],
            "twin": None,
            "permutable": self.permutable,
            "types": {"signature": "void f(void)", "declarations": []},
        }
