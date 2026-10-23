"""The original developers' debug records as evidence: the struct, union and typedef definitions, the functions with
their source files, and the globals a debug catalog holds (schema 1, the output of a reader of the toolchain's debug
stream, such as SN SYM records). The catalog is a project input: `[inputs] debug_types` of the recipe config, a path
absolute or relative to the project root. Nothing here reads a ROM or knows a project.

Three uses: a record the project invented (a per-function view, a padding view, a placeholder record) whose size and
member offsets agree with exactly one original record takes the original type and member names (`recipe name next
--step T`); a function or global whose address in a version is where the record puts it may take the record's name
(`debug NAME` evidence); and the source file of each function is a module boundary for `recipe structure`."""

import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Json

from recipemaker import cparse, state

_SCALAR = {
    "char": 1, "signed char": 1, "unsigned char": 1,
    "short": 2, "unsigned short": 2, "short int": 2, "unsigned short int": 2,
    "int": 4, "unsigned int": 4, "long": 4, "unsigned long": 4, "long int": 4, "unsigned long int": 4,
    "unsigned": 4, "signed": 4, "float": 4, "void": 1,
    "double": 8, "long long": 8, "unsigned long long": 8, "long double": 8,
}  # fmt: skip
_FLOAT = {"float", "double", "long double"}
_ARRAY = re.compile(r"\[(\d*)\]")
KSEG0 = 0x80000000  # a reader may give the address without the segment bit
MIN_FIELDS = 2  # an original record is proposed when at least this many named members agree exactly


@dataclass(frozen=True)
class Slot:
    """One member of an original record: where it starts, its bytes, its kind letter (cparse's: f i p s)."""

    name: str
    offset: int
    size: int
    kind: str
    type: str
    bits: int = 0  # a bit field's width, 0 for a whole member


@dataclass(frozen=True)
class Record:
    """One original struct or union definition, by the name C spells it with (its typedef, else `struct tag`)."""

    name: str
    tag: str  # the tag as the record spells it; an untagged one is the reader's placeholder (`.12fake`)
    union: bool
    size: int
    slots: tuple[Slot, ...]
    where: str  # `version rom 0x...`: where the definition is in the debug stream


@dataclass(frozen=True)
class Function:
    name: str
    version: str
    vram: int | None  # None for an object-file record: its value is a section offset
    file: str  # the source path the record names, as written
    line: int
    frame: int
    params: tuple[str, ...]
    static: bool = False  # a global the record marks file-local


@dataclass(frozen=True)
class Catalog:
    path: str
    records: tuple[Record, ...]
    functions: tuple[Function, ...]
    globals: tuple[Function, ...]  # data symbols: name, version, address; file, line and frame unused

    def named(self, name: str) -> list[Function]:
        return [f for f in (*self.functions, *self.globals) if f.name == name]


def path(config: Config, source: Path | None = None) -> Path | None:
    """The catalog the recipe config names for this project, or None when it names none."""
    found = state.inputs(source).get("debug_types")
    if not found:
        return None
    given = Path(found)
    return given if given.is_absolute() else config.project.root / given


def load(config: Config, source: Path | None = None, roms: Mapping[str, str] | None = None) -> Catalog | None:
    """The configured catalog, read once per command; None when none is configured or when it is another game's.
    A configured path that is not a schema-1 catalog is refused by name. `roms` is the project's ROM sha256 per
    version (default: the snapshot's)."""
    where = path(config, source)
    if where is None:
        return None
    if not where.is_file():
        raise state.stop("recipe.debug", f"the debug catalog {where} does not exist", path=str(where))
    key = ("recipe.debug", str(where), where.stat().st_mtime_ns)
    document = api.memo(key, lambda: json.loads(where.read_text(encoding="utf-8")))
    shas = roms if roms is not None else api.memo(("recipe.roms", str(config.project.root)), lambda: _roms(config))
    kept = own(document, shas)
    if kept is None:
        sys.stderr.write(f"recipe: the debug catalog {where} is ignored: no version's ROM matches its rom_sha256\n")
        return None
    catalog: Catalog = api.memo((*key, tuple(sorted(shas.items()))), lambda: read(kept, str(where)))
    return catalog


def _roms(config: Config) -> dict[str, str]:
    return {v: r.rom_sha256 for v, r in api.snapshot(config).versions.items()}


def own(document: Json, roms: Mapping[str, str]) -> Json | None:
    """The catalog limited to the versions whose ROM is the project's (the catalog's per-version rom_sha256 equals the
    project ROM's): a catalog read from another game's ROM (BattleTanx has a `us` too) must never name this one by
    address. None when no version matches; the record definitions stay with any matching version."""
    versions = {
        v: facts
        for v, facts in (document.get("versions") or {}).items()
        if facts.get("rom_sha256") and facts.get("rom_sha256") == roms.get(v)
    }
    return {**document, "versions": versions} if versions else None


def _kind(spelled: str) -> str:
    base = _ARRAY.sub("", spelled).strip()
    if "*" in base:
        return "p"
    if base.startswith(("struct ", "union ")):
        return "s"
    return "f" if base in _FLOAT else "i"


class _Sizes:
    """Type sizes of one version's definitions: scalars, pointers, arrays, records by tag and typedefs."""

    def __init__(self, tags: Mapping[str, int], typedefs: Mapping[str, str]) -> None:
        self.tags, self.typedefs = tags, typedefs

    def of(self, spelled: str, seen: tuple[str, ...] = ()) -> int | None:
        dims = [int(d) if d else 0 for d in _ARRAY.findall(spelled)]
        base = _ARRAY.sub("", spelled).strip()
        if "*" in base:
            one: int | None = 4
        elif base in _SCALAR:
            one = _SCALAR[base]
        elif base.startswith(("struct ", "union ")):
            one = self.tags.get(base.split(" ", 1)[1])
        elif base.startswith("enum "):
            one = 4
        elif base in self.typedefs and base not in seen:
            one = self.of(self.typedefs[base], (*seen, base))
        else:
            one = None
        if one is None:
            return None
        for d in dims:
            one *= d
        return one


def _slot(row: Json, sizes: _Sizes, end: int) -> Slot:
    spelled = str(row["type"])
    bits = int(row.get("bits", 0))
    if bits:
        size = bits // 8 if bits % 8 == 0 and not row.get("bit") else 0
    else:
        found = sizes.of(spelled)
        size = found if found is not None else max(end - int(row["offset"]), 0)
    return Slot(str(row["name"]), int(row["offset"]), size, _kind(spelled), spelled, bits)


def _where(entry: Json) -> tuple[str, int]:
    version = min(entry.get("where", {}) or {"": [0, 0]})
    return version, int((entry.get("where", {}).get(version) or [0])[0])


def read(document: Json, source: str = "") -> Catalog:
    """A catalog document as records, functions and globals. Records are keyed by the typedef that names their tag
    (an untagged record has no other spelling); one with no typedef keeps `struct tag`."""
    if document.get("schema") != 1 or not isinstance(document.get("types"), dict):
        raise state.stop("recipe.debug", f"{source or 'the debug catalog'} is not a schema-1 debug catalog")
    entries = [(name, e) for name, rows in sorted(document["types"].items()) for e in rows]
    tags: dict[str, int] = {}
    typedefs: dict[str, str] = {}
    aliases: dict[str, set[str]] = {}
    for name, entry in entries:
        d = entry["def"]
        if d["kind"] in ("struct", "union"):
            tags.setdefault(name, int(d["size"]))
        elif d["kind"] == "typedef":
            typedefs.setdefault(name, str(d["type"]))
            if re.fullmatch(r"(?:struct|union) [\w.]+", str(d["type"])):
                aliases.setdefault(str(d["type"]).split(" ", 1)[1], set()).add(name)
    sizes = _Sizes(tags, typedefs)
    records: list[Record] = []
    for name, entry in entries:
        d = entry["def"]
        if d["kind"] not in ("struct", "union") or not d.get("fields"):
            continue
        rows = sorted(d["fields"], key=lambda r: int(r["offset"]))
        ends = [int(r["offset"]) for r in rows[1:]] + [int(d["size"])]
        slots = tuple(_slot(r, sizes, end) for r, end in zip(rows, ends, strict=True))
        named = sorted(aliases.get(name, ()))
        spelled = named[0] if len(named) == 1 else name if not name.startswith(".") else ""
        if not spelled:
            continue  # an untagged record no single typedef names: C cannot spell it
        version, rom = _where(entry)
        records.append(Record(spelled, name, d["kind"] == "union", int(d["size"]), slots, f"{version} rom {rom:#x}"))
    functions: list[Function] = []
    globals_: list[Function] = []
    for version, facts in sorted(document.get("versions", {}).items()):
        for stream in facts.get("streams", ()):
            live = stream.get("dialect") == "sym"  # an object file's values are section offsets
            for row in stream.get("functions", ()):
                vram = int(row["address"]) | KSEG0 if live else None
                params = tuple(str(p.get("name", "")) for p in row.get("params", ()))
                functions.append(
                    Function(row["name"], version, vram, row.get("file", ""), row.get("line", 0), row["frame"], params)
                )
            for row in stream.get("globals", ()):
                if live and not str(row.get("type", "")).endswith("()"):  # a function's global row repeats it
                    globals_.append(
                        Function(
                            row["global"], version, int(row["address"]) | KSEG0, "", 0, 0, (), bool(row.get("static"))
                        )
                    )
    return Catalog(source, tuple(records), tuple(functions), tuple(globals_))


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class Match:
    """A project record and an original record its size and members agree with."""

    layout: cparse.Layout
    record: Record
    fields: tuple[tuple[str, str, int], ...]  # (project member, original member or member path, offset), all members
    agreed: int  # project members that start and end where an original top-level member does
    total: int  # project members
    weight: float  # how much the agreement says: odd sizes and offsets, floats and pointers say more than words

    def evidence(self) -> str:
        return (
            f"debug-type {self.record.name}: size {self.record.size:#x}, {self.total} members at original offsets "
            f"and sizes, {self.agreed} of them whole members, weight {self.weight:g} ({self.record.where})"
        )


_LEAVES_DEPTH = 4


def leaves(record: Record, by_tag: Mapping[str, Record], depth: int = 0) -> list[tuple[int, int, str, str]]:
    """(offset, size, kind, path) of every member and, below a record member or an array of scalars, of every
    element: a project that wrote a vector as three floats or an array as words still agrees with the original."""
    out: list[tuple[int, int, str, str]] = []
    for slot in record.slots:
        out.append((slot.offset, slot.size, slot.kind, slot.name))
        base = _ARRAY.sub("", slot.type).strip()
        dims = [int(d) if d else 0 for d in _ARRAY.findall(slot.type)]
        count = 1
        for d in dims:
            count *= d
        if depth < _LEAVES_DEPTH and slot.kind == "s" and (inner := by_tag.get(base.split(" ", 1)[-1])) and inner.size:
            for i in range(min(count, 64) if dims else 1):
                at = slot.offset + i * inner.size
                index = f"[{i}]" if dims else ""
                out += [(at + o, z, k, f"{slot.name}{index}.{p}") for o, z, k, p in leaves(inner, by_tag, depth + 1)]
        elif dims and count and slot.size and slot.kind != "s":
            one = slot.size // count
            out += [(slot.offset + i * one, one, slot.kind, f"{slot.name}[{i}]") for i in range(min(count, 256))]
    return out


def _weight(field: cparse.Field, kind: str) -> float:
    """What one agreeing member says: a float or pointer member, an odd width or an unaligned offset is rare
    by chance; a word at a word offset is what any record has."""
    width = field.size * field.count
    if (field.kind == "f" and kind == "f") or (field.kind == "p" and kind == "p"):
        say = 2.0
    elif field.kind == "s":
        say = 2.0 + (width > 8)
    elif field.count > 1 or width in (1, 2):
        say = 1.5
    else:
        say = 1.0 if kind == field.kind else 0.5
    return say + 0.5 * bool(field.offset % 4)


def _fits(kind: str, other: str, width: int) -> bool:
    """Whether a project member of kind letter `kind` may be an original member of kind `other`: floats are floats;
    an integer may stand for a pointer, an enum or a record (early decompiled code often has it so), and a pointer for
    a word."""
    if kind == "f" or other == "f":
        return kind == other
    if kind == "s":
        return other == "s"
    if kind == "p":
        return other == "p" or (other == "i" and width == 4)
    return True


def _members(layout: cparse.Layout) -> list[cparse.Field]:
    return sorted((*layout.fields, *layout.records), key=lambda f: (f.offset, f.name))


def agree(layout: cparse.Layout, record: Record, by_tag: Mapping[str, Record] | None = None) -> Match | None:
    """The match of a project record with an original one: same size and kind, and every project member starts and
    ends where an original member or element does, of a compatible kind; None at the first that does not."""
    if layout.size != record.size or layout.union != record.union or not layout.size:
        return None
    every = leaves(record, by_tag or {})
    at: dict[tuple[int, int], list[tuple[str, str, bool]]] = {}
    for offset, size, kind, name in every:
        at.setdefault((offset, size), []).append((kind, name, "." not in name and "[" not in name))
    fields: list[tuple[str, str, int]] = []
    agreed, weight = 0, 0.0
    members = _members(layout)
    for field in members:
        width = field.size * field.count
        found = [c for c in at.get((field.offset, width), ()) if _fits(field.kind, c[0], width)]
        if not found:
            return None
        top = [c for c in found if c[2]]
        best = top[0] if top else found[0]
        if record.union and len(top) > 1:
            best = ("", "", False)  # two union members of one shape: the member is not known
        fields.append((field.name, best[1], field.offset))
        agreed += bool(top) and bool(best[1])
        weight += _weight(field, best[0] or found[0][0])
    return Match(layout, record, tuple(fields), agreed, len(members), weight)


MIN_WEIGHT = 8.0  # the least weight a proposal needs: four floats, or eight words, agree


def matches(layouts: Iterable[cparse.Layout], catalog: Catalog) -> dict[str, list[Match]]:
    """Per project record (by name), its agreeing original records of at least MIN_FIELDS members, the heaviest
    first; one original name with several definitions counts once, by its heaviest."""
    by_size: dict[int, list[Record]] = {}
    by_tag = {r.tag: r for r in catalog.records}
    for record in catalog.records:
        by_size.setdefault(record.size, []).append(record)
    out: dict[str, list[Match]] = {}
    for layout in layouts:
        if layout.inner:
            continue
        found: dict[str, Match] = {}
        for record in by_size.get(layout.size, ()):
            got = agree(layout, record, by_tag)
            if got and got.total >= MIN_FIELDS and (record.name not in found or got.weight > found[record.name].weight):
                found[record.name] = got
        if found:
            out[layout.name] = sorted(found.values(), key=lambda m: (-m.weight, -m.agreed, m.record.name))
    return out


def unique(found: Sequence[Match]) -> Match | None:
    """The one original record a project record agrees with, when it weighs at least MIN_WEIGHT and no other
    original record agrees with it at all."""
    if len(found) != 1 or found[0].weight < MIN_WEIGHT:
        return None
    return found[0]


def spelled_name(name: str) -> str:
    """The identifier of a project record name (`struct Tag` -> Tag)."""
    return name.split(" ", 1)[1] if name.startswith(("struct ", "union ")) else name


def tag_of(record: Record) -> str:
    return "" if record.tag.startswith(".") else record.tag


_PLACEHOLDER = re.compile(r"^(?:pad\w*|unk(?:nown)?_?[0-9A-Fa-f]*|field_?[0-9A-Fa-f]+|_[0-9A-Fa-f]+|f[0-9A-F]+)$", re.I)
_INVENTED = re.compile(r"\d")  # a record a tool or lane named by an address, offset, size or counter


def invented(layout: cparse.Layout) -> bool:
    """Whether a project record's name or members were invented here: its name carries a number (an address, an
    offset, a size, a view counter: Block40, Shape_func_8020D364_de_2), or it is a padding view (pad arrays at least
    half its bytes, at most three named members), or every member is an offset placeholder (unk4, field_8)."""
    padded = sum(p.size * p.count for p in layout.pads)
    members = _members(layout)
    return (
        bool(_INVENTED.search(spelled_name(layout.name)))
        or (2 * padded >= layout.size and len(members) <= 3)
        or bool(members and all(_PLACEHOLDER.match(f.name) for f in members))
    )


def strong(match: Match) -> bool:
    """A match a move may follow without a reader: every project member is a whole original member, pointers stay
    pointers, at least three members, weight at least MIN_WEIGHT."""
    if match.agreed != match.total or match.total < 3 or match.weight < MIN_WEIGHT:
        return False
    slots = {s.offset: s for s in match.record.slots}
    return all(f.kind != "p" or slots[f.offset].kind == "p" for f in _members(match.layout) if f.offset in slots)


def whole(match: Match) -> bool:
    """A match a record named here may follow too: strong, at least six members, weight at least 15 (words alone say
    little: six floats, or a dozen words with pointers, say it)."""
    return strong(match) and match.total >= 6 and match.weight >= 15.0


_TYPEDEF_OF = r"typedef\s+(?:struct|union)\s+(\w+)\s*(?:\{{|{alias}\s*;)"


def tag_in(text: str, alias: str) -> str:
    """The tag a header's typedef gives the record `alias` (`typedef struct Tag Alias;`, `typedef struct Tag {`)."""
    for found in re.finditer(_TYPEDEF_OF.format(alias=re.escape(alias)), text):
        if found[0].endswith("{"):
            end = cparse.close(text, found.end() - 1)
            if not re.match(rf"\s*{re.escape(alias)}\s*;", text[end + 1 :]):
                continue
        return found[1]
    return ""


@dataclass(frozen=True)
class Plan:
    """The type moves of one pass and what it found: `proposals` are every unique match (moved or not, with why)."""

    moves: tuple[Json, ...]
    proposals: tuple[Json, ...]


def plan(
    layouts: Sequence[cparse.Layout], catalog: Catalog, identifiers: Iterable[str], read: Any, scope: Any = None
) -> Plan:
    """Type moves for the invented project records that strongly match exactly one original record no other project
    record matches: the record takes the original typedef name, and its tag the original tag. `identifiers` are the
    words the project already spells (an original name one of them holds is refused); `read(path)` gives a header's
    text, for the tag a typedef names. A `scope` (a set of words) keeps only the records it spells by name or tag;
    every record still counts toward the matches and member spellings."""
    found = matches(layouts, catalog)
    by_name = {layout.name: layout for layout in layouts}
    taken = set(identifiers)
    claims: dict[str, list[str]] = {}
    for name, ms in found.items():
        if (one := unique(ms)) is not None:
            claims.setdefault(one.record.name, []).append(name)
    verdicts: dict[str, tuple[Match, str, str]] = {}  # per project record: its match, its tag, why it does not move
    for name, ms in sorted(found.items()):
        one = unique(ms)
        if one is None:
            continue
        layout, record, current = by_name[name], one.record, spelled_name(name)
        tag = tag_in(read(layout.path) or "", current) if not name.startswith(("struct ", "union ")) else current
        if current in (record.name, tag_of(record)) or tag == tag_of(record):
            why = "already the original name"
        elif not (invented(layout) or whole(one)):
            why = "named here, not invented: a reader decides"
        elif not strong(one):
            why = "not every member is a whole original member, or a pointer is not one"
        else:
            why = ""
        verdicts[name] = (one, tag, why)
    used = set(taken)  # the names the project spells before this pass moves any
    moves: list[Json] = []
    fields: list[Json] = []
    merges: list[Json] = []
    proposals: list[Json] = []
    for name, (one, tag, why) in verdicts.items():
        layout, record, current = by_name[name], one.record, spelled_name(name)
        if scope is not None and not {current, tag} & scope:
            continue
        row: dict[str, Any] = {
            "record": name,
            "header": layout.path,
            "original": record.name,
            "tag": tag_of(record),
            "size": record.size,
            "members": [{"current": c, "original": o, "offset": at} for c, o, at in one.fields],
            "evidence": one.evidence(),
        }
        group = [n for n in sorted(claims[record.name]) if verdicts[n][2] in ("", "already the original name")]
        free = not (record.name in used or (tag_of(record) and tag_of(record) in used))
        keep = next((n for n in group if verdicts[n][2]), next((n for n in group if not verdicts[n][2] and free), ""))
        merged = not why and keep not in ("", name)
        if not why and not merged and not free:
            why = f"{record.name} is already a name in the project"
        if merged and (stuck := next(iter(_stuck(one)), None)):
            why, merged = f"member {stuck[0]} has no original name it can take ({stuck[1]})", False
        row["moved"], row["why"] = not why, why or ("merged into " + spelled_name(keep) if merged else "")
        proposals.append(row)
        original = why == "already the original name" and strong(one)
        if why and not original:
            continue
        keyword = "union" if record.union else "struct"
        if not why and not merged:
            moves.append({"op": "type", "subject": current, "target": record.name})
            if tag and tag != current:
                moves.append({"op": "type", "subject": tag, "target": tag_of(record) or record.name})
            elif tag_of(record):
                moves.append(
                    {"op": "type", "subject": f"{keyword} {record.name}", "target": f"{keyword} {tag_of(record)}"}
                )
            taken |= {record.name, tag_of(record)}
        spelled = name.startswith(("struct ", "union "))
        final_tag = (tag_of(record) or record.name) if not why and not merged else tag
        alias = record.name if not why and not merged and not spelled else ("" if spelled else current)
        row["renamed"] = renamed = _member_moves(one, layout.path, final_tag, alias)
        fields += renamed
        if merged:
            into, (_, into_tag, _) = by_name[keep], verdicts[keep]
            kept = keep.startswith(("struct ", "union "))
            kept_tag = into_tag if verdicts[keep][2] else tag_of(record) or record.name
            kept_alias = "" if kept else record.name if not verdicts[keep][2] else spelled_name(keep)
            target = kept_alias or f"{keyword} {kept_tag}"
            merge: dict[str, Any] = {"op": "merge", "subject": current, "target": target, "path": layout.path}
            merges.append(
                merge
                | {"tag": tag, "alias": alias, "into_path": into.path, "into_tag": kept_tag, "into_alias": kept_alias}
            )
    return Plan(tuple([*moves, *fields, *merges]), tuple(proposals))


def _stuck(match: Match) -> list[tuple[str, str]]:
    """The members whose original name a rename cannot give: a path into an inner record, or a name the record has."""
    have = {f.name for f in (*match.layout.fields, *match.layout.records, *match.layout.pads)}
    return [
        (current, original)
        for current, original, _ in match.fields
        if current != original and (not re.fullmatch(r"[A-Za-z_]\w*", original) or original in have)
    ]


def _member_moves(match: Match, path: str, tag: str, alias: str) -> list[Json]:
    """Member renames of a matched record: each whole member takes its original name, unless the record has that name.
    A spelling other records have too is renamed by the typed accesses of the build, or not at all."""
    have = {f.name for f in (*match.layout.fields, *match.layout.records, *match.layout.pads)}
    return [
        {"op": "field", "subject": current, "target": original, "path": path, "tag": tag, "alias": alias}
        for current, original, _ in match.fields
        if current != original and re.fullmatch(r"[A-Za-z_]\w*", original) and original not in have
    ]


# ---------------------------------------------------------------- functions and files


def placed(catalog: Catalog, name: str, placements: Iterable[Mapping[str, Any]]) -> Function | None:
    """The record of `name` whose version and address are one of these placements, or None."""
    at = {(p["version"], p["vram"]) for p in placements}
    return next((f for f in catalog.named(name) if f.vram is not None and (f.version, f.vram) in at), None)


def rows(catalog: Catalog, members: Iterable[Mapping[str, Any]]) -> list[Json]:
    """`recipe name submit` rows for every function and global a record places where a member is whose name is
    another: the original name, `debug NAME` evidence, and the record's source line as the doc."""
    out: list[Json] = []
    for member in members:
        for p in member["placements"]:
            for one in (*catalog.functions, *catalog.globals):
                if one.vram == p["vram"] and one.version == p["version"] and one.name != member["name"]:
                    function = member["kind"] == "function"
                    if function != (one in catalog.functions):
                        continue
                    kind = "function" if function else "static" if one.static else "global"
                    where = (
                        f"{one.file.replace(chr(92), '/').rsplit('/', 1)[-1]}:{one.line}" if one.file else one.version
                    )
                    doc = f"The original {one.name} ({where})."
                    out.append(
                        {
                            "target": member["name"],
                            "kind": kind,
                            "name": one.name,
                            "evidence": [f"debug {one.name}"],
                            "doc": doc,
                        }
                    )
    return list({json.dumps(r, sort_keys=True): r for r in out}.values())


def root_of(files: Iterable[str]) -> str:
    """The folder every source path of the catalog shares (`C:/PROJECTS/GAME/SOURCE/`), in forward slashes."""
    paths = [PurePosixPath(f.replace("\\", "/")).parent.parts for f in files if f]
    if not paths:
        return ""
    common = list(paths[0])
    for parts in paths[1:]:
        while list(parts[: len(common)]) != common:
            common.pop()
    return "/".join(common).rstrip("/") + "/" if common else ""


def boundaries(catalog: Catalog, doc: Json) -> list[Json]:
    """Module evidence rows for `recipe structure`: per source file, its first and last function in the .text order
    of the version whose stream placed them (by address, else by member name), with the path below the catalog's
    common source folder."""
    by_place = {
        (p["version"], p["vram"]): m["name"]
        for m in doc["members"]
        if m["kind"] == "function"
        for p in m["placements"]
        if p["section"] == ".text"
    }
    base = root_of(f.file for f in catalog.functions if f.vram is not None)
    at = {m["name"]: p["vram"] for m in doc["members"] if m["kind"] == "function" for p in m["placements"][:1]}
    files: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for f in catalog.functions:  # placed by address, else by the name a member already has (a function named from it)
        member = by_place.get((f.version, f.vram)) if f.vram is not None else None
        if f.file and (member := member or (f.name if f.name in at else None)):
            files.setdefault((f.version, f.file), []).append((f.vram if f.vram is not None else at[member], member))
    rows: list[Json] = []
    seen: set[str] = set()
    for (_, file), held in sorted(files.items()):
        spelled = file.replace("\\", "/")
        source = spelled[len(base) :] if base and spelled.startswith(base) else spelled.rsplit("/", 1)[-1]
        if source in seen:
            continue
        seen.add(source)
        held.sort()
        rows.append({"first": held[0][1], "last": held[-1][1], "source": source, "functions": len(held)})
    return rows
