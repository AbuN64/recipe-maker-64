"""The rung contract: a rung proposes candidates, the ladder measures them all in one pool batch and keeps the best."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from unbake import config as configuration
from unbake import effort, layout
from unbake.contracts import Snapshot
from unbake.families.typedebt import fake_name

from recipemaker import cparse, dispatch, draft, index, ledger, records, twin


@dataclass(frozen=True)
class Proposal:
    name: str
    text: str  # the candidate file
    extras: Mapping[str, str] = field(default_factory=dict)  # header or declaration files with their new text
    evidence: tuple[str, ...] = ()  # one line each for the brief's "Fixed for you"


Rung = Callable[[Snapshot, str, str], Sequence[Proposal]]  # (snapshot, member, candidate text) -> proposals


def propose(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    return [p for rung in RUNGS for p in rung(snapshot, member, text)]


def retype(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    """Give an untyped pointer base the smallest declared struct that has a field for every access made through it."""
    known = index.get(snapshot).identities
    owner = ledger.owners(known)
    before = index.resolve(snapshot, text)
    out = []
    defined = re.search(rf"^[^\n(=;#]*\b{re.escape(member)}\(.*\) \{{$", text, re.M)
    start = defined.start() if defined else 0
    for base in dict.fromkeys(row.base for row in before.left if not row.type and row.candidates):
        names = next(row.candidates for row in before.left if row.base == base)
        best = min(names, key=lambda n: ledger.extent(owner[n]))
        untyped = re.compile(rf"\b(?:void|u8|s8|char)\s*\*\s*{re.escape(base)}\b")
        fixed = text[:start] + untyped.sub(f"{best} *{base}", text[start:], count=1)  # never a callee's prototype
        after = index.resolve(snapshot, fixed)
        named = after.resolved - before.resolved
        header = next((v.path for v in owner[best].views if v.path.endswith(".h")), "")
        if fixed != text and named > 0 and header:
            line = f"retyped {base} as {best} ({header}): {named} of {len(before.left)} placeholders named"
            out.append(Proposal(f"retype {base}", ledger.including(after.text, header), {}, (line,)))
    return out


_UNTYPED = frozenset({"void", "char", "s8", "u8", "s16", "u16", "s32", "u32", "int", "short", "long", "unsigned int"})
_PADDED = ("char", "s8", "u8", "signed char", "unsigned char", "s16", "u16", "s32", "u32", "int")


def _view(known: Sequence[ledger.Identity], kind: str) -> tuple[ledger.Identity, cparse.Layout] | None:
    """The identity of the declared type `kind` and the view of it the project defines in a header."""
    for identity in known:
        for view in identity.views:
            if (view.name in (kind, f"struct {kind}") or kind in view.aliases) and view.path.endswith(".h"):
                return identity, view
    return None


def _pieces(pad: cparse.Field, kind: str, wanted: Sequence[ledger.Use], taken: set[str]) -> list[str] | None:
    """The member lines that replace the padding `pad` (elements of C type `kind`) so each wanted access is a field of
    its own type, the rest staying padding; None when an access cannot be cut out of it without moving a byte."""
    end, at, lines = pad.offset + pad.size * pad.count, pad.offset, []
    by_offset: dict[int, ledger.Use] = {}
    for use in sorted(wanted, key=lambda u: u.offset):
        have = by_offset.setdefault(use.offset, use)
        if (have.size, have.type) != (use.size, use.type):
            return None
    for offset, use in sorted(by_offset.items()):
        size = use.size or 0
        if not size or offset < at or offset + size > end or offset % size or (offset - pad.offset) % pad.size:
            return None
        if size % pad.size or not use.type or fake_name(use.type) or "M2C" in use.type:
            return None
        name = f"unk{offset:X}"
        if name in taken:
            return None
        if offset > at:
            lines.append(f"{kind} {pad.name if at == pad.offset else f'pad{at:X}'}[0x{(offset - at) // pad.size:X}];")
        lines.append(f"{use.type}{'' if use.type.endswith('*') else ' '}{name};")
        at = offset + size
    if at < end:
        lines.append(f"{kind} pad{at:X}[0x{(end - at) // pad.size:X}];")
    names = [re.search(r"(\w+)(?:\[[^\]]*\])?;$", x)[1] for x in lines]  # type: ignore[index]
    if any(n in taken and n != pad.name for n in names):
        return None
    return lines


def split(
    text: str, known: Sequence[ledger.Identity], headers: Mapping[str, str]
) -> tuple[str, dict[str, str], list[str]]:
    """Each access through a typed base that lands inside a padding array of the type's header definition, as a field
    cut out of that padding: the header gets the field (a split of the pad, every other byte where it was) and the
    draft names it. Returns the draft, the edited header texts and one line per split."""
    types = ledger.declared_types(text)
    edits: dict[str, str] = {}
    notes: list[str] = []
    found = ledger.uses(text)
    named: list[tuple[int, int, str]] = []
    for base in dict.fromkeys(u.base for u in found if u.base in types):
        got = _view(known, types[base])
        if got is None:
            continue
        identity, view = got
        mine = [u for u in found if u.base == base and not any(f.offset == u.offset for f in identity.fields)]
        tag = view.name.removeprefix("struct ")
        for pad in view.pads:
            inside = [u for u in mine if pad.offset <= u.offset < pad.offset + pad.size * pad.count]
            current = edits.get(view.path, headers.get(view.path, ""))
            body = records.bodies(view.path, current).get(tag)
            if not inside or body is None:
                continue
            line = re.search(rf"^([ \t]*)((?:\w+ )*\w+)\s+{pad.name}\s*\[[^\]]*\]\s*;[^\n]*\n", body.text, re.M)
            if line is None or line[2] not in _PADDED:
                continue
            taken = set(re.findall(r"(\w+)\s*(?:\[[^\]]*\])*\s*;", body.text))
            pieces = _pieces(pad, line[2], inside, taken)
            if pieces is None:
                continue
            spelled = body.text[: line.start()] + "".join(f"{line[1]}{x}\n" for x in pieces) + body.text[line.end() :]
            edits[view.path] = current[: body.start] + spelled + current[body.end :]
            named += [(u.start, u.end, f"{base}->unk{u.offset:X}") for u in inside]
            notes.append(f"split {pad.name} of {view.name} ({view.path}) for {len(inside)} accesses through {base}")
    for start, end, new in sorted(named, reverse=True):
        text = text[:start] + new + text[end:]
    return text, edits, notes


def split_pads(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    """A typed base read inside its type's padding: the pad split in the header, the draft naming the new field."""
    texts = draft.header_texts(snapshot)
    fixed, edits, notes = split(text, index.get(snapshot).identities, texts)
    return [Proposal("split pad", fixed, edits, tuple(notes))] if edits else []


def parameters(text: str, member: str) -> list[str]:
    """The parameter declarations of the definition of `member` in `text`."""
    found = re.search(rf"^[^\n;{{}}#]*\b{re.escape(member)}\s*\(([^;{{]*)\)\s*\{{$", text, re.M)
    if found is None or found[1].strip() in ("", "void"):
        return []
    return ledger._split(found[1])


def _type_of(expression: str, local: Mapping[str, str], declared: Mapping[str, tuple[str, int, str]]) -> str | None:
    """The record type an argument expression points at: a cast to `T *`, a pointer variable of the caller or a
    global, or `&` of a record variable; None when the caller shows none."""
    expression = expression.strip()
    if cast := re.match(r"\(\s*(?:struct\s+)?(\w+)\s*\*\s*\)", expression):
        return cast[1]
    if re.fullmatch(r"\w+", expression):
        if expression in local:
            return local[expression]
        row = declared.get(expression)
        found = re.search(rf"(?:struct\s+)?(\w+)\s*\*\s*{expression}\s*;", row[2]) if row else None
        return found[1] if found else None
    if address := re.fullmatch(r"&\s*(\w+)", expression):
        row = declared.get(address[1])
        found = re.search(rf"(?:struct\s+)?(\w+)\s+{address[1]}\s*;", row[2]) if row else None
        return found[1] if found else None
    return None


_MOVES = ("addu", "or", "move", "daddu")
Term = tuple[str, ...]  # ("address", sym) | ("value", sym) | ("member", off, *term) | ("return", callee) | (why,)


def _value(code: Sequence[dispatch.Row], at: int, register: str, depth: int = 0) -> Term:
    """What `register` holds just before instruction `at`, traced back through one straight run of code: a global's
    address or value, a word loaded from such a value, a callee's return; else why it is not known."""
    saved = re.fullmatch(r"\$(?:s[0-7]|fp)", register)
    for i in range(at - 1, -1, -1):
        row = code[i]
        if row is None:
            return ("parameter", register) if register in ("$a0", "$a1", "$a2", "$a3") else ("merged paths",)
        name, ops = row
        if name == "jal" and i == at - 1:
            continue  # traced from its delay slot, which runs before the call
        call = name == "jal" or (i and (before := code[i - 1]) is not None and before[0] == "jal")
        if call and not saved:  # a call (or its delay slot, which runs before it) leaves only saved registers
            callee = ops[0] if name == "jal" else before[1][0]  # type: ignore[index]
            return ("return", callee) if register == "$v0" else ("clobbered by a call",)
        if not ops or ops[0] != register or re.match(r"b|j|s[bhwd](?:c1|l|r)?$", name):  # writes no register
            continue
        if depth > 6:
            return ("not traced",)
        if name in _MOVES and len(ops) == 3 and "$zero" in ops[1:]:
            return _value(code, i, ops[1] if ops[2] == "$zero" else ops[2], depth + 1)
        if name == "move" and len(ops) == 2:
            return _value(code, i, ops[1], depth + 1)
        if name == "addiu" and (low := re.fullmatch(r"%lo\((\w+)\)", ops[2])):
            return ("address", low[1])
        if name == "lw" and (low := re.fullmatch(r"%lo\((\w+)\)\((\$\w+)\)", ops[1])):
            return ("value", low[1])
        if name == "lw" and (word := re.fullmatch(r"(-?0x[0-9A-Fa-f]+|-?\d+)\((\$\w+)\)", ops[1])):
            return ("member", word[1], *_value(code, i, word[2], depth + 1))
        return ("computed",)
    return ("parameter", register) if register in ("$a0", "$a1", "$a2", "$a3") else ("merged paths",)


def _pointee(
    term: Term, scope: ledger.Scope, owner: Mapping[str, ledger.Identity], returns: Mapping[str, str]
) -> tuple[str, str]:
    """(record type, "") the traced value points at, or ("", why not): a global's declaration, the member type of the
    record a loaded pointer points at, a callee's declared return type."""
    if term[0] in ("address", "value"):
        seen = scope.globals.get(term[1])
        if seen is None:
            return "", "undeclared global"
        stars = seen.stars + (term[0] == "address" or seen.array)
        return (seen.type, "") if stars == 1 and ledger._owner(owner, seen.type) else ("", "global of no record type")
    if term[0] == "member":
        kind, why = _pointee(term[2:], scope, owner, returns)
        identity = ledger._owner(owner, kind) if kind else None
        found = next((f for f in identity.fields if f.offset == int(term[1], 0)), None) if identity else None
        if found is None:
            return "", why or "member not in the record"
        inner = found.type.replace("*", "").strip()
        good = found.type.count("*") == 1 and ledger._owner(owner, inner)
        return (inner, "") if good else ("", "member of no record type")
    if term[0] == "return":
        spelled = re.match(r"\s*(?:extern\s+)?(?:struct\s+)?(\w+)\s*\*\s*\w+\s*\(", returns.get(term[1], ""))
        if spelled and ledger._owner(owner, spelled[1]):
            return spelled[1], ""
        return "", "callee returns no record pointer"
    return "", term[0]


def _program(snapshot: Snapshot) -> tuple[ledger.Scope, dict[str, ledger.Identity], dict[str, str]]:
    """The globals every header declaring them agrees on, the type owners and each function's prototype."""
    found = index.get(snapshot)
    rows: dict[str, list[ledger.Global]] = {}
    for declared, _ in found.globals.values():
        for name, one in declared.items():
            rows.setdefault(name, []).append(one)
    agreed = {n: r[0] for n, r in rows.items() if len({(x.type, x.stars, x.array) for x in r}) == 1}
    returns = {name: line[2] for name, line in found.declarations.items()}
    return ledger.Scope(agreed), ledger.owners(found.identities), returns


def _readers(snapshot: Snapshot, table: str, holders: Mapping[str, list[str]], depth: int = 0) -> list[str]:
    """The functions that read `table`, else those reading a table that points at it (two levels at most)."""
    found = list(dict.fromkeys(f for f, _, _ in index.get(snapshot).uses.get(table, ())))
    if found or depth >= 2:
        return found
    return list(
        dict.fromkeys(r for parent in holders.get(table, ()) for r in _readers(snapshot, parent, holders, depth + 1))
    )


def _sibling_type(snapshot: Snapshot, name: str, position: int) -> str:
    """The record type the function `name` declares for parameter `position`: its landed C, else its prototype."""
    unit = layout.unit_of(snapshot, name)
    body = (twin.function(index.source(snapshot, unit.path), name) or "") if unit is not None else ""
    params = parameters(body, name) if body else []
    if position < len(params):
        kind = re.match(r"(?:const\s+)?(?:struct\s+)?(\w+)\s*\*(?!\s*\*)", params[position].strip())
        owner = ledger.owners(index.get(snapshot).identities)
        return kind[1] if kind and ledger._owner(owner, kind[1]) else ""
    found = passed(snapshot, f"{name}({', '.join(['_'] * position + ['x'])});", "x")
    return found[0] if found else ""


def _dispatched(
    snapshot: Snapshot,
    member: str,
    position: int,
    program: tuple[ledger.Scope, dict[str, ledger.Identity], dict[str, str]],
    seen: frozenset[str],
) -> list[tuple[str, str]]:
    """What the tables holding `member` show of parameter `position`: a type a sibling entry declares, and what
    reaches the argument register at each indirect call of the functions that read the table."""
    out: list[tuple[str, str]] = []
    tables, holders = effort.memo(("recipe.tables", snapshot.digest), lambda: dispatch.tables(snapshot))
    for table, column, _ in tables.get(member, []):
        out += [(kind, "") for kind in (_sibling_type(snapshot, s, position) for s in column) if kind]
        readers = _readers(snapshot, table, holders)
        for reader in readers:
            try:
                code = dispatch.instructions(index.asm(snapshot, reader))
            except (KeyError, OSError):
                continue
            for at, row in enumerate(code):
                if row and row[0] == "jalr":
                    run = [*code[:at], *code[at + 1 : at + 2]]
                    term = _value(run, len(run), f"$a{position}")
                    if term[0] != "parameter":
                        out.append(_pointee(term, *program))
                        continue
                    inner = asm_evidence(snapshot, reader, int(term[1][-1]), seen | {member})
                    kinds = {k for k, _ in inner if k}
                    why = "/".join(sorted({w for _, w in inner if w})) or "no caller"
                    out.append((kinds.pop(), "") if len(kinds) == 1 else ("", f"dispatcher parameter ({why})"))
        if not readers:
            out.append(("", "table read by no function"))
    return out


def asm_evidence(
    snapshot: Snapshot, member: str, position: int, seen: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """Per call of `member` in a caller that is still assembly, (record type, "") its argument `position` points at,
    or ("", why not): what reaches the argument register at the call, from the declarations of the whole program. A
    caller passing its own parameter on gives what its prototype or, in turn, its own callers show."""
    if position > 3 or len(seen) > 4:
        return []
    found = index.get(snapshot)
    scope, owner, returns = effort.memo(("recipe.program", snapshot.digest), lambda: _program(snapshot))
    out = []
    for caller in dict.fromkeys(found.callers.get(member, ())):
        if layout.unit_of(snapshot, caller) is not None or caller in seen:
            continue  # a landed caller shows its types in C
        try:
            code = dispatch.instructions(index.asm(snapshot, caller))
        except (KeyError, OSError):
            continue
        for at, row in enumerate(code):
            if not (row and row[0] == "jal" and row[1] == [member]):
                continue
            run = [*code[:at], *code[at + 1 : at + 2]]  # the delay slot runs before the call
            term = _value(run, len(run), f"$a{position}")
            if term[0] != "parameter":
                out.append(_pointee(term, scope, owner, returns))
                continue
            index_ = int(term[1][-1])
            proto = passed(snapshot, f"{caller}({', '.join(['_'] * index_ + ['x'])});", "x")
            inner = [(k, "") for k in proto] or asm_evidence(snapshot, caller, index_, seen | {member})
            kinds = {k for k, _ in inner if k}
            why = "/".join(sorted({w for _, w in inner if w})) or "no caller"
            out.append((kinds.pop(), "") if len(kinds) == 1 else ("", f"caller parameter ({why})"))
    return out + _dispatched(snapshot, member, position, (scope, owner, returns), seen)


def evidence(snapshot: Snapshot, member: str, position: int) -> list[str | None]:
    """Per call of `member`, the record type its argument `position` points at (None: unknown): in a landed caller
    from its C, in a caller still in assembly from what reaches the argument register."""
    kinds = configuration.load_resource("units.toml")["kind"]
    declared = index.get(snapshot).declarations
    out: list[str | None] = [kind or None for kind, _ in asm_evidence(snapshot, member, position)]
    for caller in dict.fromkeys(index.get(snapshot).callers.get(member, ())):
        unit = layout.unit_of(snapshot, caller)
        if unit is None or not kinds[unit.kind]["decompiled"] or unit.path.startswith("src/fuzzy/"):
            continue
        body = twin.function(index.source(snapshot, unit.path), caller) or ""
        local = ledger.declared_types(body)
        for call in re.finditer(rf"\b{re.escape(member)}\s*\(", body):
            depth, at = 1, call.end()
            while at < len(body) and depth:
                depth += (body[at] == "(") - (body[at] == ")")
                at += 1
            args = ledger._split(body[call.end() : at - 1])
            out.append(_type_of(args[position], local, declared) if position < len(args) else None)
    return out


def passed(snapshot: Snapshot, text: str, name: str) -> list[str]:
    """The record type of each callee parameter the draft passes `name` to as it is, from the callee's prototype."""
    owner, declared, out = ledger.owners(index.get(snapshot).identities), index.get(snapshot).declarations, []
    for call in re.finditer(r"\b(\w+)\s*\(", text):
        depth, at = 1, call.end()
        while at < len(text) and depth:
            depth += (text[at] == "(") - (text[at] == ")")
            at += 1
        args = [a.strip() for a in ledger._split(text[call.end() : at - 1])]
        proto = re.search(rf"\b{call[1]}\s*\(([^;{{]*)\)", declared.get(call[1], ("", 0, ""))[2])
        given = ledger._split(proto[1]) if proto else []
        for arg, param in zip(args, given, strict=False):
            kind = re.match(r"(?:const\s+)?(?:struct\s+)?(\w+)\s*\*(?!\s*\*)", param)
            if arg == name and kind and ledger._owner(owner, kind[1]):
                out.append(kind[1])
    return out


def _retyped(params: Sequence[str], position: int, kind: str) -> str:
    name = re.search(r"(\w+)\s*$", params[position])
    spelled = f"{kind} *{name[1] if name and len(params[position].split()) > 1 else ''}".rstrip()
    return ", ".join(spelled if i == position else p for i, p in enumerate(params))


def _untyped_params(text: str, member: str, left: Sequence[ledger.Left]) -> list[tuple[str, int]]:
    """(name, position) of each parameter base the resolver left untyped, with no pointer arithmetic on it (a new type
    would scale it)."""
    names = [re.search(r"(\w+)\s*$", p) for p in parameters(text, member)]
    out = []
    for base in dict.fromkeys(row.base for row in left if not row.type):
        position = next((i for i, n in enumerate(names) if n and n[1] == base), None)
        if position is not None and not re.search(rf"\b{base}\s*[-+\[]|[-+]\s*{base}\b", text):
            out.append((base, position))
    return out


def _param_typed(
    snapshot: Snapshot,
    member: str,
    text: str,
    typing: tuple[str, int, str],
    known: Sequence[ledger.Identity],
    extras: Mapping[str, str],
) -> tuple[str, dict[str, str], list[str]] | None:
    """The candidate with parameter `position` (named `base`) declared a pointer to the record `spelled`, the project's
    prototype retyped in the same change, accesses the record lacks split out of its padding; with the header texts
    it changes (`extras` first) and one line per split. None when some access through the base stays unnamed."""
    base, position, spelled = typing
    params = parameters(text, member)
    head = re.search(rf"^([^\n;{{}}#]*\b{re.escape(member)}\s*\()([^;{{]*)(\)\s*\{{)$", text, re.M)
    if head is None:
        return None
    fixed = text[: head.start(2)] + _retyped(params, position, spelled) + text[head.end(2) :]
    texts = {**draft.header_texts(snapshot), **extras}
    fixed, edits, notes = split(fixed, known, texts)
    edits = {**extras, **edits}
    found = index.get(snapshot)
    after = ledger.resolve(fixed, known, found.extents, index.scope(snapshot, fixed))
    if any(row.base == base for row in after.left):
        return None  # the type has no field for some access, even with its padding split
    declared = found.declarations.get(member)
    if declared and declared[0].endswith(".h"):
        header = edits.get(declared[0], texts.get(declared[0], ""))
        proto = re.search(rf"^([^\n;{{}}#]*\b{re.escape(member)}\s*\()([^;{{]*)(\)\s*;)", header, re.M)
        if proto is None:
            return None
        given = ledger._split(proto[2]) if proto[2].strip() not in ("", "void") else []
        if len(given) != len(params):
            return None
        edits[declared[0]] = header[: proto.start(2)] + _retyped(given, position, spelled) + header[proto.end(2) :]
    return after.text, edits, notes


def retype_from_callers(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    """An untyped parameter base given the one record type every landed caller that shows a type passes it, with the
    project's prototype retyped in the same change; the accesses then name that type's fields, its padding split for
    the ones it lacks. Nothing is proposed without such evidence or when the type misses an access."""
    known = index.get(snapshot).identities
    out = []
    for base, position in _untyped_params(text, member, index.resolve(snapshot, text).left):
        calls = [t for t in evidence(snapshot, member, position) if t] + passed(snapshot, text, base)
        seen = set(calls)
        kind = seen.pop() if len(seen) == 1 else ""
        got = _view(known, kind) if kind and kind not in _UNTYPED and not fake_name(kind) else None
        if got is None:
            continue
        view = got[1]
        spelled = view.name if view.name.startswith("struct ") else kind
        made = _param_typed(snapshot, member, text, (base, position, spelled), known, {})
        if made is None:
            continue
        line = f"retyped {base} as {spelled} from {len(calls)} landed calls ({view.path})"
        out.append(Proposal(f"caller type {base}", ledger.including(made[0], view.path), made[1], (line, *made[2])))
    return out


def _group_type(snapshot: Snapshot, nodes: Sequence[dispatch.Node]) -> set[str]:
    """The record types a project header defines that the group's functions already declare for the object (landed C
    or a prototype); a view one source defines for itself is no type the others can name."""
    known = index.get(snapshot).identities
    kinds = {kind for name, position in nodes if (kind := _sibling_type(snapshot, name, position))}
    return {kind for kind in kinds if _view(known, kind)}


def _spelled(text: str, base: str) -> dict[int, str]:
    """Per offset the scalar or `void *` type the draft spells for an access through `base`."""
    out: dict[int, str] = {}
    for use in ledger.uses(text):
        if use.base.replace(" ", "") == base and use.size:
            out.setdefault(use.offset, "void *" if use.kind == "p" else use.type)
    return out


def _draft_accesses(text: str, base: str, member: str) -> list[dispatch.Access]:
    """The accesses the draft makes through `base`, as the group's evidence of its own function."""
    return [
        dispatch.Access(u.offset, u.size or 0, u.kind, "", member)
        for u in ledger.uses(text)
        if u.base.replace(" ", "") == base and u.size
    ]


def group_record(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    """An untyped parameter whose object one dispatch site (a data table column) passes to a group of functions: the
    group's record type, when a group function already declares one, else one record built from the accesses every
    group function makes through the object, defined in the header every group function's unit reaches. A group
    whose functions disagree proposes nothing (the census names both)."""
    known = index.get(snapshot).identities
    out = []
    for base, position in _untyped_params(text, member, index.resolve(snapshot, text).left):
        if any(evidence(snapshot, member, position)) or passed(snapshot, text, base):
            continue  # a caller or callee shows a type: retype_from_callers owns it
        found = dispatch.group(snapshot, member, position)
        if not isinstance(found, dispatch.Group):
            continue
        kinds = _group_type(snapshot, found.nodes)
        if len(kinds) > 1:
            continue
        if kinds:
            kind = kinds.pop()
            got = _view(known, kind) if kind not in _UNTYPED and not fake_name(kind) else None
            if got is None:
                continue
            spelled = got[1].name if got[1].name.startswith("struct ") else kind
            made = _param_typed(snapshot, member, text, (base, position, spelled), known, {})
            if made is None:
                continue
            line = f"retyped {base} as {spelled}, the record of dispatch group {found.name} ({got[1].path})"
            out.append(
                Proposal(f"group type {base}", ledger.including(made[0], got[1].path), made[1], (line, *made[2]))
            )
            continue
        made_record = group_evidence(snapshot, member, text, base, found)
        path = dispatch.header(snapshot, found.nodes)
        if isinstance(made_record, str) or not path:
            continue
        made_record, hops = _hopped(snapshot, found, made_record)
        current = draft.header_texts(snapshot).get(path, "")
        for one in (*hops, made_record):  # each pointee before the record that points at it
            current = dispatch.define(current, one, (n for n, _ in found.nodes))
        mine = _identities(path, (*hops, made_record))
        made = _param_typed(
            snapshot, member, text, (base, position, made_record.name), (*known, *mine), {path: current}
        )
        if made is None:
            continue
        line = (
            f"retyped {base} as {made_record.name}, a record of {len(made_record.fields)} members from the accesses"
            f" of the {len(found.nodes)} functions of its dispatch group ({path})"
        )
        hopped = [
            f"typed its member at 0x{h.name.rpartition('Ptr')[2]} as {h.name} *, from {len(h.fields)} members every"
            " group function accesses through it"
            for h in hops
        ]
        out.append(
            Proposal(f"group record {base}", ledger.including(made[0], path), made[1], (line, *hopped, *made[2]))
        )
    return out


def _identities(path: str, made: Sequence[dispatch.Record]) -> tuple[ledger.Identity, ...]:
    """The ledger's view of records this change defines in `path`."""
    views = cparse._layouts({path: cparse.scan("".join(r.text() for r in made))})
    return tuple(ledger.Identity((v,), v.fields) for v in views)


def _hopped(
    snapshot: Snapshot, found: dispatch.Group, made: dispatch.Record
) -> tuple[dispatch.Record, list[dispatch.Record]]:
    """The group record with each `void *` member whose pointee every group function accesses consistently pointing
    at that pointee's own record (one per member, named by it), and those records. A pointee the functions disagree
    about stays `void *`."""
    hops = []
    for offset, _, kind in made.fields:
        if kind != "void *":
            continue
        got = dispatch.record(f"{found.name}Ptr{offset:X}", dispatch.pointee(snapshot, found, offset))
        if isinstance(got, dispatch.Record):
            hops.append(got)
            made = dispatch.retarget(made, offset, got.name)
    return made, hops


_SCALAR_WORD = frozenset({"s32", "u32", "int", "unsigned int", "long"})


def _scalar_uses(snapshot: Snapshot, name: str) -> list[str]:
    """The landed sources naming the global `name` other than by its address: they read it as it is declared."""
    pattern = re.compile(rf"(?<![&\w]){re.escape(name)}\b")
    return [p for p in cparse.landed_sources(snapshot) if pattern.search(index.source(snapshot, p))]


def _scaled_uses(snapshot: Snapshot, name: str) -> list[str]:
    """The landed sources adding to or indexing the pointer global `name`, uncast."""
    n = re.escape(name)
    pattern = re.compile(rf"(?<![\w)])\s*{n}\s*[-+\[]|[-+]\s*{n}\b")
    return [p for p in cparse.landed_sources(snapshot) if pattern.search(index.source(snapshot, p))]


def _declared(snapshot: Snapshot, text: str, name: str) -> ledger.Global | None:
    """The declaration of the global `name`: the one the draft's headers show, else any project header's, else the
    draft's own `extern` line (path "")."""
    seen = index.scope(snapshot, text).globals.get(name)
    if seen is not None and seen.path:
        return seen
    for path, (rows, _) in sorted(index.get(snapshot).globals.items()):
        if name in rows and not path.startswith("include/sdk/"):
            return rows[name]
    return ledger.globals_in(text).get(name)


def owner_header(snapshot: Snapshot, name: str) -> str:
    """The module header of the unit owning the data symbol `name` in the layout: its own member, else the member of
    the names version whose placement holds its address; "" when none does or the project has no such header."""
    members = snapshot.layout.members
    member = members.get(name)
    address = index.get(snapshot).addresses.get(name)
    if member is None and address is not None:
        version = snapshot.config.project.names_from
        member = next(
            (
                m
                for m in members.values()
                for p in m.placements
                if p.version == version and p.vram <= address < p.vram + max(p.size, 1)
            ),
            None,
        )
    group = snapshot.layout.groups.get(member.group) if member else None
    header = group.header if group else ""
    return header if header and header in draft.header_texts(snapshot) else ""


def _plain(seen: ledger.Global | None) -> bool:
    integers = {*cparse.SCALARS, "unsigned int", "long"} - cparse.FLOATS
    return seen is None or (seen.stars == 0 and not seen.array and seen.type in integers)


def global_evidence(
    snapshot: Snapshot, text: str, name: str, address: bool, aliases: Sequence[str] = ()
) -> tuple[dispatch.Record, str, str] | str:
    """The record of the global `name` (`address`: the object itself; else what its pointer value points at) built
    from every function accessing it and the draft (through the base itself and each local in `aliases` it feeds),
    with the header that gets it and how: "retype" a declaration, "declare" it in the owner unit's header when no
    header declares it, "extend" the symbol's own record with the members another user shows; or why none is."""
    found = index.get(snapshot)
    if name not in found.addresses:
        return "no address in the names version"
    seen = _declared(snapshot, text, name)
    plain = _plain(seen)
    own = dispatch.data_name(name, found.addresses[name], not address)
    if fake_name(own):
        return f"its record name {own} would carry a function symbol"
    if seen is not None and seen.type == own and seen.path and seen.stars == (0 if address else 1) and not seen.array:
        mode = "extend"
    elif address and not plain:
        return f"declared {seen.type}{'*' * seen.stars}{'[]' * seen.array}, not a scalar"  # type: ignore[union-attr]
    elif (
        not address
        and seen is not None
        and not (seen.stars == 1 and seen.type == "void")
        and not (plain and seen.type in _SCALAR_WORD)
    ):
        return f"declared {seen.type}{'*' * seen.stars}{'[]' * seen.array}, not an untyped pointer"
    else:
        mode = "retype" if seen is not None and seen.path else "declare"
    if mode != "extend" and (plain or address) and (users := _scalar_uses(snapshot, name)):
        return f"landed code reads it as the {seen.type if seen else 'scalar'} it is declared ({users[0]})"
    if mode != "extend" and not plain and (users := _scaled_uses(snapshot, name)):
        return f"landed code does arithmetic on it, which a record pointer would scale ({users[0]})"
    path = seen.path if mode != "declare" else owner_header(snapshot, name)  # type: ignore[union-attr]
    if not path:
        return "no header declares it and no owner unit header holds it"
    rows = dispatch.global_object(snapshot, name) if address else dispatch.global_pointee(snapshot, name)
    spelled: dict[int, str] = {}
    for base in (f"&{name}" if address else name, *aliases):
        rows += _draft_accesses(text, base, "the draft")
        spelled = {**_spelled(text, base), **spelled}
    made = dispatch.record(own, rows, spelled)
    return made if isinstance(made, str) else (made, path, mode)


_ASSIGNED = re.compile(r"^[ \t]*(\w+) = (&?)([A-Za-z_]\w*);", re.M)


def fed(text: str, member: str, base: str) -> tuple[str, bool] | None:
    """(symbol, address) when the draft's local `base` only ever holds one data symbol's value (`x = D;`) or address
    (`x = &D;`), is declared an untyped pointer or word, and takes no pointer arithmetic; else None."""
    params = {m[1] for p in parameters(text, member) if (m := re.search(r"(\w+)\s*$", p))}
    if base in params or not re.fullmatch(r"\w+", base):
        return None
    if not re.search(rf"^[ \t]*(?:void \*|s32 |u32 |int )\s*{re.escape(base)};", text, re.M):
        return None
    writes = re.findall(rf"(?<![\w.>]){re.escape(base)}\s*(?:[-+*/|&^]?=(?!=)|\+\+|--)", text)
    sources = {(m[3], bool(m[2])) for m in _ASSIGNED.finditer(text) if m[1] == base}
    if len(sources) != 1 or len(writes) != sum(1 for m in _ASSIGNED.finditer(text) if m[1] == base):
        return None
    if re.search(rf"\b{re.escape(base)}\s*[-+\[]|[-+]\s*{re.escape(base)}\b", text):
        return None
    ((symbol, address),) = sources
    return (symbol, address) if re.fullmatch(r"[A-Za-z_]\w*", symbol) else None


def _roots(
    snapshot: Snapshot, member: str, text: str, left: Sequence[ledger.Left]
) -> dict[tuple[str, bool], list[str]]:
    """Per data symbol root (name, address) the locals of the draft it feeds; a base naming the symbol itself adds the
    root with no local."""
    params = {m[1] for p in parameters(text, member) if (m := re.search(r"(\w+)\s*$", p))}
    local = {*ledger.declared_types(text), *params}
    addresses = index.get(snapshot).addresses
    out: dict[tuple[str, bool], list[str]] = {}
    for row in left:
        base = row.base.replace(" ", "")
        got = re.fullmatch(r"(&?)([A-Za-z_]\w*)", base)
        if got is None:
            continue
        if got[2] in addresses and (bool(got[1]) or got[2] not in local):
            out.setdefault((got[2], bool(got[1])), [])
        elif not got[1] and (root := fed(text, member, got[2])) and root[0] in addresses:
            names = out.setdefault(root, [])
            if got[2] not in names:
                names.append(got[2])
    return out


def _define(header: str, one: dispatch.Record, name: str, address: bool, mode: str) -> str | None:
    """The header with the symbol's record defined (or its definition replaced, `extend`) and `name` declared as it
    or as a pointer to it; None when the declaration to retype is not there."""
    spelled = f"extern {one.name} {'' if address else '*'}{name};\n"
    if mode == "extend":
        old = re.search(rf"^typedef struct {one.name} \{{\n(?:[^}}]*\n)*?\}} {one.name};\n", header, re.M)
        return None if old is None else header[: old.start()] + one.text() + header[old.end() :]
    if mode == "declare":
        return _declare(header, one, spelled)
    extern = re.search(rf"^[ \t]*extern\b[^;\n]*\b{re.escape(name)}\b[^;\n]*;[^\n]*\n?", header, re.M)
    if extern is None:
        return None
    return header[: extern.start()] + one.text() + spelled + header[extern.end() :]


def _declare(header: str, one: dispatch.Record, spelled: str) -> str:
    """The owner header with the record and the symbol's declaration before its include guard's end."""
    guard = re.search(r"^[ \t]*#[ \t]*endif\b[^\n]*\n?\s*\Z", header, re.M)
    at = guard.start() if guard else len(header)
    lead = "" if not at or header[at - 1] == "\n" else "\n"
    return header[:at] + lead + one.text() + spelled + header[at:]


def global_record(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    """The last rung, owner-local records: each data symbol the draft reads by offset (itself, its address, or a local
    holding either) gets the one record of that symbol, named from it (never a function), its members the offsets and
    widths every function accessing it shows and placeholder bytes between. The record is defined where the symbol
    is declared (the declaration retyped), else in the header of the unit owning the symbol in the layout (declared
    there); a record the symbol already has is extended with the members another user shows. Two users disagreeing
    at an offset, landed code reading it as its scalar or adding to it, leave it as it is (the census names why)."""
    found = index.get(snapshot)
    texts = draft.header_texts(snapshot)
    edits: dict[str, str] = {}
    made: list[dispatch.Record] = []
    lines: list[str] = []
    retyped: dict[str, ledger.Global] = {}
    locals_: dict[str, str] = {}
    left = index.resolve(snapshot, text).left
    for (name, address), aliases in _roots(snapshot, member, text, left).items():
        if name in retyped:
            continue
        evidence = global_evidence(snapshot, text, name, address, aliases)
        if isinstance(evidence, str):
            continue
        one, path, mode = evidence
        header = _define(edits.get(path, texts.get(path, "")), one, name, address, mode)
        if header is None:
            continue
        edits[path] = header
        made.append(one)
        retyped[name] = ledger.Global(one.name, 0 if address else 1, False, path)
        locals_.update({alias: one.name for alias in aliases})
        what = "it" if address else "what it points at"
        verb = {"retype": "retyped", "declare": "declared", "extend": "extended"}[mode]
        lines.append(
            f"{verb} {name} as {one.name}{'' if address else ' *'}, a record of {len(one.fields)} members"
            f" from every function accessing {what} ({path})"
            + (f"; typed {', '.join(aliases)} as {one.name} *" if aliases else "")
        )
    if not made:
        return []
    for name in retyped:  # the header's declaration is the one the draft sees
        text = re.sub(rf"^[ \t]*extern\b[^;\n]*\b{re.escape(name)}\b[^;\n]*;\n", "", text, flags=re.M)
    for alias, kind in locals_.items():
        text = re.sub(
            rf"^([ \t]*)(?:void \*|s32 |u32 |int )\s*{re.escape(alias)};", rf"\g<1>{kind} *{alias};", text, flags=re.M
        )
    scope = index.scope(snapshot, text)
    scope = ledger.Scope({**scope.globals, **retyped}, scope.addresses, scope.elsewhere)
    mine = tuple(i for path in edits for i in _identities(path, [r for r in made if r.name in edits[path]]))
    for path in edits:
        text = ledger.including(text, path)
    after = ledger.resolve(text, (*found.identities, *mine), found.extents, scope)
    gone = {*retyped, *locals_}
    if any(re.fullmatch(r"&?(\w+)", row.base.replace(" ", "")) and row.base.lstrip("&") in gone for row in after.left):
        return []
    return [Proposal("owner records", after.text, edits, tuple(lines))]


def group_evidence(
    snapshot: Snapshot, member: str, text: str, base: str, found: dispatch.Group
) -> dispatch.Record | str:
    """The group's record from every group function's accesses through the object and the draft's own, or why the
    functions cannot share one."""
    rows = [a for node in found.nodes if node[0] != member for a in dispatch.flows(snapshot, node).accesses]
    mine = [node for node in found.nodes if node[0] == member]
    rows += [a for node in mine for a in dispatch.flows(snapshot, node).accesses]
    rows += _draft_accesses(text, base, member)
    return dispatch.record(found.name, rows, _spelled(text, base))


def _grouped(snapshot: Snapshot, member: str, text: str, base: str, position: int) -> str:
    """What the dispatch group of an untyped parameter with no caller evidence says of it."""
    found = dispatch.group(snapshot, member, position)
    if found is None:
        return "no caller evidence"
    if isinstance(found, str):
        return f"dispatch group refused: {found}"
    made = group_evidence(snapshot, member, text, base, found)
    if isinstance(made, str):
        return f"dispatch group {found.name} refused: {made}"
    if not dispatch.header(snapshot, found.nodes):
        return f"dispatch group {found.name}: no header every group function reaches"
    if any(p.name.endswith(f" {base}") for p in group_record(snapshot, member, text)):
        return f"dispatch group {found.name}: its record was proposed and measured worse than the candidate"
    return f"dispatch group {found.name}: its record misses an access"


def classify(snapshot: Snapshot, member: str, text: str, left: Sequence[ledger.Left]) -> tuple[ledger.Left, ...]:
    """Each placeholder left with the class of what keeps it one: an expression base, a typed base with no field and no
    padding there, an untyped base whose callers show no type or a type that misses an access."""
    params = [m[1] for p in parameters(text, member) if (m := re.search(r"(\w+)\s*$", p))]
    local = ledger.declared_types(text)
    out = []
    for row in left:
        name = row.base.replace(" ", "").removeprefix("&")
        if not re.fullmatch(r"&?\w+", row.base):
            why = "expression base"
        elif name not in params and name not in local and name in index.get(snapshot).addresses:
            got = global_evidence(snapshot, text, name, row.base.startswith("&"))
            why = f"global, {got}" if isinstance(got, str) else f"global, record {got[0].name} measured worse"
        elif (
            row.base not in params
            and (root := fed(text, member, row.base))
            and root[0] in index.get(snapshot).addresses
        ):
            got = global_evidence(snapshot, text, root[0], root[1], [row.base])
            how = f"local fed by {'&' if root[1] else ''}{root[0]}"
            why = f"{how}, {got}" if isinstance(got, str) else f"{how}, record {got[0].name} measured worse"
        elif row.type:
            why = "typed base, field in another view" if row.elsewhere else "typed base, no field or padding there"
        elif row.base in params:
            seen = {t for t in evidence(snapshot, member, params.index(row.base)) if t} | set(
                passed(snapshot, text, row.base)
            )
            why = "untyped parameter, " + (
                "callers disagree or the type misses"
                if seen
                else _grouped(snapshot, member, text, row.base, params.index(row.base))
            )
        elif re.search(rf"^[ \t]+\w[\w ]*[ *]{re.escape(row.base)};", text, re.M) and row.base not in params:
            why = "untyped local, not fed by one data symbol"
        else:
            why = "untyped local or global, no evidence"
        out.append(dataclasses.replace(row, why=why))
    return tuple(out)


def resolve(snapshot: Snapshot, member: str, text: str) -> list[Proposal]:
    """The candidate with each M2C_FIELD the resolver can name now: a file an earlier tool left, or one a rung typed."""
    found = index.resolve(snapshot, text)
    line = f"resolved {found.resolved} M2C_FIELD of the candidate"
    return [Proposal("resolve", found.text, {}, (line,))] if found.resolved else []


# the ordered table: the ladder runs each rung that proposes something
RUNGS: tuple[Rung, ...] = (resolve, retype, split_pads, retype_from_callers, group_record, global_record)
