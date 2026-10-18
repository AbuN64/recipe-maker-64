"""Struct views read from the project's headers: where each named field sits and how big it is."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from pycparser import CParser, c_ast
from pycparser.c_parser import ParseError
from unbake import api
from unbake.contracts import Finding, Refusal, Snapshot, digest

from recipemaker import headers

SCALARS = {
    **dict.fromkeys(("s8", "u8", "char", "void"), 1),
    **dict.fromkeys(("s16", "u16", "short"), 2),
    **dict.fromkeys(("s32", "u32", "int", "long", "f32", "float"), 4),
    **dict.fromkeys(("s64", "u64", "f64", "double", "long long", "Gfx"), 8),
}
FLOATS = {"f32", "float", "f64", "double"}
_WORDS = {"const", "volatile", "unsigned", "signed", "struct", "union", "enum", "static", "extern"}
_BODY = re.compile(r"(typedef\s+)?(struct|union)\s*(\w+)?\s*\{")
_SLOT = re.compile(r"sp[0-9A-F]+")  # a stack slot of one function, not a field of a type
_TOKEN = re.compile(r"\w+|\*|\[[^\]]*\]")
_POINTER = re.compile(r"\(\s*\*\s*(\w+)\s*\)\s*\(")
_TYPEDEF = re.compile(r"typedef\s+((?:[\w ]|\*)+?)\s*(\w+)\s*;")


Member = tuple[str, int, str, list[str]]  # (type, stars, name, dims); name "" is an anonymous record member
Record = tuple[str | None, str | None, list[Member], str]  # (typedef name, tag, members, "struct" or "union")
Parsed = tuple[list[Record], list[tuple[str, str]]]


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    size: int  # of one element
    count: int
    kind: str  # "f" float, "i" integer, "p" pointer, "s" record
    type: str = ""  # the element's C type as the header spells it (`s32`, `MenuWidget *`, `struct Slot`)


@dataclass(frozen=True)
class Layout:
    name: str  # the C type to write: a typedef name, else "struct tag"
    path: str  # the header that defines it
    fields: tuple[Field, ...]
    pads: tuple[Field, ...] = ()  # the padding arrays (pad*), by offset: room a field may be split out of
    records: tuple[Field, ...] = ()  # the members that are records themselves (kind "s"), by offset
    size: int = 0  # sizeof the type, 0 when unknown
    aliases: tuple[str, ...] = ()  # the typedef names spelled for it besides `name`
    union: bool = False  # every member starts at offset 0
    inner: bool = False  # an untagged record written inside another (`name` is its member path), C cannot spell it


def _clean(text: str) -> str:
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.DOTALL)
    return re.sub(r"^\s*#[^\n]*(?:\\\n[^\n]*)*", " ", text, flags=re.MULTILINE)


def close(text: str, start: int) -> int:
    depth = 0
    for at in range(start, len(text)):
        depth += (text[at] == "{") - (text[at] == "}")
        if depth == 0:
            return at
    return len(text)


def _key(keyword: str, tag: str) -> str:
    return f"{keyword} {tag}"


def _members(body: str, owner: str | None, records: list[Record]) -> list[Member]:
    """The members of one record body. A record defined inside it is scanned too: a tagged one as its own entry, an
    untagged one as an entry named by its member path from `owner` (`Owner.u`, `Owner.1` for an anonymous member)."""
    out: list[Member] = []
    depth, start, count = 0, 0, 0
    for at, char in enumerate(body + ";"):
        depth += (char == "{") - (char == "}")
        if char != ";" or depth:
            continue
        part, start = body[start:at].strip(), at + 1
        if not part:
            continue
        inner = ""
        if "{" in part:
            nested = re.match(r"(struct|union)\s*(\w+)?\s*\{", part)
            if nested is None or (owner is None and not nested[2]):
                break
            end = close(part, nested.end() - 1)
            declarators = part[end + 1 :].strip()
            count += 1
            first = re.match(r"[\s*]*(\w+)", declarators)
            inner = _key(nested[1], nested[2]) if nested[2] else f"{owner}.{first[1] if first else count}"
            records.append(
                (None if nested[2] else inner, nested[2], _members(part[nested.end() : end], inner, records), nested[1])
            )
            if not declarators:
                out.append((inner, 0, "", []))
                continue
            part = f"_record_ {declarators}"
        if ":" in part:
            break
        if pointer := _POINTER.search(part):
            out.append(("void", 1, pointer[1], []))
            continue
        kind = ""
        for number, piece in enumerate(part.split(",")):
            tokens = _TOKEN.findall(piece)
            dims = [t.strip("[] ") for t in tokens if t.startswith("[")]
            words = [t for t in tokens if not t.startswith("[")]
            if len(words) < 1 + (number == 0):
                return out
            if number == 0:
                kind = inner or " ".join(w for w in words[:-1] if w not in _WORDS and w != "*")
            out.append((kind, words[:-1].count("*"), words[-1], dims))
    return out


def scan(text: str) -> Parsed:
    text = _clean(text)
    structs: list[Record] = []
    at = 0
    while found := _BODY.search(text, at):
        end = close(text, found.end() - 1)
        alias = re.match(r"\s*(\w+)\s*;", text[end + 1 :]) if found[1] else None
        owner = alias[1] if alias else _key(found[2], found[3]) if found[3] else None
        members = _members(text[found.end() : end], owner, structs)
        structs.append((alias[1] if alias else None, found[3], members, found[2]))
        at = end + 1  # a record written inside is scanned with its owner
    plain = [(m[2], " ".join(w for w in m[1].split() if w not in _WORDS)) for m in _TYPEDEF.finditer(text)]
    return structs, plain


def _length(dim: str, sizeof: Callable[[str], int]) -> int | None:
    text = re.sub(r"sizeof\s*\(\s*([\w ]+?)\s*(\*?)\s*\)", lambda m: str(4 if m[2] else sizeof(m[1])), dim)
    if not re.fullmatch(r"[\s\dxXa-fA-F()+*-]+", text):
        return None
    try:
        value = eval(text, {"__builtins__": {}})
    except (SyntaxError, TypeError, NameError, ZeroDivisionError):
        return None
    return value if isinstance(value, int) and value >= 0 else None


def _raw(parsed: dict[str, Parsed]) -> dict[str, tuple[str, list[Member], str | None, str | None, bool]]:
    """Each record by the name C spells it (`Name`, `struct tag`, `union tag`; an inner one by its member path)."""
    return {
        (alias or f"{keyword} {tag}"): (path, members, alias, tag, keyword == "union")
        for path, (structs, _) in parsed.items()
        for alias, tag, members, keyword in structs
        if members and (alias or tag)
    }


def _layouts(parsed: dict[str, Parsed]) -> list[Layout]:
    typedefs = {name: kind for _, plain in parsed.values() for name, kind in plain}
    shapes: dict[str, tuple[int, int, tuple[Field, ...]]] = {}
    pads: dict[str, tuple[Field, ...]] = {}
    raw = _raw(parsed)

    def size_of(kind: str, stars: int, seen: tuple[str, ...]) -> tuple[int, int, str, str] | None:
        """(size, alignment, kind letter, spelled type) of one element."""
        if stars:
            return 4, 4, "p", f"{kind} {'*' * stars}"
        base = typedefs.get(kind, kind)
        if base in SCALARS:
            return SCALARS[base], SCALARS[base], "f" if base in FLOATS else "i", kind
        for key in (base, f"struct {base}", f"union {base}"):
            if key in raw and key not in seen and (got := shape(key, (*seen, key))):
                return got[0], got[1], "s", key
        return None

    def shape(key: str, seen: tuple[str, ...]) -> tuple[int, int, tuple[Field, ...]] | None:
        """A union places every member at offset 0; the members of an anonymous record member are the owner's own,
        at its offset. Padding is room to split only where the record is a plain struct of its own."""
        if key not in shapes:
            union = raw[key][4]
            offset, end, align, fields, room, inner = 0, 0, 1, [], [], []
            for kind, stars, name, dims in raw[key][1]:
                got = size_of(kind, stars, seen)
                count = 1
                for dim in dims:
                    if (length := _length(dim, lambda kind: (size_of(kind, 0, seen) or (0,))[0])) is None:
                        got = None
                        break
                    count *= length
                if got is None:
                    break
                size, unit, letter, spelled = got
                offset = 0 if union else -(-offset // unit) * unit
                if not name and letter == "s":
                    fields += [replace(f, offset=f.offset + offset) for f in shapes[spelled][2]]
                    inner += [replace(f, offset=f.offset + offset) for f in records.get(spelled, ())]
                elif letter == "s":
                    inner.append(Field(name, offset, size, count, letter, spelled))
                elif not name.startswith("pad") and not _SLOT.fullmatch(name):
                    fields.append(Field(name, offset, size, count, letter, spelled))
                elif name.startswith("pad") and not union:
                    room.append(Field(name, offset, size, count, letter, spelled))
                offset, align = offset + size * count, max(align, unit)
                end = max(end, offset)
            shapes[key] = (-(-end // align) * align, align, tuple(fields))
            pads[key] = tuple(room)
            records[key] = tuple(inner)
        return shapes[key]

    records: dict[str, tuple[Field, ...]] = {}
    aliases: dict[str, list[str]] = {}
    for name, kind in typedefs.items():
        for key in (kind, f"struct {kind}", f"union {kind}"):
            if key in raw and name != key and name not in raw:
                aliases.setdefault(key, []).append(name)
                break
    out = []
    for key, (path, _, alias, tag, union) in raw.items():
        if got := shape(key, (key,)):
            inner = alias is not None and tag is None and "." in alias
            view = Layout(key, path, got[2], pads.get(key, ()), records.get(key, ()), got[0], (), union, inner)
            out.append(replace(view, aliases=tuple(aliases.get(key, ()))))
    return out


def _scan_row(row: tuple[str, str]) -> Parsed:
    return scan(row[1])


def landed_sources(snapshot: Snapshot) -> list[str]:
    """The C sources of the tree."""
    return headers.sources(snapshot, "src", ".c")


def load(snapshot: Snapshot) -> list[Layout]:
    """Every struct view the project defines."""
    config = snapshot.config
    paths = [*headers.sources(snapshot), *landed_sources(snapshot)]
    rows = [(path, snapshot.read(path).decode(errors="replace")) for path in paths]
    scanned = api.pool_map(config, "recipe.cparse", _scan_row, rows, digest)
    parsed = {path: found for (path, _), found in zip(rows, scanned, strict=True)}
    return _layouts(parsed)


class _Names(c_ast.NodeVisitor):
    """The names a top-level node uses, and the ones it declares (its own name, the tags and enumerators it defines)."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.declared: set[str] = set()

    def visit_IdentifierType(self, node: c_ast.IdentifierType) -> None:
        self.used.update(node.names)

    def visit_ID(self, node: c_ast.ID) -> None:
        self.used.add(node.name)

    def visit_Enumerator(self, node: c_ast.Enumerator) -> None:
        self.declared.add(node.name)
        self.generic_visit(node)

    def visit_Struct(self, node: c_ast.Struct | c_ast.Union | c_ast.Enum) -> None:
        if node.name:  # a tag is kept apart from a variable by nothing here: the over-kept name is harmless
            (self.declared if getattr(node, "decls", None) or getattr(node, "values", None) else self.used).add(
                node.name
            )
        self.generic_visit(node)

    visit_Union = visit_Enum = visit_Struct


_PIECE = re.compile(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|^[ \t]*#.*$|[{}();]", re.M)


def spans(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, is a directive) for each top-level statement, function body or directive line of `text`."""
    spans, begin, depth, parens, opener = [], 0, 0, 0, ""
    for found in _PIECE.finditer(text):
        token = found[0]
        if token[0] in "\"'":
            continue
        if token.lstrip()[0] == "#":
            if not (depth or parens or text[begin : found.start()].strip()):
                spans.append((found.start(), found.end(), True))
                begin = found.end()
            continue
        if token == "{" and not depth:
            opener = text[begin : found.start()].rstrip()[-1:]
        depth += (token == "{") - (token == "}")
        parens += (token == "(") - (token == ")")
        if (token == ";" and not depth and not parens) or (token == "}" and not depth and opener == ")"):
            spans.append(
                (begin + len(text[begin : found.end()]) - len(text[begin : found.end()].lstrip()), found.end(), False)
            )
            begin = found.end()
    return spans


class Unit:
    """One preprocessed unit parsed once, so any names can be cut to the statements that declare or define them and
    what those reach, transitively. A statement is kept whole and as written; `#pragma` lines are always kept."""

    def __init__(self, text: str) -> None:
        try:
            tree = CParser().parse(re.sub(r"^#\s+\d+.*$", "", headers.normal(text), flags=re.M))  # lines stay physical
        except ParseError as error:
            raise Refusal(Finding("recipe.parse", f"the unit does not parse: {error}")) from error
        breaks = [m.start() for m in re.finditer("\n", text)]
        found = spans(text)
        self.text = text
        self.pragmas = [(a, text[a:b]) for a, b, directive in found if directive and "#pragma" in text[a:b]]
        self.spans = [
            (a, b, bisect_left(breaks, a) + 1, bisect_left(breaks, b - 1) + 1)
            for a, b, directive in found
            if not directive
        ]
        ends = [last for _, _, _, last in self.spans]
        groups: dict[int, list[c_ast.Node]] = {}
        for node in tree.ext:
            if node.coord and (at := bisect_left(ends, node.coord.line)) < len(self.spans):
                groups.setdefault(at, []).append(node)
        self.names: dict[int, _Names] = {}
        self.providers: dict[str, list[int]] = {}
        for at, group in groups.items():
            self.names[at] = names = _Names()
            for node in group:
                names.visit(node)
                top = node.decl if isinstance(node, c_ast.FuncDef) else node
                if isinstance(top, (c_ast.Decl, c_ast.Typedef)) and top.name:
                    names.declared.add(top.name)
            for name in names.declared:
                self.providers.setdefault(name, []).append(at)
        self.defined = {n.decl.name for group in groups.values() for n in group if isinstance(n, c_ast.FuncDef)}

    def cut(self, seeds: Iterable[str]) -> str:
        kept = {at for name in seeds for at in self.providers.get(name, ())}
        todo = list(kept)
        while todo:
            for name in self.names[todo.pop()].used:
                for at in self.providers.get(name, ()):
                    if at not in kept:
                        kept.add(at)
                        todo.append(at)
        pieces = [(self.spans[at][0], self.text[self.spans[at][0] : self.spans[at][1]]) for at in kept]
        return "\n".join(piece for _, piece in sorted(pieces + self.pragmas)) + "\n"


def reach(text: str, member: str) -> str:
    """The unit cut to the function `member` and the declarations it reaches."""
    unit = Unit(text)
    if member not in unit.defined:
        raise Refusal(Finding("recipe.parse", f"{member} is not defined in the unit", unit=member))
    return unit.cut([member])
