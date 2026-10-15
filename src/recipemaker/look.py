"""`recipe look`: prototypes, struct layouts and fields from the index."""

from __future__ import annotations

import difflib
from typing import Any

from unbake import effort, headers, layout, types
from unbake.contracts import Config, Json, Snapshot

from recipemaker import index

_ROWS = 6  # usage rows shown


def _hex(value: int) -> str:
    return f"0x{value:X}"


def _answer(name: str, kind: str, text: str, where: str, **more: Any) -> Json:
    return {"name": name, "kind": kind, "text": text, "where": where, "fields": [], "seen_in": [], "facts": [], **more}


def _function(snapshot: Snapshot, name: str, idx: index.Index) -> Json | None:
    row = types.load(snapshot)["function"].get(name)
    declared = idx.declarations.get(name)
    if row is None and (declared is None or "(" not in declared[2]):
        return None
    unit = layout.unit_of(snapshot, name)
    where = f"{declared[0]}:{declared[1]}" if declared else ""
    text = (row["signature"] + ";") if row else declared[2] if declared else ""
    extra = {"landed": unit.path if unit else "", "evidence": row["evidence"] if row else "declared"}
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


def _reach(snapshot: Snapshot, name: str, item: str) -> int | None:
    """The largest offset from `name` that the leased item's accesses reach, from unbake's usage rows."""
    if not item:
        return None
    start = snapshot.versions[index.reference(snapshot, item)].symbols.get(name)
    rows = index.usage(snapshot, item)["addresses"]
    return max((r["address"] - start for r in rows if r["base"] and r["base"][0] == name), default=None)


def _global(snapshot: Snapshot, name: str, idx: index.Index, item: str) -> Json | None:
    row = types.load(snapshot)["global"].get(name)
    declared = idx.declarations.get(name)
    landed = [e for e in headers.externs(snapshot).get(name, []) if not e[0].startswith("src/fuzzy/")]
    if row is not None:
        text, where, size = row["declaration"], f"types.toml ({row['evidence']})", ""
    elif declared is not None and "(" not in declared[2]:
        text, where, size = declared[2], f"{declared[0]}:{declared[1]}", ""
    elif landed:
        text, where, size = "", f"{landed[0][0]}:{landed[0][1]}", ""
    else:
        return None
    text += "".join(f"{'; ' if text else ''}declared in the landed source {p}:{n} as `{t}`" for p, n, t in landed[:3])
    if (top := _reach(snapshot, name, item)) is not None:
        text += f"; {name} {size}the leased function reaches +0x{top:X}"
    return _answer(name, "global", text, where, facts=[*_conflict(snapshot, name), *_usage(snapshot, name)])


def _conflict(snapshot: Snapshot, name: str) -> list[str]:
    """Every declaration the census finds in disagreement for `name`, and which one it treats as authoritative."""
    found = next((f for f in types.conflicts(snapshot) if f.unit == name), None)
    if found is None:
        return []
    said = found.symptoms.get("definition")
    wins = f"{said['path']}:{said['line']} (the landed definition)" if said else "none: no landed source defines it"
    return [f"conflict: {row}" for row in found.missing] + [f"authoritative: {wins}"]


def _row(start: int, r: Json) -> str:
    who = ", ".join(f"{n}{' (landed)' * landed}" for n, landed in r["readers"][:3]) or "no function"
    base = f"; reached as {r['base'][0]}+0x{r['base'][1]:X}" if r["base"] else ""
    span = r["span"]
    both = (
        f"; written together by {span['writer']}: {span['stores']} stores, {span['fills']} fills, "
        f"+0x{span['size']:X} bytes"
        if span
        else ""
    )
    spelled = f"; {r['spellings'][0][2]}" if r["spellings"] else ""
    return f"+0x{r['address'] - start:X} {r['how']} {r['width']} bytes: {who}{spelled}{base}{both}"


def _usage(snapshot: Snapshot, name: str) -> list[str]:
    """The versions that name the symbol and, from unbake's usage rows, who touches it and how."""
    found = types.usage_at(snapshot, name)
    have = [v for v in snapshot.versions if v in found]
    if not have:
        return []
    missing = [v for v in snapshot.versions if v not in found]
    named = f"named in: {', '.join(have)}" + f"; not in: {', '.join(missing)}" * bool(missing)
    start = snapshot.versions[have[0]].symbols[name]
    return [named, *(_row(start, r) for r in found[have[0]][:_ROWS])]


def _unknown(snapshot: Snapshot, name: str, idx: index.Index) -> Json:
    """A name no project header defines, and the nearest names the project does define."""
    known = {*idx.declarations, *(n.removeprefix("struct ") for i in idx.identities for n in i.names)}
    near = difflib.get_close_matches(name, known | types.load(snapshot)["global"].keys(), 5)
    nearest = ", ".join(f"{n} ({idx.declarations[n][0]})" if n in idx.declarations else n for n in near)
    member = snapshot.layout.members.get(name)
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
    with effort.stage("recipe.look"):
        snapshot = layout.capture(config)
        idx = index.get(snapshot)
        found = _global(snapshot, name, idx, item) or _function(snapshot, name, idx) or _type(name, idx)
        return found or _unknown(snapshot, name, idx)
