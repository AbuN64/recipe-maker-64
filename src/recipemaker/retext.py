"""recipe text: published data members that are integer arrays holding text become char arrays, bytes unchanged.
The rule is draft.text_like (one derivation, shared with the data draft); this module only finds each member's
definition in its unit source and swaps in the draft's text."""

from __future__ import annotations

import re

from unbake.contracts import Config, Json

from recipemaker import draft, host, world

_INTEGER = r"(?:const\s+)?(?:unsigned\s+)?(?:int|short|u32|u16)"


def _written(definition: str) -> bytes | None:
    """The bytes the integer array definition emits (big-endian), or None when an element is no plain number."""
    width = 2 if re.search(r"\b(?:short|u16)\b", definition.split("[", 1)[0]) else 4
    body = definition[definition.index("{") + 1 : definition.rindex("}")]
    try:
        values = [int(v.strip().rstrip("uUlL"), 0) for v in body.split(",") if v.strip()]
        return b"".join((v & ((1 << (8 * width)) - 1)).to_bytes(width, "big") for v in values)
    except ValueError:
        return None


def run(config: Config, params: Json) -> Json:
    snapshot = world.capture(config)
    only = set(filter(None, (params.get("only") or "").split(",")))
    texts: dict[str, str] = {}
    items: list[Json] = []
    kept: list[Json] = []
    for name in sorted(world.of(snapshot).members):
        entry = world.of(snapshot).members[name]
        unit = world.unit_of(snapshot, name) if entry.kind == "data" else None
        if unit is None or (only and unit.path not in only):
            continue
        try:
            _, version, placements, _ = draft.data_image(snapshot, name)
        except Exception:
            continue
        text = texts.get(unit.path) or (snapshot.peek(unit.path) or b"").decode()
        aliased = draft.data_symbol(snapshot, entry, version, placements[0].vram)
        for symbol in dict.fromkeys((name, aliased)):  # the source spells the member name, the symbol table an alias
            pattern = re.compile(rf"^{_INTEGER}\s+{re.escape(symbol)}\s*\[\d*\]\s*=\s*\{{[^}}]*\}};", re.M)
            if found := pattern.search(text):
                break
        else:
            continue
        written = _written(found[0])
        new = (
            draft.text_array(snapshot, version, symbol, placements[0].vram, written, "const" in found[0].split("[")[0])
            if written is not None
            else None
        )
        if not new:
            continue
        new = new.strip()
        texts[unit.path] = text[: found.start()] + new + text[found.end() :]
        items.append({"symbol": symbol, "file": unit.path, "size": len(written or b"")})
    if params.get("write"):
        for path, text in texts.items():
            host.write(config.project.root / path, text.encode())
    return {
        "items": items,
        "kept": kept,
        "files": sorted(texts),
        "written": bool(params.get("write")),
        "next": "recipe status",
    }
