"""recipe text: published data members that are integer arrays holding text become char arrays, and words that are
code pointers through KUSEG become references to the function's alias symbol; bytes unchanged.
The rules are draft.text_like and draft.code_alias (one derivation each, shared with the data draft); this module
only finds each member's definition in its unit source, swaps in the draft's spelling and adds the alias rows."""

from __future__ import annotations

import re

from unbake.contracts import Config, Json, Snapshot

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


def _cells(definition: str) -> list[tuple[int, int, int | None]]:
    """Each element of the definition's initializer: (start, end) in the definition and its value, None when the
    element is no plain number."""
    out, start = [], definition.index("{") + 1
    for found in re.finditer(r"[^,{}]+", definition[start : definition.rindex("}")]):
        cell = found[0].strip()
        if not cell:
            continue
        lead = len(found[0]) - len(found[0].lstrip())
        begin = start + found.start() + lead
        try:
            value: int | None = int(cell.rstrip("uUlL"), 0) & 0xFFFFFFFF
        except ValueError:
            value = None
        out.append((begin, begin + len(cell), value))
    return out


Rows = dict[str, dict[str, int]]  # version -> alias name -> its KUSEG address
Held = dict[str, tuple[list[tuple[int, int, world.Member]], list[int]]]  # draft._into's span index per version


def _pointed(
    snapshot: Snapshot, symbol: str, definition: str, held: Held, rows: Rows
) -> tuple[str, list[tuple[int, str, str]], list[str]]:
    """The definition with every word draft.code_alias proves a KUSEG code pointer spelled as `(unsigned int)ALIAS`,
    with each (index, alias, function) and the reasons words were kept. The versions it serves are those whose ROM
    bytes at the symbol its numbers spell; every one must name the same function and share one alias name."""
    if re.search(r"\b(?:short|u16)\b", definition.split("[", 1)[0]):
        return definition, [], []
    cells = _cells(definition)
    images, vram = {}, {}
    for version, record in snapshot.versions.items():  # the array runs over every row inside it: read its own span
        fact = snapshot.files.symbols.get(version, {}).get(symbol)
        if fact is None or fact[1] is None:
            continue
        raw = world.rom_bytes(record, fact[1], fact[1] + 4 * len(cells))
        words = [int.from_bytes(raw[at : at + 4], "big") for at in range(0, len(raw) - 3, 4)]
        if len(words) == len(cells) and all(v is None or v == words[i] for i, (_, _, v) in enumerate(cells)):
            images[version], vram[version] = words, fact[0]
    done: list[tuple[int, str, str]] = []
    kept: list[str] = []
    for index in reversed(range(len(cells))):
        start, end, value = cells[index]
        if value is None or not images:
            continue
        targets = {v: draft.code_alias(snapshot, held, v, vram[v] + 4 * index, images[v], index) for v in images}
        if all(t is None for t in targets.values()):
            continue
        names = {t.name for t in targets.values() if t is not None}
        alias = draft.alias_name(snapshot, tuple(sorted(images)), value)
        if None in targets.values() or len(names) > 1 or alias is None:
            kept.append(f"word {index} 0x{value:08X}: the versions {sorted(images)} disagree on it")
            continue
        definition = definition[:start] + f"(unsigned int){alias}" + definition[end:]
        for version in images:
            if alias not in snapshot.versions[version].symbols:
                rows.setdefault(version, {})[alias] = value
        done.append((index, alias, names.pop()))
    return definition, done[::-1], kept


def _declared(text: str, at: int, name: str) -> bool:
    """Whether the name is declared ahead of `at` in the same preprocessor block (a version block or the file)."""
    block = max((m.end() for m in re.finditer(r"^[ \t]*#[ \t]*(?:if|el|endif).*$", text[:at], re.M)), default=0)
    return re.search(rf"^\s*extern\b[^;]*\b{re.escape(name)}\s*(?:\[\d*\])?\s*;", text[block:at], re.M) is not None


def _rows(config: Config, rows: Rows) -> list[str]:
    """Adds each alias row to its version's symbol_addrs.txt in name order; the files it changes."""
    files = []
    for version, names in sorted(rows.items()):
        path = config.project.root / "versions" / version / "symbol_addrs.txt"
        lines = path.read_text().splitlines(keepends=True)
        for name, value in sorted(names.items()):
            at = next((i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() > name), len(lines))
            lines.insert(at, f"{name} = 0x{value:08X}; //\n")
        host.write(path, "".join(lines).encode())
        files.append(path.relative_to(config.project.root).as_posix())
    return files


def run(config: Config, params: Json) -> Json:
    snapshot = world.capture(config)
    only = set(filter(None, (params.get("only") or "").split(",")))
    texts: dict[str, str] = {}
    items: list[Json] = []
    kept: list[Json] = []
    pointers: list[Json] = []
    rows: Rows = {}
    held: Held = {}
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
        if new:
            texts[unit.path] = text[: found.start()] + new.strip() + text[found.end() :]
            items.append({"symbol": symbol, "file": unit.path, "size": len(written or b"")})
            continue
        for found in reversed(list(pattern.finditer(text))):  # one definition per version block
            definition, done, why = _pointed(snapshot, symbol, found[0], held, rows)
            kept += [{"symbol": symbol, "why": w} for w in why]
            if not done:
                continue
            externs = "".join(
                f"extern char {a}[];\n"
                for a in sorted({a for _, a, _ in done})
                if not _declared(text, found.start(), a)
            )
            text = text[: found.start()] + externs + definition + text[found.end() :]
            texts[unit.path] = text
            pointers += [
                {"symbol": symbol, "file": unit.path, "word": i, "alias": a, "function": f} for i, a, f in done
            ]
    files = sorted(texts)
    if params.get("write"):
        for path, text in texts.items():
            host.write(config.project.root / path, text.encode())
        files += _rows(config, rows)
    return {
        "items": items,
        "pointers": pointers,
        "rows": sum(len(names) for names in rows.values()),
        "kept": kept,
        "files": files,
        "written": bool(params.get("write")),
        "next": "recipe status",
    }
