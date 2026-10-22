"""Type items: a parameter typed `void *` whose object the code reaches by fixed offsets. One change set: a record built
from every observed access (the function's, the callees it passes the object to as it is, the functions handed the same
object, the members the module's other functions already typed), the prototype once in the unit's family header taking
it, every other copy of the declaration gone. The call sites are reported, not cast. Landing is `declare.run`'s."""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from unbake.contracts import Config, Json, Snapshot

from recipemaker import cparse, declare, declplan, dispatch, headers, host, index, ledger, records, rungs, state, world

BYTES = frozenset({"char", "u8", "s8", "void", "unsigned char", "signed char"})
MAX_NODES = 24  # callees followed from the function
SKIPS = ("needs-header-change", "type-unresolved")
_CALL = re.compile(r"(?<![\w.>&*])\b([A-Za-z_]\w*)\s*\(")
_HEAD = r"^[^\n;{}#]*\w\([^;{]*\)\s*\{$"


@dataclass
class Made:
    """The change set of one parameter: edits are full file texts."""

    item: str
    function: str
    position: int
    name: str = ""  # the record
    header: str = ""
    edits: dict[str, bytes] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)  # offset, width, type, name per member
    sites: list[str] = field(default_factory=list)  # every call and what it passes
    conflicts: list[str] = field(default_factory=list)  # calls passing another type: the compiler only warns
    shared: list[str] = field(default_factory=list)  # functions whose evidence went into the record
    risky: tuple[str, ...] = ()
    skip: str = ""


def item_name(function: str, position: int) -> str:
    return f"{function}.arg{position}"


def parse_item(name: str) -> tuple[str, int] | None:
    found = re.fullmatch(r"(\w+)\.arg(\d)", name)
    return (found[1], int(found[2])) if found else None


def _prototype(snapshot: Snapshot, function: str) -> tuple[str, list[str]] | None:
    row = index.get(snapshot).declarations.get(function)
    return declplan.split_signature(row[2].rstrip(";"), function) if row else None


def targets(snapshot: Snapshot, function: str) -> list[int]:
    """The parameters declared `void *` (or not at all) that the function's code reaches by offsets."""
    given = _prototype(snapshot, function)
    return [
        p
        for p in range(4)
        if (given[1][p].replace(" ", "") if given and p < len(given[1]) else "void*") in ("void*", "constvoid*")
        and len(dispatch.flows(snapshot, (function, p)).accesses) > 1
    ]


def _view(snapshot: Snapshot, function: str, position: int) -> list[dispatch.Access]:
    """The scalar members of the record type `function` declares for the parameter, as accesses."""
    kind = rungs._sibling_type(snapshot, function, position)
    owner = ledger._owner(ledger.owners(index.get(snapshot).identities), kind) if kind else None
    return [
        dispatch.Access(f.offset, f.size, f.kind if f.kind in "fp" else "i", "", function)
        for f in (owner.fields if owner else ())
        if f.count == 1 and f.size in (1, 2, 4, 8) and f.kind in "ifp"
    ]


def _accesses(snapshot: Snapshot, node: tuple[str, int]) -> list[dispatch.Access]:
    return list(dispatch.flows(snapshot, node).accesses) or _view(snapshot, *node)


def _closure(snapshot: Snapshot, function: str, position: int) -> tuple[list[dispatch.Access], list[str]]:
    """Every access through the object by the function and the callees it passes it to as it is."""
    todo, rows = [(function, position)], []
    seen: set[tuple[str, int]] = set()
    while todo and len(seen) < MAX_NODES:
        node = todo.pop(0)
        if node not in seen:
            seen.add(node)
            rows += _accesses(snapshot, node)
            todo += list(dispatch.flows(snapshot, node).passes)
    return rows, sorted({n for n, _ in seen} - {function})


def _calls(text: str, only: str = "") -> list[tuple[str, int, int, int]]:
    """(callee, start, end, position) of each argument of each call in `text`."""
    out = []
    for call in _CALL.finditer(text):
        if call[1] in ("if", "while", "for", "switch", "return", "sizeof") or (only and call[1] != only):
            continue
        parsed = declplan._arguments(text, call.end() - 1)
        out += [(call[1], a, b, n) for n, (a, b) in enumerate(parsed[0] if parsed else []) if text[a:b].strip()]
    return out


def _normal(snapshot: Snapshot, expression: str) -> str:
    """The object an argument names: no spaces, `&`, casts or `[0]`, and no trailing member at offset 0 of its record,
    so `&P->a[i]` and `P->a[i].first` are one object while `P->a[i].other` is another."""
    text = re.sub(r"\([^()]*\)", "", expression.replace(" ", "")).lstrip("&*").split("+")[0]
    scope, owner, _ = rungs._program(snapshot)
    while step := re.search(r"(?:\[0\]|\.(\w+))$", text):
        if step[1]:
            got = ledger.typed(re.sub(r"\[[^\]0-9][^\]]*\]", "[0]", text[: step.start()]), {}, scope, owner)
            identity = ledger._owner(owner, got[0]) if got and not got[1] else None
            one = ledger._named(identity, step[1]) if identity else None
            if one is None or one.offset:
                break
        text = text[: step.start()]
    return text


def _users(snapshot: Snapshot, name: str) -> list[str]:
    return [p for p in declare._users(snapshot, name) if headers.is_source(p) and snapshot.peek(p) is not None]


def _classify(snapshot: Snapshot, text: str, path: str, at: int, argument: str) -> str:
    """What an argument passes: `record NAME`, `bytes` (a char array or pointer, a void pointer) or `unknown`."""
    scope, owner, _ = rungs._program(snapshot)
    opened = list(re.finditer(_HEAD, text[:at], re.M))
    types = ledger.declared_types(text[opened[-1].start() : at] + ") {" if opened else "")
    mine = ledger.owners(ledger.Identity((v,), v.fields) for v in cparse._layouts({path: cparse.scan(text)}))
    scope, owner = ledger.Scope({**scope.globals, **ledger.globals_in(text, path)}, scope.addresses), {**owner, **mine}
    cast = re.match(r"\(\s*(?:const\s+)?(?:struct\s+)?(\w+(?:\s+\w+)?)\s*\*+\s*\)", argument)
    lvalue = re.sub(r"\[[^\]0-9][^\]]*\]", "[0]", argument.replace(" ", ""))
    got = None if cast else ledger.typed(lvalue.lstrip("&"), types, scope, owner)
    kind = cast[1] if cast else ""
    if got:
        element, stars, array = got
        if (lvalue.startswith("&") and not stars) or (array and not lvalue.startswith("&")) or stars == 1:
            kind = element
    if kind in BYTES:
        return "bytes"
    return f"record {kind}" if kind and ledger._owner(owner, kind) else "unknown"


def sites(snapshot: Snapshot, function: str, position: int) -> list[tuple[str, int, str, str]]:
    """(path, line, argument, what it passes) for every call of `function` in the landed sources."""
    out = []
    for path in _users(snapshot, function):
        text = declplan.overlay({}, snapshot.peek)(path) or ""
        for one in declplan.copies(text, function)[::-1]:
            text = text[: one.start] + " " * (one.end - one.start) + text[one.end :]
        for _, start, end, n in _calls(text, function):
            if n == position:
                argument = text[start:end].strip()
                out.append(
                    (path, text.count("\n", 0, start) + 1, argument, _classify(snapshot, text, path, start, argument))
                )
    return out


def _mates(snapshot: Snapshot, function: str, found: Sequence[tuple[str, int, str, str]]) -> list[tuple[str, int]]:
    """The other functions handed the same object, however a call spells its member or address."""
    roots = {_normal(snapshot, s[2]) for s in found if re.search(r"\w(?:->|\.)\w", s[2])}
    out: dict[tuple[str, int], None] = {}
    for root in roots:
        symbol = re.match(r"\w+", root)
        for path in _users(snapshot, symbol[0]) if symbol else []:
            text = (snapshot.peek(path) or b"").decode(errors="replace")
            for callee, start, end, n in _calls(text):
                if callee != function and _normal(snapshot, text[start:end]) == root:
                    out[(callee, n)] = None
    return [n for n in out if n[0] in index.get(snapshot).declarations]


def _module(
    snapshot: Snapshot, function: str, position: int, accesses: Sequence[dispatch.Access]
) -> tuple[list[tuple[cparse.Field, str]], list[str]]:
    """The record members the module's other functions took the object as, when every member of the view they declare
    fits the accesses (sits over accessed bytes, nothing sticking out): (member, its type as the header spells it)."""
    layout = world.of(snapshot)
    member = layout.members.get(function)
    group = layout.groups.get(member.group) if member else None
    owner = ledger.owners(index.get(snapshot).identities)
    found: dict[int, tuple[cparse.Field, str]] = {}
    read = []
    for name in group.members if group else ():
        kind = rungs._sibling_type(snapshot, name, position) if name != function else ""
        identity = ledger._owner(owner, kind) if kind else None
        spans = (
            [(f.offset, f.offset + f.size * f.count) for f in (*identity.fields, *identity.records) if f.count]
            if identity
            else []
        )
        if not spans or any(not any(lo <= a.offset and a.offset + a.width <= hi for a in accesses) for lo, hi in spans):
            continue
        read.append(f"{name} ({kind})")
        assert identity is not None
        for one in identity.records:
            body = records.bodies(
                identity.views[0].path, (snapshot.peek(identity.views[0].path) or b"").decode(errors="replace")
            )
            tag = body.get(identity.views[0].name.removeprefix("struct "))
            spelled = re.search(rf"^\s*(.+?)\s*\b{re.escape(one.name)}\s*;", tag.text if tag else "", re.M)
            if spelled:
                found.setdefault(one.offset, (one, spelled[1].strip()))
    return [found[k] for k in sorted(found)], read


def _record(rows: Sequence[dispatch.Access], name: str) -> dispatch.Record | str:
    """The record of the accesses; an integer and a float access of one word read as the float, signs that disagree
    dropped."""
    floats = {(a.offset, a.width) for a in rows if a.kind == "f"}
    signed = {a.offset for a in rows if a.sign == "s"} & {a.offset for a in rows if a.sign == "u"}
    return dispatch.record(
        name,
        [
            dispatch.Access(
                a.offset,
                a.width,
                "f" if (a.offset, a.width) in floats else a.kind,
                "" if a.offset in signed else a.sign,
                a.function,
            )
            for a in rows
        ],
    )


@dataclass(frozen=True)
class _Defined:
    name: str
    body: str

    def text(self) -> str:
        return self.body


def _body(name: str, members: list[tuple[int, int, str, str]]) -> str:
    lines, at = [], 0
    for offset, size, ctype, label in members:
        lines += [f"    char pad{at:X}[0x{offset:X} - 0x{at:X}];"] * (offset > at)
        lines.append(f"    {ctype}{'' if ctype.endswith('*') else ' '}{label};")
        at = offset + size
    return f"typedef struct {name} {{\n" + "\n".join(lines) + f"\n}} {name};\n"


def plan(snapshot: Snapshot, function: str, position: int) -> Made:
    """The change set typing parameter `position` of `function` as one record."""
    made = Made(item_name(function, position), function, position)
    given, declared = _prototype(snapshot, function), index.get(snapshot).declarations.get(function)
    if given is None or declared is None or position >= len(given[1]):
        made.skip = f"{function} has no prototype to take the type"
        return made
    rows, passed = _closure(snapshot, function, position)
    found = sites(snapshot, function, position)
    made.sites = [f"{p}:{n} [{what}] {arg}" for p, n, arg, what in found]
    mates = _mates(snapshot, function, found)
    for name, pos in mates:
        rows += _accesses(snapshot, (name, pos))
    made.shared = [f"{n} (arg {p})" for n, p in mates] + [f"{n} (passed on)" for n in passed]
    if not rows:
        made.skip = f"no access through parameter {position} of {function}"
        return made
    reused, read = _module(snapshot, function, position, rows)
    made.shared += [f"{n} (module mate)" for n in read]
    spans = [(f.offset, f.offset + f.size * f.count) for f, _ in reused]
    numbered = re.search(r"func_([0-9A-F]{8})", function)
    made.name = f"Obj{numbered[1] if numbered else format(index.get(snapshot).addresses.get(function, 0), '08X')}"
    made.name += f"Arg{position}" * bool(position)
    rest = [a for a in rows if not any(lo <= a.offset < hi for lo, hi in spans)]
    got = _record(rest, made.name) if rest else dispatch.Record(made.name, ())
    if isinstance(got, str):
        made.skip = f"the accesses cannot share one record: {got}"
        return made
    members = [(o, w, t, f"unk{o:X}") for o, w, t in got.fields] + [
        (f.offset, f.size * f.count, t, f.name) for f, t in reused
    ]
    members.sort()
    if any(a[0] + a[1] > b[0] for a, b in itertools.pairwise(members)):
        made.skip = "reused members overlap the accessed ones"
        return made
    made.fields = [f"0x{o:X} {w} {t} {n}" for o, w, t, n in members]
    return _edit(snapshot, made, given, declared, _Defined(made.name, _body(made.name, members)))


def _edit(
    snapshot: Snapshot, made: Made, given: tuple[str, list[str]], declared: tuple[str, int, str], defined: _Defined
) -> Made:
    """The texts: the record and the prototype in the family header, the other copies gone, the includes the units
    that lose sight of the name need."""
    function = made.function
    params = [f"{made.name} *" if n == made.position else p for n, p in enumerate(given[1])]
    unit = world.of(snapshot).members.get(function)
    spelled = [declared] + [(p, n, t) for p, n, t in headers.externs(snapshot).get(function, [])]
    row = {
        "name": function,
        "definition": (unit.group if unit else "", 0, f"{given[0]} {function}({', '.join(params)})"),
    }
    changes = declare.plan(snapshot, {**row, "spellings": spelled, "critical": 0})
    if changes.skip or not changes.header:
        made.skip = changes.skip or "no family header"
        return made
    made.header, made.risky = changes.header, changes.risky
    texts = {p: t.decode() for p, t in changes.edits.items()}
    read = declplan.overlay(texts, snapshot.peek)
    place = dispatch.define(read(made.header) or "", defined, [function])
    texts[made.header] = declare._typed(snapshot, made.header, defined.body, place, read)
    for path in _users(snapshot, function):
        text = _after_includes(read(path) or "", declplan.include_line(made.header), function)
        texts[path] = _declared_callees(snapshot, texts, path, text, function)
    for path, line, argument, what in sites(snapshot, function, made.position):
        if what != f"record {made.name}":
            made.conflicts.append(f"{path}:{line} passes {'a ' + what if what == 'bytes' else what}: {argument}")
    made.edits = {p: t.encode() for p, t in texts.items() if t.encode() != snapshot.peek(p)}
    return made


def _declared_callees(snapshot: Snapshot, texts: dict[str, str], path: str, text: str, function: str) -> str:
    """`text` with the include of every header declaring a function that the call's own function calls and the unit
    does not see: a screen compiles that function, and a call to a name nothing declares proves nothing."""
    use = re.search(rf"\b{re.escape(function)}\s*\(", text)
    heads = list(re.finditer(_HEAD, text[: use.start()], re.M)) if use else []
    if not heads:
        return text
    body = text[heads[-1].start() : cparse.close(text, text.index("{", heads[-1].end() - 2)) + 1]
    read = declplan.overlay(texts, snapshot.peek)
    for call in dict.fromkeys(m[1] for m in _CALL.finditer(body)):
        row = index.get(snapshot).declarations.get(call)
        if (
            row
            and headers.is_header(row[0])
            and not declplan.copies(text, call)
            and not declplan.reaches(path, row[0], read)
        ) and not declare._clashes(snapshot, row[0], [path], read):
            text = _after_includes(declplan.add_include(text, row[0]), declplan.include_line(row[0]), call)
            texts[path] = text
    return text


def _after_includes(text: str, line: str, function: str) -> str:
    """`text` with the include `line` after the last include above the first use of `function` that the use can see
    (not inside a conditional branch that closed before it): the header leans on the types the includes before the use
    bring."""
    if text.count(line) != 1 or not (use := re.search(rf"\b{re.escape(function)}\b", text.replace(line, "", 1))):
        return text
    plain = text.replace(line + "\n", "", 1)
    stack: list[int] = []
    marks: dict[int, tuple[int, ...]] = {}
    seen = 0
    for match in re.finditer(r"^[ \t]*#[ \t]*(\w+)[^\n]*\n", plain, re.M):
        if match.start() >= use.start():
            break
        if match[1] in ("if", "ifdef", "ifndef"):
            seen += 1
            stack.append(seen)
        elif match[1] in ("else", "elif") and stack:
            seen += 1
            stack[-1] = seen
        elif match[1] == "endif" and stack:
            stack.pop()
        elif match[1] == "include":
            marks[match.end()] = tuple(stack)
    ok = [at for at, chain in marks.items() if tuple(stack)[: len(chain)] == chain]
    return plain[: max(ok)] + line + "\n" + plain[max(ok) :] if ok else text


# ---------------------------------------------------------------- leases and the skip route


def message(made: Made) -> str:
    return (
        f"Type parameter {made.position} of {made.function} as {made.name}, a record of the offsets the code reads\n\n"
        f"{len(made.fields)} members from the accesses of {made.function} and {len(made.shared)} functions sharing "
        f"the object; the prototype is declared once in {made.header}.\nEvery version's ROM builds byte-identical."
    )


def take(config: Config, snapshot: Snapshot, function: str, position: int, now: float) -> Json:
    """Plan the type item, write its work folder (the change set) and lease it."""
    made = plan(snapshot, function, position)
    where = declare.folder(config, made.item)
    host.clear(where)
    for path, data in made.edits.items():
        host.write(where / path, data)
    brief = [
        f"# {made.item}: type parameter {made.position} of {made.function}",
        "",
        f"record {made.name} in {made.header}:",
    ]
    brief += [f"  {x}" for x in made.fields] + ["", "evidence from:"] + [f"  {x}" for x in made.shared or ["none"]]
    brief += [
        "",
        "call sites:",
        *[f"  {x}" for x in made.sites],
        "",
        "sites passing another type (the compiler only warns):",
    ]
    brief += [f"  {x}" for x in made.conflicts or ["none"]] + ["", f"files: {', '.join(sorted(made.edits))}"]
    host.write(where / "brief.md", ("\n".join(brief) + f"\n\nprove it: recipe try {made.item} [--dry]\n").encode())
    side = {
        "batch": [made.item],
        "risky": list(made.risky),
        "function": function,
        "record": made.name,
        "message": message(made),
    }
    host.write(where / "plan.json", json.dumps(side).encode())
    lease, rules = state.take(config, made.item, now), state.settings()
    return {
        "item": made.item,
        "kind": "type",
        "why": made.skip or f"{made.name}: {len(made.fields)} members, {len(made.sites)} call sites",
        "record": made.name,
        "fields": made.fields,
        "sites": made.sites,
        "conflicts": made.conflicts,
        "evidence": "; ".join(made.shared),
        "brief": str(where / "brief.md"),
        "lease_until": state.stamp(lease.expires),
        "idle_minutes": rules.idle_minutes,
        "stall_tries": rules.stall_tries,
        "next": f"recipe try {made.item}" if made.edits else f"recipe skip {function} --reason other",
    }


def needs(config: Config) -> dict[str, Json]:
    """Per blocked function the type item that unblocks it: {function: {item, next}}, in the project's recipe state."""
    try:
        return dict(json.loads((config.project.root / ".unbake/recipe/needs.json").read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError):
        return {}


def _save(config: Config, held: dict[str, Json]) -> None:
    host.write(config.project.root / ".unbake/recipe/needs.json", json.dumps(held, sort_keys=True).encode())


def route(config: Config, snapshot: Snapshot, function: str) -> Json | None:
    """Record the type item a function skipped for a type waits on (its first `void *` parameter its code reaches by
    offsets), so the ranked work lists name it in the function's place; None when no parameter needs one."""
    found = targets(snapshot, function) if function in world.of(snapshot).members else []
    if not found:
        return None
    row = {"item": item_name(function, found[0]), "next": f"recipe work {item_name(function, found[0])} --kind type"}
    _save(config, {**needs(config), function: row})
    return row


def done(config: Config, function: str) -> None:
    """The type item landed: the function ranks again by itself."""
    held = needs(config)
    if held.pop(function, None) is not None:
        _save(config, held)


def next_item(config: Config, snapshot: Snapshot, now: float) -> Json:
    """Lease the first type item a skipped function waits on."""
    for row in sorted(needs(config).values(), key=lambda r: r["item"]):
        parsed = parse_item(row["item"])
        if parsed and not state.lease(config, row["item"]):
            return take(config, snapshot, parsed[0], parsed[1], now)
    return {"item": "", "why": "no function waits for a type", "next": "recipe status"}
