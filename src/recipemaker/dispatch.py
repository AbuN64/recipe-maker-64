"""Dispatch groups: the functions one indirect call reaches with the same object (the entries of one data table
column, joined across the tables holding them, and the callees they pass that object on to as it is) and the one
record their accesses through it show. A record is built only from what the code reads and writes: each member is the
offset and width some group function accesses, the rest is placeholder bytes, and two functions disagreeing at an
offset refuse the record, naming both."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from unbake import api
from unbake.contracts import Snapshot

from recipemaker import index, world

_LINE = re.compile(r"^\s*/\*[^*]*\*/\s+([a-z][\w.]*)\s*(.*?)\s*$")
_BOUNDARY = re.compile(r"^\s*(?:\.L\w+:|glabel\s|\w+:)")
_DATA_WORD = re.compile(r"/\*\s*\w+\s+([0-9A-Fa-f]{8})\s+\w+\s*\*/\s*\.word\s+(\w+)")
_DLABEL = re.compile(r"^\s*dlabel\s+(\w+)", re.M)
_MEMORY = re.compile(r"(-?0x[0-9A-Fa-f]+|-?\d+)\((\$\w+)\)")
_MOVES = ("addu", "or", "move", "daddu")
# mnemonic -> (width, kind, sign) of the value it moves; a store has no sign
_LOADS = {
    "lb": (1, "i", "s"),
    "lbu": (1, "i", "u"),
    "lh": (2, "i", "s"),
    "lhu": (2, "i", "u"),
    "lw": (4, "i", "s"),
    "lwu": (4, "i", "u"),
    "ld": (8, "i", "s"),
    "lwc1": (4, "f", ""),
    "ldc1": (8, "f", ""),
}
_STORES = {
    "sb": (1, "i", ""),
    "sh": (2, "i", ""),
    "sw": (4, "i", ""),
    "sd": (8, "i", ""),
    "swc1": (4, "f", ""),
    "sdc1": (8, "f", ""),
}
_UNALIGNED = frozenset({"lwl", "lwr", "swl", "swr", "ldl", "ldr", "sdl", "sdr"})
_NO_WRITE = frozenset({"mtc1", "dmtc1", "ctc1", "mult", "multu", "div", "divu", "dmult", "dmultu", "ddiv", "ddivu"})
_KEPT = re.compile(r"\$(?:s[0-7]|fp|sp|gp)")  # registers a call leaves as they were
_TYPES = {
    ("i", 1, "s"): "s8",
    ("i", 1, "u"): "u8",
    ("i", 2, "s"): "s16",
    ("i", 2, "u"): "u16",
    ("i", 4, "s"): "s32",
    ("i", 4, "u"): "u32",
    ("i", 8, "s"): "s64",
    ("i", 8, "u"): "u64",
    ("f", 4, ""): "f32",
    ("f", 8, ""): "f64",
    ("p", 4, ""): "void *",
}
_LIMIT = 1024  # functions in one group: past that a walk is runaway, never one object (disagreements refuse first)

Row = tuple[str, list[str]] | None
Node = tuple[str, int]  # (function, parameter position)


@dataclass(frozen=True)
class Access:
    offset: int
    width: int  # 0: an unaligned word access, which no member can hold
    kind: str  # "i" integer, "f" float, "p" pointer
    sign: str  # "s" | "u" | "" (a store, or a float or pointer)
    function: str


@dataclass(frozen=True)
class Flow:
    accesses: tuple[Access, ...]
    passes: tuple[Node, ...]  # the callee parameters the object reaches as it is
    pointees: tuple[tuple[int, Access], ...] = ()  # (member offset, access) through a pointer member's value


@dataclass(frozen=True)
class Group:
    name: str  # neutral, from the address of the first table column entry: `Obj80137910`, `Obj80137910Arg1`
    nodes: tuple[Node, ...]  # the table entries first, then the callees, each once


@dataclass(frozen=True)
class Record:
    name: str
    fields: tuple[tuple[int, int, str], ...]  # (offset, width, C type) by offset

    def text(self) -> str:
        """The record's definition: each member named by its offset, the bytes between as placeholder arrays."""
        lines, at = [], 0
        for offset, width, kind in self.fields:
            if offset > at:
                lines.append(f"    u8 unk_{at:X}[0x{offset - at:X}];")
            lines.append(f"    {kind}{'' if kind.endswith('*') else ' '}unk{offset:X};")
            at = offset + width
        return f"typedef struct {self.name} {{\n" + "\n".join(lines) + f"\n}} {self.name};\n"


def retarget(made: Record, offset: int, pointee: str) -> Record:
    """The record with its `void *` member at `offset` pointing at the record `pointee`."""
    fields = tuple((o, w, f"{pointee} *" if o == offset and t == "void *" else t) for o, w, t in made.fields)
    return Record(made.name, fields)


def instructions(text: str) -> list[Row]:
    """Each line of an asm function as (mnemonic, operands); None for a label or the function's start."""
    out: list[Row] = []
    for line in text.splitlines():
        if _BOUNDARY.match(line):
            out.append(None)
        elif found := _LINE.match(line):
            out.append((found[1], [x.strip() for x in found[2].split(",")] if found[2] else []))
    return out


def tables(snapshot: Snapshot) -> tuple[dict[str, list[tuple[str, tuple[str, ...], int]]], dict[str, list[str]]]:
    """Per function the data tables of the names version that hold it, each with the functions in the same column
    of that table (the entries at its offset modulo the table's stride) and the address of the column's first entry:
    one indirect call reaches them all. And per symbol the tables holding a word of it, so a table only another table
    points at is reached through that one."""
    config = snapshot.config
    functions = {n for n, m in world.of(snapshot).members.items() if m.kind == "function"}
    out: dict[str, list[tuple[str, tuple[str, ...], int]]] = {}
    holders: dict[str, list[str]] = {}
    for path in sorted((config.project.root / "asm" / config.project.names_from / "data").rglob("*.s")):
        text = path.read_text(errors="replace")
        marks = [(m.start(), m[1]) for m in _DLABEL.finditer(text)]
        for (start, table), (end, _) in zip(marks, [*marks[1:], (len(text), "")], strict=False):
            every = [(int(a, 16), w) for a, w in _DATA_WORD.findall(text[start:end])]
            for _, word in every:
                holders.setdefault(word, []).append(table)
            words = [(a, w) for a, w in every if w in functions]
            if not words:
                continue
            stride = math.gcd(*[b - words[0][0] for b, _ in words[1:]]) or 4
            for at, name in words:
                same = [(b, w) for b, w in words if (b - at) % stride == 0]
                column = tuple(dict.fromkeys(w for _, w in same if w != name))
                out.setdefault(name, []).append((table, column, min(b for b, _ in same)))
    return out, holders


def _written(name: str, ops: Sequence[str]) -> str:
    """The register an instruction writes, "" when it writes none."""
    if not ops or not ops[0].startswith("$") or name in _NO_WRITE or name in _STORES or name in _UNALIGNED:
        return ""
    return "" if re.match(r"b|j|t[enlg]", name) else ops[0]


def flow(code: Sequence[Row], position: int, function: str = "") -> Flow:
    """The accesses a function makes through its parameter `position` and the callee parameters it passes it to, by
    following the registers that hold it from the entry on. A register written with anything else, or not kept by a
    call, stops holding it. A word loaded through the object and then used as an address is a pointer member."""
    bases = {f"$a{position}"}
    loaded: dict[str, int] = {}  # register -> the access that loaded it from the object
    accesses: list[Access] = []
    passes: list[Node] = []
    pointees: list[tuple[int, Access]] = []

    def step(row: tuple[str, list[str]]) -> None:
        name, ops = row
        memory = _MEMORY.fullmatch(ops[1]) if len(ops) == 2 else None
        if memory and (name in _LOADS or name in _STORES or name in _UNALIGNED):
            register = memory[2]
            if register in bases:
                width, kind, sign = (0, "i", "") if name in _UNALIGNED else {**_LOADS, **_STORES}[name]
                accesses.append(Access(int(memory[1], 0), width, kind, sign, function))
            elif register in loaded:
                at = loaded[register]
                accesses[at] = replace(accesses[at], kind="p", sign="")
                if name not in _UNALIGNED:
                    width, kind, sign = {**_LOADS, **_STORES}[name]
                    pointees.append((accesses[at].offset, Access(int(memory[1], 0), width, kind, sign, function)))
        target = _written(name, ops)
        if not target:
            return
        copied = (name == "move" and len(ops) == 2 and ops[1] in bases) or (
            name in _MOVES
            and len(ops) == 3
            and "$zero" in ops[1:]
            and (ops[2] if ops[1] == "$zero" else ops[1]) in bases
        )
        through = bool(memory) and memory[2] in bases  # type: ignore[index]
        bases.discard(target)
        loaded.pop(target, None)
        if copied:
            bases.add(target)
        elif name == "lw" and through:
            loaded[target] = len(accesses) - 1

    at = 0
    while at < len(code):
        row = code[at]
        at += 1
        if row is None:
            continue
        if row[0] in ("jal", "jalr"):
            slot = code[at] if at < len(code) else None
            if slot is not None:
                step(slot)  # the delay slot runs before the call
                at += 1
            if row[0] == "jal" and row[1]:
                passes += [(row[1][0], k) for k in range(4) if f"$a{k}" in bases]
            bases.intersection_update(r for r in bases if _KEPT.fullmatch(r))
            loaded = {r: i for r, i in loaded.items() if _KEPT.fullmatch(r)}
            continue
        step(row)
    return Flow(tuple(accesses), tuple(dict.fromkeys(passes)), tuple(pointees))


def _code(snapshot: Snapshot, name: str) -> list[Row]:
    def read() -> list[Row]:
        try:
            return instructions(index.asm(snapshot, name))
        except (KeyError, OSError):
            return []

    return api.memo(("recipe.dispatch.code", snapshot.digest, name), read)


def flows(snapshot: Snapshot, node: Node) -> Flow:
    name, position = node
    return api.memo(
        ("recipe.dispatch.flow", snapshot.digest, node), lambda: flow(_code(snapshot, name), position, name)
    )


def _columns(snapshot: Snapshot) -> dict[str, list[tuple[str, tuple[str, ...], int]]]:
    return api.memo(("recipe.tables", snapshot.digest), lambda: tables(snapshot))[0]


def _core(snapshot: Snapshot, seed: str) -> tuple[frozenset[str], int]:
    """The table entries joined to `seed` through shared columns of any table, and the lowest column address."""
    columns = _columns(snapshot)
    seen, todo, first = {seed}, [seed], 0xFFFFFFFF
    while todo:
        for _, column, start in columns.get(todo.pop(), []):
            first = min(first, start)
            for other in column:
                if other not in seen:
                    seen.add(other)
                    todo.append(other)
    return frozenset(seen), first


def _backward(snapshot: Snapshot, node: Node, depth: int = 0, seen: frozenset[Node] = frozenset()) -> set[Node]:
    """The table entries whose object reaches `node` through callers passing it on as it is."""
    name = node[0]
    if _columns(snapshot).get(name):
        return {node}
    if depth >= 4:
        return set()
    out: set[Node] = set()
    for caller in dict.fromkeys(index.get(snapshot).callers.get(name, ())):
        for k in range(4):
            here = (caller, k)
            if here not in seen and node in flows(snapshot, here).passes:
                out |= _backward(snapshot, here, depth + 1, seen | {node})
    return out


def group(snapshot: Snapshot, member: str, position: int) -> Group | str | None:
    """The dispatch group of parameter `position` of `member`: the table entries joined through shared columns, the
    callees they pass the object on to as it is, and the table entries of every other group passing its object to
    one of those callees (one parameter has one type, so the objects are one). None when no table reaches it, a reason
    when a helper joins more functions than one object plausibly has."""
    columns = _columns(snapshot)
    pending = sorted(_backward(snapshot, (member, position)))
    if not pending:
        return None
    nodes: list[Node] = []
    seen: set[Node] = set()
    starts: list[tuple[int, int]] = []
    while pending:
        node = pending.pop(0)
        if node in seen:
            continue
        added = [node]
        if columns.get(node[0]):
            core, first = _core(snapshot, node[0])
            starts.append((first, node[1]))
            added = [(n, node[1]) for n in sorted(core) if (n, node[1]) not in seen]
        for one in added:
            seen.add(one)
            nodes.append(one)
            for passed in flows(snapshot, one).passes:
                if passed not in seen:
                    pending += [passed, *sorted(_backward(snapshot, passed))]
        if len(nodes) > _LIMIT:
            return f"its group passes the object on to more than {_LIMIT} functions"
    first, at = min(starts)
    return Group(_name(first, at), tuple(nodes))


def pointee(snapshot: Snapshot, found: Group, offset: int) -> list[Access]:
    """The accesses every group function makes through the pointer member at `offset` of the object."""
    return [a for node in found.nodes for at, a in flows(snapshot, node).pointees if at == offset]


def global_object(snapshot: Snapshot, name: str) -> list[Access]:
    """The accesses of every function reading or writing the global `name` directly, by offset into it."""
    out = []
    for function, mnemonic, offset in index.get(snapshot).uses.get(name, ()):
        if mnemonic in _UNALIGNED:
            out.append(Access(offset, 0, "i", "", function))
        elif mnemonic in _LOADS or mnemonic in _STORES:
            out.append(Access(offset, *{**_LOADS, **_STORES}[mnemonic], function))
    return out


def global_pointee(snapshot: Snapshot, name: str) -> list[Access]:
    """The accesses every function makes through the value of the pointer global `name`: a register loaded from it
    holds the pointee until it is written or a call does not keep it."""
    word = re.compile(rf"%lo\({re.escape(name)}\)\(\$\w+\)")
    out = []
    for function in dict.fromkeys(f for f, _, _ in index.get(snapshot).uses.get(name, ())):
        held: set[str] = set()
        for row in _code(snapshot, function):
            if row is None:
                continue
            mnemonic, ops = row
            memory = _MEMORY.fullmatch(ops[1]) if len(ops) == 2 else None
            if memory and memory[2] in held and (mnemonic in _LOADS or mnemonic in _STORES):
                out.append(Access(int(memory[1], 0), *{**_LOADS, **_STORES}[mnemonic], function))
            if mnemonic in ("jal", "jalr"):
                held = {r for r in held if _KEPT.fullmatch(r)}
                continue
            if target := _written(mnemonic, ops):
                held.discard(target)
                if mnemonic == "lw" and word.fullmatch(ops[1]):
                    held.add(target)
    return out


def data_name(symbol: str, address: int, pointee: bool) -> str:
    """The record of a data symbol (`Data800D4360`), or of what a pointer symbol points at (`Data800D4360Ptr`), named
    from the symbol so every user shares it: a neutral `D_[auto_]<hex>` symbol by its address, a named one by its
    words in PascalCase (`g_menuState` -> `DataMenuState`). The `Data` prefix keeps it a type name that no function or
    variable carries, and a rename of the symbol renames its record the same way."""
    neutral = re.fullmatch(r"D_(?:auto_)?[0-9A-Fa-f]+", symbol)
    if neutral or not symbol:
        stem = f"{address:08X}"
    else:
        words = [w for w in re.split(r"_+", re.sub(r"^(?:g|s|D)_", "", symbol)) if w]
        stem = "".join(w[:1].upper() + w[1:] for w in words if w.isalnum()) or f"{address:08X}"
    return f"Data{stem}" + ("Ptr" if pointee else "")


def _name(first: int, position: int) -> str:
    return f"Obj{first:08X}" + (f"Arg{position}" if position else "")


def _ctype(kind: str, width: int, signs: set[str]) -> str:
    sign = "u" if signs == {"u"} else "s" if kind == "i" else ""
    return _TYPES.get((kind, width, sign), "")


def record(name: str, accesses: Iterable[Access], spelled: Mapping[int, str] | None = None) -> Record | str:
    """The record holding every access, or why none can: an unaligned or negative access, two widths or kinds at one
    offset, signed and unsigned loads of one member, or members that overlap. Each refusal names the functions.
    `spelled` gives a member no load gives a sign the C type a draft already spells for it."""
    spelled = spelled or {}
    by_offset: dict[int, list[Access]] = {}
    for access in accesses:
        if access.width == 0:
            return f"{access.function} reads 0x{access.offset:X} by unaligned word access"
        if access.offset < 0:
            return f"{access.function} reads below the object (offset {access.offset})"
        by_offset.setdefault(access.offset, []).append(access)
    fields: list[tuple[int, int, str]] = []
    end, last = 0, ""
    for offset in sorted(by_offset):
        rows = by_offset[offset]
        widths = sorted({r.width for r in rows})
        if len(widths) > 1:
            a, b = (next(r for r in rows if r.width == w) for w in widths[:2])
            return f"{a.function} and {b.function} access 0x{offset:X} as {a.width} and {b.width} bytes"
        width = widths[0]
        kinds = {r.kind for r in rows}
        if "f" in kinds and len(kinds) > 1:
            a = next(r for r in rows if r.kind == "f")
            b = next(r for r in rows if r.kind != "f")
            return f"{a.function} reads 0x{offset:X} as a float and {b.function} as an integer"
        signs = {r.sign for r in rows if r.sign}
        if len(signs) > 1:
            a = next(r for r in rows if r.sign == "s")
            b = next(r for r in rows if r.sign == "u")
            return f"{a.function} loads 0x{offset:X} signed and {b.function} unsigned"
        given = spelled.get(offset, "")
        pointer = width == 4 and kinds <= {"i", "p"} and ("p" in kinds or given.endswith("*"))
        kind = "f" if "f" in kinds else "p" if pointer else "i"
        ctype = _ctype(kind, width, signs)
        if kind == "i" and not signs and _TYPES.get(("i", width, "u")) == given:
            ctype = given  # a member only stored to: the draft's spelling of it
        if not ctype or offset % min(width, 8):
            return f"{rows[0].function} accesses 0x{offset:X} ({width} bytes) where no member of that width can sit"
        if offset < end:
            return f"{last} and {rows[0].function} access overlapping bytes at 0x{offset:X}"
        fields.append((offset, width, ctype))
        end, last = offset + width, rows[0].function
    if not fields:
        return "no group function accesses the object"
    return Record(name, tuple(fields))


def header(snapshot: Snapshot, nodes: Sequence[Node]) -> str:
    """The project header every group function's unit reaches: of the headers included (transitively) by both each
    function's prototype header and its module header, the most specific one; "" when they share none."""
    found = index.get(snapshot)
    table = found.globals

    def closure(path: str) -> set[str]:
        seen, todo = set(), [path]
        while todo:
            at = todo.pop()
            if at in seen or at not in table:
                continue
            seen.add(at)
            todo += table[at][1]
        return seen

    common: set[str] | None = None
    for name in dict.fromkeys(n for n, _ in nodes):
        member = world.of(snapshot).members.get(name)
        module = world.of(snapshot).groups.get(member.group) if member else None
        declared = found.declarations.get(name)
        paths = [p for p in (declared[0] if declared else "", module.header if module else "") if p in table]
        for path in paths:
            common = closure(path) if common is None else common & closure(path)
    if not common:
        return ""
    sized = {p: len(closure(p)) for p in common if not p.startswith("include/sdk/")}
    return min(sized, key=lambda p: (-sized[p], p)) if sized else ""


def define(text: str, record_: Record, functions: Iterable[str] = ()) -> str:
    """The header text with the record defined before the first prototype of a group function (which may come to name
    it), else at its end, inside its include guard: after every type its members use. The text as it was when it
    defines a record of that name already."""
    if re.search(rf"\b(?:struct\s+{record_.name}\s*\{{|}}\s*{record_.name}\s*;)", text):
        return text
    names = "|".join(re.escape(n) for n in functions)
    first = re.search(rf"^[^\n#]*\b(?:{names})\s*\(", text, re.M) if names else None
    guard = re.search(r"^[ \t]*#[ \t]*endif\b[^\n]*\n?\s*\Z", text, re.M)
    at = first.start() if first else guard.start() if guard else len(text)
    if at and text[at - 1] != "\n":
        return text[:at] + "\n" + record_.text() + text[at:]
    return text[:at] + record_.text() + text[at:]
