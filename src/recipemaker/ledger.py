"""The type ledger: views of one type join into identities; M2C_FIELD resolves only through the declared type."""

import dataclasses
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from recipemaker.cparse import FLOATS, SCALARS, Field, Layout

_USE = re.compile(r"M2C_FIELD\(")
_SHARED = 3  # equal fields that make two views one identity


@dataclass(frozen=True)
class Use:
    start: int
    end: int
    base: str
    offset: int
    size: int | None  # of the accessed value, None when its type is not a scalar or pointer
    kind: str
    type: str = ""  # the accessed value's C type as m2c spelled it (`s32`, `void *`)


@dataclass(frozen=True)
class Identity:
    views: tuple[Layout, ...]
    fields: tuple[Field, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(view.name for view in self.views)

    @property
    def records(self) -> tuple[Field, ...]:
        """The record members of the type's views, by offset: the first view to place any at an offset keeps all it
        places there (a union's members share offset 0)."""
        found: dict[int, list[Field]] = {}
        for view in self.views:
            mine: dict[int, list[Field]] = {}
            for member in view.records:
                mine.setdefault(member.offset, []).append(member)
            for offset, members in mine.items():
                found.setdefault(offset, members)
        return tuple(sorted((f for members in found.values() for f in members), key=lambda f: f.offset))

    @property
    def spelled(self) -> bool:
        """Whether C can name the type: one of its views is no record written untagged inside another."""
        return any(not view.inner for view in self.views)

    @property
    def size(self) -> int:
        """sizeof the type as its views declare it, 0 when none says."""
        return max((view.size for view in self.views), default=0)


@dataclass(frozen=True)
class Global:
    """A file-scope variable as one declaration the draft sees spells it."""

    type: str  # the element type without the declarator's stars (`ModelPreviewScreen`, `s32`)
    stars: int
    array: bool
    path: str = ""  # the header declaring it, "" when the draft declares it itself


@dataclass(frozen=True)
class Scope:
    """What a draft sees beyond its function: the globals its headers and own lines declare, the address of each
    symbol in the names version, and record globals of headers it does not include (found by address only)."""

    globals: Mapping[str, Global] = dataclasses.field(default_factory=dict)
    addresses: Mapping[str, int] = dataclasses.field(default_factory=dict)
    elsewhere: Mapping[str, Global] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class Left:
    base: str
    offset: int
    type: str  # the declared type of the base, "" when the candidate does not show one
    identity: tuple[str, ...]  # names of the views of that type's identity
    elsewhere: bool  # another view of the identity has a field there (skip reason needs-header-change)
    candidates: tuple[str, ...] = ()  # untyped base: types with a field fitting every access made through it
    why: str = ""  # the class of what keeps it a placeholder, once the rungs have looked (see rungs.classify)


@dataclass(frozen=True)
class Result:
    text: str
    resolved: int
    left: tuple[Left, ...]


def _split(args: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for at, char in enumerate(args):
        depth += (char in "([") - (char in ")]")
        if char == "," and not depth:
            parts.append(args[start:at].strip())
            start = at + 1
    return [*parts, args[start:].strip()]


def uses(text: str) -> list[Use]:
    out = []
    for found in _USE.finditer(text):
        depth, at = 1, found.end()
        while at < len(text) and depth:
            depth += (text[at] == "(") - (text[at] == ")")
            at += 1
        parts = _split(text[found.end() : at - 1])
        if len(parts) != 3 or not re.fullmatch(r"-?(0[xX][0-9A-Fa-f]+|\d+)", parts[2]):
            continue
        kind = parts[1].rstrip().removesuffix("*").strip()
        size = 4 if "*" in kind else SCALARS.get(kind)
        out.append(
            Use(
                found.start(),
                at,
                parts[0],
                int(parts[2], 0),
                size,
                "p" if "*" in kind else "f" if kind in FLOATS else "i",
                " ".join(parts[1].split()).removesuffix("*").rstrip(),
            )
        )
    return out


def _shape(field: Field) -> tuple[int, int, str]:
    return field.offset, field.size, field.kind


def _merge(fields: Iterable[Field]) -> tuple[tuple[Field, ...], tuple[int, ...]]:
    by_offset: dict[int, Field] = {}
    bad: set[int] = set()
    for field in fields:
        have = by_offset.setdefault(field.offset, field)
        if _shape(have) != _shape(field):
            bad.add(field.offset)
    kept = sorted((f for f in by_offset.values() if f.offset not in bad), key=lambda f: f.offset)
    return tuple(kept), tuple(sorted(bad))


def view_shapes(views: Sequence[Layout]) -> list[set[tuple[int, int, str]]]:
    """The field shapes of each view; a union's members overlap, so it shares no shape with another view."""
    return [set() if view.union else {_shape(f) for f in view.fields} for view in views]


def shared_pairs(shapes: Sequence[set[tuple[int, int, str]]]) -> list[tuple[int, int]]:
    """The pairs of views sharing three or more equal fields, by the bit sets of the shapes each view has."""
    ids: dict[tuple[int, int, str], int] = {}
    masks = {
        at: sum(1 << ids.setdefault(shape, len(ids)) for shape in found)
        for at, found in enumerate(shapes)
        if len(found) >= _SHARED
    }
    ordered = list(masks.items())
    return [
        (a, b)
        for n, (a, mask) in enumerate(ordered)
        for b, other in ordered[n + 1 :]
        if (mask & other).bit_count() >= _SHARED
    ]


def identities(
    views: Sequence[Layout], evidence: Iterable[tuple[str, str]] = (), shared: Iterable[tuple[int, int]] | None = None
) -> list[Identity]:
    """Join views into identities by evidence pairs or three shared fields, unless two shapes meet at an offset."""
    by_name = {view.name: i for i, view in enumerate(views)}
    parent = list(range(len(views)))
    placed: dict[int, dict[int, tuple[int, str]]] = {}  # root -> offset -> (size, kind) of the group's fields
    clash = set[int]()  # roots whose fields disagree at some offset: no join with them is made
    for i, view in enumerate(views):
        placed[i] = {}
        for field in () if view.union else view.fields:  # a union's members overlap by design
            if placed[i].setdefault(field.offset, (field.size, field.kind)) != (field.size, field.kind):
                clash.add(i)

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(a: int, b: int) -> None:
        ra, rb = root(a), root(b)
        if ra == rb or ra in clash or rb in clash:
            return
        if len(placed[ra]) < len(placed[rb]):
            ra, rb = rb, ra
        if any(placed[ra].get(at, shape) != shape for at, shape in placed[rb].items()):
            return
        placed[ra].update(placed.pop(rb))
        parent[rb] = ra

    for left, right in evidence:
        if left in by_name and right in by_name:
            join(by_name[left], by_name[right])
    for a, b in sorted(shared_pairs(view_shapes(views)) if shared is None else shared):
        join(a, b)
    groups: dict[int, list[Layout]] = {}
    for i, view in enumerate(views):
        groups.setdefault(root(i), []).append(view)
    result = []
    for members in groups.values():
        merged, _ = _merge(f for view in members if not view.union for f in view.fields)
        overlapping = tuple(f for view in members if view.union for f in view.fields)
        result.append(Identity(tuple(members), tuple(sorted((*merged, *overlapping), key=lambda f: f.offset))))
    return result


def _fits(field: Field, use: Use) -> bool:
    if use.size != field.size or use.offset != field.offset or field.count != 1:
        return False
    if field.kind == "f" or use.kind == "f":
        return field.kind == use.kind
    if field.size == 4 and "p" in (field.kind, use.kind):
        return True  # a 4-byte integer and a pointer hold each other
    return field.kind == use.kind


def extent(identity: Identity) -> int:
    """The size of a type: the end of its last field."""
    return max((f.offset + f.size * f.count for f in identity.fields), default=0)


def rank(known: Iterable[Identity], offsets: Iterable[int], limit: int | None) -> list[tuple[str, int, int]]:
    """(name, offsets covered, size) of the types with a field at some of `offsets`, best first; a type larger than
    the object (`limit` bytes, None when unknown) is never offered."""
    wanted = sorted(set(offsets))
    rows = [
        (
            i.names[0],
            sum(any(f.offset <= o < f.offset + f.size * f.count for f in i.fields) for o in wanted),
            extent(i),
        )
        for i in known
        if i.spelled and (limit is None or extent(i) <= limit)
    ]
    return sorted((r for r in rows if r[1]), key=lambda r: (-r[1], r[2], r[0]))


def declared_types(text: str) -> dict[str, str]:
    """The declared type of each pointer name in the function: its parameters and locals, never a prototype above."""
    found: dict[str, str] = {}
    opened = re.search(r"^[^\n(=;#]*\w\(.*\) \{$", text, re.M)
    for match in re.finditer(
        r"\b(?:struct\s+)?([A-Za-z_]\w*)\s*\*+\s*(\w+)\s*[,)=;\[]", text[opened.start() if opened else 0 :]
    ):
        if match[1] not in ("return", "else", "sizeof", "void", "char", "const", "unsigned", "signed"):
            found.setdefault(match[2], match[1])
    return found


_EXTERN = re.compile(
    r"^[ \t]*extern\s+(?:(?:const|volatile)\s+)*((?:(?:struct|union|unsigned|signed)\s+)?\w+)\s*((?:\*\s*)*)"
    r"(\w+)\s*((?:\[[^\]]*\]\s*)*);",
    re.M,
)
_EXPRESSION = re.compile(r"([A-Za-z_]\w*)((?:->\w+|\.\w+|\[(?:0[xX][0-9A-Fa-f]+|\d+)\])*)")
_STEP = re.compile(r"->(\w+)|\.(\w+)|\[(0[xX][0-9A-Fa-f]+|\d+)\]")
_DEPTH = 8  # records nested deeper than this are never searched
_PASSES = 4  # a field named in one pass may type the base of another in the next (nested M2C_FIELD)


def globals_in(text: str, path: str = "") -> dict[str, Global]:
    """The variables `extern` declarations of `text` declare, each with its spelled type; a function prototype is
    none. In a draft (no `path`) only the lines above its function count."""
    opened = None if path else re.search(r"^[^\n(=;#]*\w\(.*\) \{$", text, re.M)
    head = text[: opened.start()] if opened else text
    found: dict[str, Global] = {}
    for match in _EXTERN.finditer(head):
        kind = " ".join(match[1].split())
        found.setdefault(match[3], Global(kind, match[2].count("*"), bool(match[4].strip()), path))
    return found


def including(text: str, path: str) -> str:
    """`text` with an include of the project header `path` after its last include."""
    line = f'#include "{path.removeprefix("include/")}"\n'
    if line in text:
        return text
    last = max((m.end() for m in re.finditer(r'^#include "[^"]*"\n', text, re.M)), default=0)
    return text[:last] + line + text[last:]


def owners(known: Iterable[Identity]) -> dict[str, Identity]:
    """Each name a type is spelled by (its views, their typedef names, `struct tag`) to its identity."""
    out: dict[str, Identity] = {}
    for identity in known:
        for view in identity.views:
            for name in (view.name, *view.aliases):
                out.setdefault(name, identity)
    return out


def _owner(owner: Mapping[str, Identity], kind: str) -> Identity | None:
    kind = " ".join(kind.split())
    bare = kind.removeprefix("struct ").removeprefix("union ")
    return owner.get(kind) or owner.get(f"struct {bare}") or owner.get(f"union {bare}") or owner.get(bare)


def member(identity: Identity, offset: int, use: Use, owner: Mapping[str, Identity], depth: int = 0) -> str | None:
    """The member path (`.a`, `.slots[3].hp`) of the access `use` at `offset` into the type, through its arrays and
    nested records; None when no member of the access's size and kind sits there."""
    for found in identity.fields:
        span, gap = found.size * found.count, offset - found.offset
        inside = found.count and 0 <= gap < span and not gap % found.size
        if inside and _fits(replace(found, offset=offset, count=1), replace(use, offset=offset)):
            at = (offset - found.offset) // found.size
            return f".{found.name}" + (f"[{at}]" if found.count > 1 else "")
    if depth >= _DEPTH:
        return None
    for found in identity.records:
        if found.count and found.size and found.offset <= offset < found.offset + found.size * found.count:
            at, rest = divmod(offset - found.offset, found.size)
            inner = _owner(owner, found.type)
            path = member(inner, rest, use, owner, depth + 1) if inner else None
            if path:
                return f".{found.name}" + (f"[{at}]" if found.count > 1 else "") + path
    return None


def _named(identity: Identity, name: str) -> Field | None:
    return next((f for f in (*identity.fields, *identity.records) if f.name == name), None)


def typed(
    expression: str, types: Mapping[str, str], scope: Scope, owner: Mapping[str, Identity]
) -> tuple[str, int, bool] | None:
    """(element type, stars, is an array) of an lvalue expression (`p`, `g`, `p->a.b[2]`) from the function's
    declarations, the globals the draft sees and the member types of the records it walks; None when unknown."""
    found = _EXPRESSION.fullmatch(expression.replace(" ", ""))
    if found is None:
        return None
    root = found[1]
    if root in types:
        kind, stars, array = types[root], 1, False
    elif root in scope.globals:
        seen = scope.globals[root]
        kind, stars, array = seen.type, seen.stars, seen.array
    else:
        return None
    for step in _STEP.finditer(found[2]):
        if step[3] is not None:  # an index
            if array:
                array = False
            elif stars:
                stars -= 1
            else:
                return None
            continue
        if step[1] is not None:  # -> dereferences first
            if array:
                array = False
            elif stars:
                stars -= 1
            else:
                return None
        if stars or array:
            return None
        identity = _owner(owner, kind)
        got = _named(identity, step[1] or step[2]) if identity else None
        if got is None or not got.type:
            return None
        kind, stars, array = got.type.replace("*", "").strip(), got.type.count("*"), got.count > 1
    return kind, stars, array


def _element(element: str, use: Use) -> bool:
    """Whether the access reads one element of a pointer to (or array of) the scalar `element`: the same type, or a
    4-byte integer of either sign, at a whole element offset."""
    size = SCALARS.get(element)
    if not size or element == "void" or use.size != size or use.offset < 0 or use.offset % size:
        return False
    if element == use.type:
        return True
    return size == 4 and use.kind == "i" and (element in FLOATS) == (use.type in FLOATS) and use.type in SCALARS


def _place(use: Use, types: Mapping[str, str], scope: Scope, owner: Mapping[str, Identity]) -> tuple[str, str, str]:
    """(replacement, header to include, declared type of the base) for one access; the replacement is "" when no
    member is found. A record or pointer base names its member; an address of a symbol that sits inside a record
    global the program declares names that record's member at the symbol's offset in it."""
    base = use.base.replace(" ", "")
    address = base.startswith("&")
    whole = base.removeprefix("&")
    got = typed(whole, types, scope, owner)
    kind = ""
    if got:
        element, stars, array = got
        identity = _owner(owner, element)
        if (stars, array) == ((0, False) if address else (1, False)):
            kind = "" if element == "void" else element
            path = member(identity, use.offset, use, owner) if identity else None
            if path:
                return (f"{whole}{path}" if address else f"{whole}->{path[1:]}"), "", kind
            if identity and not address and identity.size and use.offset >= identity.size:  # a later element
                at, rest = divmod(use.offset, identity.size)
                path = member(identity, rest, use, owner)
                if path and re.fullmatch(r"\w+", whole):
                    return f"{whole}[{at}]{path}", "", kind
        if identity is None and _element(element, use) and (stars, array) == ((0, True) if address else (1, False)):
            return f"{whole}[{use.offset // use.size}]", "", kind  # type: ignore[operator]
        if stars == 0 and array and identity and identity.size and use.offset >= 0:  # an array of records
            kind = element
            at, rest = divmod(use.offset, identity.size)
            path = member(identity, rest, use, owner)
            if path:
                return f"{whole}[{at}]{path}", "", kind
    record = got is not None and _owner(owner, got[0]) is not None
    if address and not record and re.fullmatch(r"\w+", whole) and whole in scope.addresses:
        at = scope.addresses[whole] + use.offset
        for name, seen in (*scope.globals.items(), *scope.elsewhere.items()):
            if name == whole or seen.stars or seen.array or name not in scope.addresses:
                continue
            identity = _owner(owner, seen.type)
            start = scope.addresses[name]
            if identity is None or not identity.size or not start <= at < start + identity.size:
                continue
            path = member(identity, at - start, use, owner)
            if path:
                return f"{name}{path}", "" if name in scope.globals else seen.path, ""
    return "", "", kind


def _pass(text: str, known: Sequence[Identity], limits: Mapping[str, int], scope: Scope) -> Result:
    types = declared_types(text)
    visible = Scope({**scope.globals, **globals_in(text)}, scope.addresses, scope.elsewhere)
    owner = owners(known)
    changes: list[tuple[int, int, str]] = []
    headers: list[str] = []
    left: list[Left] = []
    seen = uses(text)
    for use in seen:
        new, header, kind = _place(use, types, visible, owner)
        identity = _owner(owner, kind) if kind else None
        if new:
            changes.append((use.start, use.end, new))
            headers += [header] if header else []
        else:
            elsewhere = identity is not None and any(f.offset == use.offset for f in identity.fields)
            made = [u for u in seen if u.base == use.base]
            reach = max(u.offset + (u.size or 0) for u in made)
            fits = (
                ()
                if kind
                else tuple(
                    i.names[0]
                    for i in known
                    if i.spelled
                    and reach <= extent(i) <= limits.get(use.base.removeprefix("&"), extent(i))
                    and all(any(_fits(f, u) for f in i.fields) for u in made)
                )
            )
            left.append(Left(use.base, use.offset, kind, identity.names if identity else (), elsewhere, fits[:3]))
    nested = [c for c in changes if any(o[0] < c[0] and c[1] <= o[1] for o in changes)]
    for start, end, new in sorted((c for c in changes if c not in nested), reverse=True):
        text = text[:start] + new + text[end:]
    for header in dict.fromkeys(headers):
        text = including(text, header)
    return Result(_retype_locals(text, visible, owner), len(changes) - len(nested), tuple(left))


def _spelled(owner: Mapping[str, Identity], kind: str) -> str:
    """How C names the record type `kind`: its typedef name when it has one, else `struct tag`."""
    identity = _owner(owner, kind)
    names = {n for v in identity.views for n in (v.name, *v.aliases)} if identity else set()
    if kind.startswith(("struct ", "union ")):
        return kind
    return kind if kind in names else f"{'union' if identity and identity.views[0].union else 'struct'} {kind}"


def _retype_locals(text: str, scope: Scope, owner: Mapping[str, Identity]) -> str:
    """A `void *` local whose every assignment stores one pointer-to-record expression (a member a pass named, a typed
    parameter or global) declared as that pointer type, so the accesses through it name members in the next pass.
    A local used in pointer arithmetic keeps its type: the new one would scale it."""
    opened = re.search(r"^[^\n(=;#]*\w\(.*\) \{$", text, re.M)
    if opened is None:
        return text
    body = text[opened.end() :]
    types = declared_types(text)
    changed = body
    for found in re.finditer(r"^([ \t]*)void \*(\w+);$", body, re.M):
        name = found[2]
        values = re.findall(rf"(?<![\w.>]){name} = ([^;]+);", body)
        if not values or re.search(rf"\b{name}\s*[-+\[]|[-+]\s*{name}\b", body):
            continue
        kinds = {typed(v.strip(), types, scope, owner) for v in values}
        got = kinds.pop() if len(kinds) == 1 else None
        if got is None or got[1] != 1 or got[2] or not (identity := _owner(owner, got[0])) or not identity.spelled:
            continue
        line = f"{found[1]}{_spelled(owner, got[0])} *{name};"
        changed = re.sub(rf"^{re.escape(found[0])}$", line.replace("\\", "\\\\"), changed, count=1, flags=re.M)
    return text[: opened.end()] + changed


def resolve(text: str, known: Sequence[Identity], limits: Mapping[str, int], scope: Scope | None = None) -> Result:
    """Each M2C_FIELD whose base has a declared record type (a parameter, local or global the draft sees, or a member
    named in an earlier pass) as that member; the rest left with what is known of them."""
    scope = scope or Scope()
    resolved, result = 0, Result(text, 0, ())
    for _ in range(_PASSES):
        before = result.text
        result = _pass(before, known, limits, scope)
        resolved += result.resolved
        if result.text == before or not result.left:
            break
    return Result(result.text, resolved, result.left)
