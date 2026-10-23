"""The source tree as Recipe Maker reads it: members, the module that owns each, and its placements per version.
Built from `api.snapshot` (the captured tree and its symbol facts); nothing here reads unbake's internals."""

import dataclasses
import mmap
import posixpath
import re
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from unbake import api
from unbake.contracts import Config, Json, Snapshot, Version, digest


@dataclass(frozen=True)
class Placement:
    version: str
    section: str  # ".text" | ".rodata" | ".data" | ".bss"
    rom_start: int  # bss: rom_start == rom_end
    rom_end: int
    vram: int
    bss_size: int = 0

    @property
    def size(self) -> int:
        return self.bss_size if self.section == ".bss" else self.rom_end - self.rom_start


@dataclass(frozen=True)
class Member:
    name: str
    kind: str  # "function" | "data"
    group: str  # the path of the module that owns it
    placements: tuple[Placement, ...]  # sorted by version
    landed: bool = False  # defined as C (not behind a stand-in, not fuzzy) in every version that holds it
    standing: tuple[str, ...] = ()  # the versions where it is still a stand-in or fuzzy: where work on it is live

    def holders(self) -> tuple[str, ...]:
        return tuple(sorted({p.version for p in self.placements}))

    def reference(self, preferred: str) -> str:
        return preferred if preferred in self.holders() else self.holders()[0]


@dataclass(frozen=True)
class Group:
    """One module: a source file and the names it owns."""

    name: str  # the module path
    segment: str
    members: tuple[str, ...]  # file order in the reference version, then the others
    sdk: bool  # inside a configured library directory
    source: str
    header: str = ""  # the include/ header named after the module, "" when there is none
    options: Json = field(default_factory=dict)


@dataclass(frozen=True)
class Unit:
    """What a module compiles as: Recipe Maker's view of a decompiled file."""

    path: str
    kind: str  # always "c": every module of the tree is a C source
    group: str
    members: tuple[str, ...]
    toolchain: str
    options: Json


@dataclass(frozen=True)
class Layout:
    groups: Mapping[str, Group]
    members: Mapping[str, Member]
    units: Mapping[str, Unit]  # by path: the modules that define at least one member as C
    fuzzy: Mapping[str, str]  # member with a NON_MATCHING body -> its module path

    def held(self, names: list[str]) -> set[str]:
        return {v for n in names for v in self.members[n].holders()}


def capture(config: Config) -> Snapshot:
    """The working tree now."""
    return api.snapshot(config)


def overlay(snapshot: Snapshot, writes: Mapping[str, bytes | None]) -> Snapshot:
    """The snapshot with proposed writes laid over it; its tree is captured again only when a source changes."""
    if not writes:
        return snapshot
    merged = {**snapshot.overlays, **writes}
    sources = any(p.startswith("src/") for p in writes)
    files = api.capture(snapshot.config, merged) if sources else snapshot.files
    key = digest((snapshot.digest, sorted((p, None if b is None else digest(b)) for p, b in writes.items())))
    return dataclasses.replace(snapshot, files=files, overlays=merged, digest=key, reads={})


_INCLUDE = re.compile(rb'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)


def _header(snapshot: Snapshot, path: str) -> str:
    return header_of(path, snapshot.peek(path) or b"")


def header_of(path: str, text: bytes) -> str:
    """The quoted include of a module whose file stem is the module's own, as an include/ path."""
    stem = posixpath.splitext(posixpath.basename(path))[0].lower()
    for name in _INCLUDE.findall(text):
        spelled = name.decode(errors="replace")
        if posixpath.splitext(posixpath.basename(spelled))[0].lower() == stem:
            return posixpath.normpath(posixpath.join("include", spelled))
    return ""


def _library(config: Config, path: str) -> bool:
    return any(Path(path).is_relative_to(folder) for folder in config.project.libraries)


def scores(snapshot: Snapshot, name: str) -> dict[str, float]:
    """The cached fuzzy score of a member per version, {} for a name with no NON_MATCHING body."""
    if name not in of(snapshot).fuzzy:
        return {}
    return api.memo(("recipe.score", snapshot.digest, name), lambda: dict(api.score(snapshot.config, name)))


def _build(snapshot: Snapshot) -> Layout:
    tree, config = snapshot.files, snapshot.config
    names_from = config.project.names_from
    tables = tree.symbols
    members: dict[str, Member] = {}
    groups: dict[str, Group] = {}
    units: dict[str, Unit] = {}
    fuzzy: dict[str, str] = {}
    ends = {v: extents(table) for v, table in tables.items()}
    for module in tree.modules:
        order: list[str] = []
        versions = sorted(module.owns, key=lambda v: (v != names_from, v))
        for version in versions:
            order += [n for n in module.owns[version] if n not in order]
        stand = {v: set(module.stand_ins.get(v, ())) for v in module.owns}
        for name in order:
            places = []
            for version in sorted(module.owns):
                if name not in module.owns[version] or name not in tables.get(version, {}):
                    continue
                vram, rom, size, kind = tables[version][name]
                section = module.sections.get(version, {}).get(name) or (".text" if kind == "func" else ".data")
                start, extent = rom or 0, size or ends[version].get(name, 0)
                bss = section == ".bss"
                places.append(Placement(version, section, start, start if bss else start + extent, vram, extent * bss))
            if not places:
                continue
            kind = "function" if tables[places[0].version][name][3] == "func" else "data"
            landed = name not in module.fuzzy and not any(name in stand[p.version] for p in places)
            live = tuple(p.version for p in places if name in stand[p.version] or name in module.fuzzy)
            members[name] = Member(name, kind, module.path, tuple(places), landed, live)
            if name in module.fuzzy:
                fuzzy[name] = module.path
        if not order:
            continue
        held = tuple(n for n in order if n in members)
        library = _library(config, module.path)
        groups[module.path] = Group(
            module.path, module.segment, held, library, module.path, _header(snapshot, module.path)
        )
        if any(members[n].landed for n in held):
            units[module.path] = Unit(module.path, "c", module.path, held, config.project.toolchain, {})
    return Layout(groups, members, units, fuzzy)


def extents(
    table: Mapping[str, tuple[int, int | None, int | None, str]], owned: Collection[str] | None = None
) -> dict[str, int]:
    """Per symbol with no size its extent up to the next symbol's ROM offset (the last one has none). With `owned`
    only a symbol some module owns ends an extent: the aliases inside a datum (a table's halfword) are no boundary."""
    starts = sorted({rom for n, (_, rom, _, _) in table.items() if rom is not None and (owned is None or n in owned)})
    after = dict(pairwise(starts))
    return {n: after[rom] - rom for n, (_, rom, size, _) in table.items() if rom is not None and rom in after}


def of(snapshot: Snapshot) -> Layout:
    """The layout of a snapshot, built once per snapshot digest."""
    return api.memo(("recipe.world", snapshot.digest), lambda: _build(snapshot))


def open_items(snapshot: Snapshot) -> list[Json]:
    """The work list: every member no file defines as C yet, {member, kind, segment, size, address} in its reference
    version, largest first and the tiny ones last."""
    layout, names_from = of(snapshot), snapshot.config.project.names_from
    rows = []
    for name, member in layout.members.items():
        if member.landed:
            continue
        version = member.reference(names_from)
        places = [p for p in member.placements if p.version == version]
        segment = layout.groups[member.group].segment if member.group in layout.groups else ""
        size, address = sum(p.size for p in places), min(p.vram for p in places)
        rows.append({"member": name, "kind": member.kind, "segment": segment, "size": size, "address": address})
    return sorted(rows, key=lambda r: (r["size"] < 16, -r["size"], r["address"], r["member"]))


def unit_of(snapshot: Snapshot, name: str) -> Unit | None:
    """The decompiled unit that defines `name` as C, None while it is behind a stand-in or fuzzy."""
    layout = of(snapshot)
    member = layout.members.get(name)
    if member is None or not member.landed:
        return None
    return layout.units.get(member.group)


def _rom(path: Path) -> mmap.mmap:
    with path.open("rb") as handle:
        return mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)


def rom_bytes(version: Version, start: int, end: int) -> bytes:
    """The ROM bytes [start, end) of one version, the image mapped once per command."""
    image = api.memo(("recipe.rom", str(version.rom), version.rom_sha256), lambda: _rom(version.rom))
    return bytes(image[start:end])


def _symbol_table(
    config: Config, version: str
) -> tuple[dict[str, tuple[int, int | None, int | None, str]], list[tuple[int, str]]]:
    """One version's symbol facts and its segments sorted by rom, read once while the fact files stay as they are:
    `api.asm_path` parses both fact files on every call, which cost 4000 parses (194 CPU s) in a cold scan."""
    root = config.project.root
    stamps = []
    for name in (f"versions/{version}/symbol_addrs.txt", f"versions/{version}/aliases.txt"):
        try:
            seen = (root / name).stat()
            stamps.append((name, seen.st_mtime_ns, seen.st_size))
        except FileNotFoundError:
            stamps.append((name, 0, 0))

    def build() -> tuple[dict[str, tuple[int, int | None, int | None, str]], list[tuple[int, str]]]:
        rows = sorted((s.rom, s.name) for s in config.project.version[version].segments)
        return dict(api.symbols(config, version)), rows

    return api.memo(("recipe.symbol-table", str(root), version, tuple(stamps)), build)


def asm_path(config: Config, version: str, name: str) -> Path:
    """The extracted assembly of a symbol, absolute."""
    facts, segments = _symbol_table(config, version)
    row = facts.get(name)
    held = [segment for start, segment in segments if row and row[1] is not None and start <= row[1]]
    if row is None or row[1] is None or not held:  # unknown or unplaced: unbake names the refusal
        return config.project.root / api.asm_path(config, version, name)
    folder = "nonmatchings" if row[3] == "func" else "data"
    return config.project.root / f"asm/{version}/{folder}/{held[-1]}/{name}.s"


ASM_WAIT = 45.0  # seconds a missing assembly file is waited for: a concurrent extraction swaps asm/<v>/ in minutes


def read_asm(path: Path) -> str:
    """The text of an extracted assembly file. Another process's extraction replaces the whole asm/<v>/ directory
    (old one moved aside, fresh one written), so a file that is missing is polled for before it is called absent."""
    deadline = time.monotonic() + ASM_WAIT
    while True:
        try:
            return path.read_text(errors="replace")
        except FileNotFoundError as gone:
            if time.monotonic() >= deadline:
                raise AsmMissing(
                    f"{path} is missing: an extraction replacing asm/ was still running, or the file is truly absent"
                ) from gone
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


class AsmMissing(FileNotFoundError):
    """An assembly file stayed missing past ASM_WAIT."""
