"""Twin rung: the same function already decompiled in another version, renamed for this one."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from unbake import config as configuration
from unbake import effort, infer, layout
from unbake.contracts import Snapshot, UnitSpec

from recipemaker import cparse, draft, index


@dataclass(frozen=True)
class Twin:
    member: str
    counterpart: str
    version: str
    other_version: str
    unit: str
    names: Mapping[str, str]  # counterpart name -> name in this version


def _decompiled(snapshot: Snapshot, member: str) -> UnitSpec | None:
    unit = layout.unit_of(snapshot, member)
    kinds = configuration.load_resource("units.toml")["kind"]
    return unit if unit is not None and kinds[unit.kind]["decompiled"] else None


def _directions(snapshot: Snapshot) -> list[tuple[str, str]]:
    return [(a, b) for a in sorted(snapshot.versions) for b in sorted(snapshot.versions) if a != b]


def pairs(snapshot: Snapshot, directions: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    """infer.pairs, kept in the content cache: the counterpart maps are a pure function of the snapshot."""
    return index.persisted(snapshot, "recipe.pairs", lambda: infer.pairs(snapshot, directions), extra=directions)


def find(snapshot: Snapshot, member: str) -> Twin | None:
    item = snapshot.layout.members.get(member)
    if item is None or item.kind != "function":
        return None
    with effort.stage("recipe.twin"):
        version = item.reference(snapshot.config.project.names_from)
        directions = [d for d in _directions(snapshot) if d[0] == version]
        found = pairs(snapshot, directions)
        for (a, b), mapping in found.items():
            other = mapping.get(member)
            unit = _decompiled(snapshot, other) if other else None
            if other is None or other == member or unit is None:
                continue
            names = {y: x for x, a_, y, b_ in infer.correspondences(snapshot) if (a_, b_) == (a, b)}
            return Twin(member, other, a, b, unit.path, {**names, other: member})
        return _clone(snapshot, member, version)


def _alike(snapshot: Snapshot, member: str) -> list[str]:
    """The other functions with the same mnemonic sequence as `member`, by name."""
    return next((sorted(g) for g in index.get(snapshot).shapes.values() if member in g), [])


def _clone(snapshot: Snapshot, member: str, version: str) -> Twin | None:
    """A landed function of the same shape in this version: the seed when no other version has the function."""
    for other in _alike(snapshot, member):
        unit = _decompiled(snapshot, other) if other != member else None
        if unit is not None:
            return Twin(member, other, version, version, unit.path, {other: member})
    return None


def alike(snapshot: Snapshot, member: str) -> list[str]:
    """The open functions with the same shape as `member`."""
    return [m for m in _alike(snapshot, member) if m != member and _decompiled(snapshot, m) is None]


def _wanting(snapshot: Snapshot, member: str) -> bool:
    """Open, or landed with some versions withheld: the members a twin is for."""
    unit = layout.unit_of(snapshot, member)
    return _decompiled(snapshot, member) is None or bool(unit and unit.withheld)


def ready(snapshot: Snapshot) -> frozenset[str]:
    """The members `find` finds a twin for, by the same two rules; one pass per snapshot digest."""
    return effort.memo(
        ("twin.ready", snapshot.digest),
        lambda: index.persisted(snapshot, "recipe.twin-ready", lambda: _ready(snapshot)),
    )


def _ready(snapshot: Snapshot) -> frozenset[str]:
    with effort.stage("recipe.twin-ready"):
        found = pairs(snapshot, _directions(snapshot))
        cloned = (
            m
            for group in index.get(snapshot).shapes.values()
            for m in group
            if any(_decompiled(snapshot, x) for x in group if x != m)
        )
        names = snapshot.config.project.names_from
        mapped = (
            x
            for (a, _), mapping in found.items()
            for x, y in mapping.items()
            if x != y and x in snapshot.layout.members and snapshot.layout.members[x].reference(names) == a
            if _decompiled(snapshot, y)
        )
        members = snapshot.layout.members
        return frozenset(
            m for m in [*mapped, *cloned] if m in members and members[m].kind == "function" and _wanting(snapshot, m)
        )


def swap(text: str, names: Mapping[str, str]) -> str:
    if not names:
        return text
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        + r")(?![A-Za-z0-9_])"
    )
    return pattern.sub(lambda m: names[m[1]], text)


def function(source: str, name: str) -> str | None:
    head = re.search(rf"^[^\n;{{}}#]*\b{re.escape(name)}\s*\([^;{{]*\)\s*\{{", source, re.M)
    if head is None:
        return None
    return source[head.start() : cparse.close(source, head.end() - 1) + 1] + "\n"


def _local(source: str, end: int) -> str:
    """What precedes the twin function in its source: types, data and static helpers, but no other function."""
    text = source[:end]
    for name in re.findall(r"^(?![\s#]|static)[^\n;{}]*?\b(\w+)\s*\([^;{]*\)\s*\{", text, re.M):
        text = text.replace(function(text, name) or "", "")
    return re.sub(r"^(?:#.*|(?!typedef|static|extern)[^\n#{}]*\w\s*\([^{;]*\)\s*;)\n", "", text, flags=re.M)


def candidate(snapshot: Snapshot, member: str) -> str | None:
    twin = find(snapshot, member)
    if twin is None:
        return None
    source = snapshot.read(twin.unit).decode(errors="replace")
    body = function(source, twin.counterpart)
    if body is None:
        return None
    text = swap(_local(source, source.index(body)) + "\n" + body, twin.names)
    have = [line for line in source.splitlines() if line.startswith("#include")]
    wanted = draft.include_paths(draft.declared_names(snapshot), text)  # the names it was renamed to
    return "\n".join([*have, *(f'#include "{p}"' for p in wanted if f'#include "{p}"' not in have)]) + "\n" + text
