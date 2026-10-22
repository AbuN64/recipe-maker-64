"""m2c's unknown-field inference prints a project struct again with the fields it read inside the padding. A draft
that keeps that definition redefines the header's struct and never compiles. The fold moves the new fields into the
header's own definition: each field the header names keeps its header line, padding m2c split becomes m2c's fields,
and the draft keeps only its code. Pure text in, text out; the caller overlays and proves the header edits."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

# a struct m2c prints at file scope: `typedef struct Tag {` .. `} Name;` or `struct Tag {` .. `};`, no nested braces
_PRINTED = re.compile(r"^(?:typedef )?struct (\w+) \{\n([^{}]*?)^\}(?: (\w+))?;\n\n?", re.M)
_COMMENT = re.compile(r"/\*.*?\*/")
_FIELD = re.compile(r"(\w+)\s*(?:\[[^\]]*\])*\s*;\s*$")
_PAD = re.compile(r"^\s*(?:char|s8|u8|signed char|unsigned char)\s+\w+\s*\[[^\]]*\]\s*;\s*$")
_INVENTED = re.compile(r"\b\w*func_[0-9A-Fa-f]{8}\w*")  # a type named after one function
_ALIAS = re.compile(r"^typedef struct (\w+) (\w+);", re.M)


@dataclass(frozen=True)
class Body:
    path: str
    start: int  # the offset of the body's first character, just after `{\n`
    end: int  # the offset of the closing `}`
    text: str


def bodies(path: str, text: str) -> dict[str, Body]:
    """Each struct this header defines with a body, by its tag and by an inline typedef name."""
    found: dict[str, Body] = {}
    for m in re.finditer(r"^(typedef )?struct (\w+)? ?\{\n", text, re.M):
        depth, at = 1, m.end()
        while depth and at < len(text):
            depth += {"{": 1, "}": -1}.get(text[at], 0)
            at += 1
        if depth:
            continue
        body = Body(path, m.end(), at - 1, text[m.end() : at - 1])
        named = re.match(r"\s*(\w+)\s*;", text[at:]) if m[1] else None
        for name in filter(None, (m[2], named[1] if named else None)):
            found.setdefault(name, body)
    return found


def aliases(texts: Mapping[str, str]) -> dict[str, str]:
    """typedef name -> struct tag, for `typedef struct Tag Name;` in any header."""
    return {m[2]: m[1] for text in texts.values() for m in _ALIAS.finditer(text)}


def _size(line: str) -> int | None:
    found = re.search(r"\[\s*(0x[0-9A-Fa-f]+|\d+)\s*\]", line)
    return int(found[1], 0) if found else None


def _name(line: str) -> str:
    found = _FIELD.search(_COMMENT.sub("", line))
    return found[1] if found else ""


_WORDS = {4: "s32", 2: "s16", 1: "s8"}


def _sized(printed: str) -> str:
    """An access m2c could not size (`M2C_UNK unkC;`, no room) before the padding that starts at its offset, as the
    widest aligned integer that padding holds (a word, else a halfword, else a byte) taken from its front."""

    def word(m: re.Match[str]) -> str:
        at, size = int(m[2], 16), int(m[4], 0)
        if int(m[3], 16) != at or not size:
            return m[0]
        width = next(w for w in (4, 2, 1) if size >= w and not at % w)
        rest = f"\n{m[1]}char pad{at + width:X}[0x{size - width:X}];" if size > width else ""
        return f"{m[1]}{_WORDS[width]} unk{m[2]};{rest}"

    pattern = r"^(\s*)M2C_UNK unk([0-9A-F]+);[^\n]*\n\s*char pad([0-9A-F]+)\[(0x[0-9A-F]+|\d+)\];"
    return re.sub(pattern, word, printed, flags=re.M)


def split(text: str, mark: str) -> list[str]:
    """`text` cut at each `mark` outside braces, brackets and parentheses."""
    parts, depth, start = [], 0, 0
    for at, char in enumerate(text):
        depth += (char in "{[(") - (char in "}])")
        if char == mark and not depth:
            parts.append(text[start:at])
            start = at + 1
    return [*parts, text[start:]]


def arguments(text: str) -> list[str]:
    """The comma-separated parts of a call's or a prototype's argument text, each stripped."""
    return [part.strip() for part in split(text, ",")]


def _statements(body: str) -> list[tuple[str, frozenset[str]]]:
    """Each member declaration of a struct body as one line with the names it holds: `T a, *b;` is one line per
    declarator, and a nested record member (`union { .. } u;`, or an anonymous one) stays whole on one line, holding
    its own name, else the names of its members."""
    out: list[tuple[str, frozenset[str]]] = []
    for part in split(_COMMENT.sub("", body), ";"):
        part = " ".join(part.split())
        if not part:
            continue
        if "{" in part:
            inner = part[part.index("{") + 1 : part.rindex("}")]
            after = re.fullmatch(r"\s*(\w+)\s*(?:\[[^\]]*\]\s*)*", part[part.rindex("}") + 1 :])
            names = frozenset({after[1]} if after else {n for s in _statements(inner) for n in s[1]})
            out.append((part + ";", names))
            continue
        pieces = split(part, ",")
        head = re.match(r"(.*?)(\**\s*\w+\s*(?:\[[^\]]*\]\s*)*)$", pieces[0]) if len(pieces) > 1 else None
        if head is None:
            out.append((part + ";", frozenset({_name(part + ";")} - {""})))
            continue
        base = head[1].strip()
        for piece in [head[2], *pieces[1:]]:
            line = f"{base} {piece.strip()};"
            out.append((line, frozenset({_name(line)} - {""})))
    return out


def merge(header: str, printed: str, alias: Mapping[str, str]) -> str | None:
    """The header body with m2c's fields in its padding, or None when m2c's version drops a named field or holds text a
    header may not have. A pointer to a typedef'd struct is spelled `struct Tag *` so the header needs no new order.
    A nested record member of the header stays as it is, standing for every field m2c printed from it (m2c spells a
    named one `union _anonymous u;` and the members of an anonymous one each on its own line)."""
    if "M2C_" in re.sub(r"^\s*M2C_UNK\s+\w+;.*$", "", printed, flags=re.M):
        return None
    statements = _statements(header)
    mine = {next(iter(names)): line for line, names in statements if "{" not in line and names}
    nested = [(line, names) for line, names in statements if "{" in line]
    lines: list[str] = []
    seen: set[str] = set()
    for raw in _sized(printed).splitlines():
        line = _COMMENT.sub("", raw).rstrip()
        name = _name(line)
        if not name or re.match(r"\s*M2C_UNK\s+\w+;", line):  # an access m2c could not size, left with no room
            continue
        group = next(((text, names) for text, names in nested if name in names), None)
        if group is not None:
            if not group[1] & seen:
                lines.append("    " + group[0])
            seen |= group[1]
            continue
        seen.add(name)
        if name in mine and _size(mine[name]) == _size(line):  # a resized padding is m2c's piece of the old one
            lines.append("    " + mine[name])
            continue
        if _INVENTED.search(line):  # a record type named after one function is no field type a header may name
            if not re.match(r"\s*(?:struct\s+)?\w+\s*\*", line):
                return None
            line = re.sub(r"^(\s*)(?:struct\s+)?\w+\s*\*", r"\1void *", line)
        spelled = re.sub(
            r"^(\s*)(\w+)(\s*\*)", lambda m: f"{m[1]}struct {alias[m[2]]}{m[3]}" if m[2] in alias else m[0], line
        )
        lines.append("    " + spelled.strip())
    if any(not _PAD.match(x) for n, x in mine.items() if n not in seen) or any(not n & seen for _, n in nested):
        return None  # a named field would vanish
    same = [" ".join(x.split()) for x in lines] == [line for line, names in statements if names]
    return header if same else "\n".join(lines) + "\n"  # the same fields: m2c added nothing the header lacks


def where(headers: Mapping[str, str]) -> dict[str, str]:
    """struct tag or inline typedef name -> the first header that defines it with a body."""
    found: dict[str, str] = {}
    for path, text in headers.items():
        for name in bodies(path, text):
            found.setdefault(name, path)
    return found


def fold(
    text: str,
    headers: Mapping[str, str],
    defined: Mapping[str, str] | None = None,
    alias: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    """The draft without each struct m2c printed again for a header that defines it, the header texts that hold m2c's
    new fields instead, and one note per fold. `defined` and `alias` are `where` and `aliases` of the headers."""
    defined = where(headers) if defined is None else defined
    alias = aliases(headers) if alias is None else alias
    edits: dict[str, str] = {}
    notes: list[str] = []
    out = text
    for m in list(_PRINTED.finditer(text)):
        tag = m[1]
        if tag.startswith("_m2c_stack_"):
            continue
        path = defined.get(tag)
        known = bodies(path, edits.get(path, headers[path])).get(tag) if path else None
        if known is None:
            continue
        merged = merge(known.text, m[2], alias)
        if merged is None:
            continue
        out = out.replace(m[0], "", 1)
        if merged == known.text:  # the header's definition already holds every field m2c printed
            continue
        head = edits.get(known.path, headers[known.path])
        edits[known.path] = head[: known.start] + merged + head[known.end :]
        notes.append(f"folded the fields m2c read inside struct {tag} into {known.path}")
    return out, edits, notes
