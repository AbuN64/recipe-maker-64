"""`recipe look`: prototypes, struct layouts and fields from the index."""

from __future__ import annotations

import difflib
from typing import Any

from unbake.contracts import Config, Json, Snapshot

from recipemaker import headers, host, index, world


def _hex(value: int) -> str:
    return f"0x{value:X}"


def _answer(name: str, kind: str, text: str, where: str, **more: Any) -> Json:
    return {"name": name, "kind": kind, "text": text, "where": where, "fields": [], "seen_in": [], "facts": [], **more}


def _function(snapshot: Snapshot, name: str, idx: index.Index) -> Json | None:
    declared = idx.declarations.get(name)
    if declared is None or "(" not in declared[2]:
        return None
    unit = world.unit_of(snapshot, name)
    where, text = f"{declared[0]}:{declared[1]}", declared[2]
    extra = {"landed": unit.path if unit else "", "evidence": "declared"}
    return _answer(name, "function", text, where, seen_in=list(idx.callers.get(name, ())), **extra)


def _type(name: str, idx: index.Index) -> Json | None:
    found = next((i for i in idx.identities if name in i.names or f"struct {name}" in i.names), None)
    if found is None:
        return None
    rows = [
        {"offset": _hex(f.offset), "size": f.size, "kind": f.kind, "name": f.name, "count": f.count}
        for f in found.fields
    ]
    where = ", ".join(sorted({v.path for v in found.views}))
    text = f"{len(found.views)} views, {len(found.fields)} fields"
    if not any(v.path.endswith(".h") for v in found.views):
        text += f"; declared only in the source {where} and in no header: using it needs a header change"
        text += " (recipe skip --reason needs-header-change); never redefine it locally"
    return _answer(name, "type", text, where, fields=rows, seen_in=[v.path for v in found.views])


def _global(snapshot: Snapshot, name: str, idx: index.Index) -> Json | None:
    declared = idx.declarations.get(name)
    landed = headers.externs(snapshot).get(name, [])
    if declared is not None and "(" not in declared[2]:
        text, where = declared[2], f"{declared[0]}:{declared[1]}"
    elif landed:
        text, where = "", f"{landed[0][0]}:{landed[0][1]}"
    else:
        return None
    text += "".join(f"{'; ' if text else ''}declared in the landed source {p}:{n} as `{t}`" for p, n, t in landed[:3])
    return _answer(name, "global", text, where, facts=[*_conflict(snapshot, name), *_usage(name, idx)])


def _conflict(snapshot: Snapshot, name: str) -> list[str]:
    """Every declaration the census finds in disagreement for `name`, and which one it treats as authoritative."""
    found = next((f for f in headers.disagreements(snapshot)[1] if f.unit == name), None)
    if found is None:
        return []
    said = found.symptoms.get("definition")
    wins = f"{said['path']}:{said['line']} (the landed definition)" if said else "none: no landed source defines it"
    return [f"conflict: {row}" for row in found.missing] + [f"authoritative: {wins}"]


def _usage(name: str, idx: index.Index) -> list[str]:
    """Who touches the symbol in the names version, and how: the functions and mnemonics of its %lo accesses."""
    rows = idx.uses.get(name, ())
    return [f"+0x{offset:X} {mnemonic}: {who}" for who, mnemonic, offset in sorted(set(rows), key=lambda r: r[2])[:6]]


def _unknown(snapshot: Snapshot, name: str, idx: index.Index) -> Json:
    """A name no project header defines, and the nearest names the project does define."""
    known = {*idx.declarations, *(n.removeprefix("struct ") for i in idx.identities for n in i.names)}
    near = difflib.get_close_matches(name, known, 5)
    nearest = ", ".join(f"{n} ({idx.declarations[n][0]})" if n in idx.declarations else n for n in near)
    member = world.of(snapshot).members.get(name)
    said = "no header declares it; declare its prototype in your file (from the brief's Types line)"
    text = (
        f"{name} is a function: {said}"
        if member and member.kind == "function"
        else f"{name} is not defined in the project"
    )
    return _answer(
        name, "unknown", f"{text}; " + (f"nearest known: {nearest}" if nearest else "no known name is close"), ""
    )


def look(config: Config, name: str, item: str = "") -> Json:
    with host.stage("recipe.look"):
        snapshot = world.capture(config)
        idx = index.get(snapshot)
        found = _global(snapshot, name, idx) or _function(snapshot, name, idx) or _type(name, idx)
        return found or _unknown(snapshot, name, idx)
