"""Twin rung: a landed function of the same shape in this tree, renamed for the open one."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from unbake import api
from unbake.contracts import Snapshot

from recipemaker import cparse, draft, host, index, world


@dataclass(frozen=True)
class Twin:
    member: str
    counterpart: str
    version: str
    other_version: str
    unit: str
    names: Mapping[str, str]  # counterpart name -> name in this version


def _decompiled(snapshot: Snapshot, member: str) -> world.Unit | None:
    return world.unit_of(snapshot, member)


def find(snapshot: Snapshot, member: str) -> Twin | None:
    """A landed function with the same mnemonic sequence: one tree holds every version, so a twin is a clone."""
    item = world.of(snapshot).members.get(member)
    if item is None or item.kind != "function":
        return None
    with host.stage("recipe.twin"):
        version = item.reference(snapshot.config.project.names_from)
        for other in _alike(snapshot, member):
            unit = _decompiled(snapshot, other) if other != member else None
            if unit is not None:
                return Twin(member, other, version, version, unit.path, {other: member})
        return None


def _alike(snapshot: Snapshot, member: str) -> list[str]:
    """The other functions with the same mnemonic sequence as `member`, by name."""
    return next((sorted(g) for g in index.get(snapshot).shapes.values() if member in g), [])


def alike(snapshot: Snapshot, member: str) -> list[str]:
    """The open functions with the same shape as `member`."""
    return [m for m in _alike(snapshot, member) if m != member and _decompiled(snapshot, m) is None]


def ready(snapshot: Snapshot) -> frozenset[str]:
    """The open functions `find` finds a twin for; one pass per snapshot digest."""
    return api.memo(("twin.ready", snapshot.digest), lambda: _ready(snapshot))


def _ready(snapshot: Snapshot) -> frozenset[str]:
    with host.stage("recipe.twin-ready"):
        members = world.of(snapshot).members
        return frozenset(
            m
            for group in index.get(snapshot).shapes.values()
            for m in group
            if m in members and members[m].kind == "function" and _decompiled(snapshot, m) is None
            if any(_decompiled(snapshot, x) for x in group if x != m)
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


def block(source: str, name: str) -> str | None:
    """The `#ifdef NON_MATCHING` branch that holds `name`: its types, externs, helpers and body, whole."""
    body = function(source, name)
    if body is None:
        return None
    at = source.index(body)
    lines = source[:at].splitlines(keepends=True)
    depth = 0
    for i in range(len(lines) - 1, -1, -1):  # the open `#if` before the body that names NON_MATCHING
        word = lines[i].lstrip().removeprefix("#").lstrip() if lines[i].lstrip().startswith("#") else ""
        depth += word.startswith("endif") - word.startswith("if")
        if depth < 0:
            if "NON_MATCHING" not in word or "!" in word:
                return body
            head, depth = "".join(lines[i + 1 :]), 0
            break
    else:
        return body
    tail = ""
    for line in source[at + len(body) :].splitlines(keepends=True):  # up to its `#else` or `#endif`
        word = line.lstrip().removeprefix("#").lstrip() if line.lstrip().startswith("#") else ""
        depth += word.startswith("if") - word.startswith("endif")
        if depth < 0 or (depth == 0 and word.startswith("else")):
            break
        tail += line
    return head + body + tail


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
