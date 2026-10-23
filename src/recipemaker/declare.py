"""Declaration items: a name declared more than one way. The fix is one change set (the prototype in the definition's
family header, the other copies deleted, the includes and the casts the compiler demands) that unbake lands as one
commit, every touched unit proved exact. `next` plans the best-ranked name, `try` proves it and lands it as its own
commit; a name that fails is retried alone with casts."""

import difflib
import json
import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from unbake import api
from unbake.contracts import Config, Json, Proof, Snapshot

from recipemaker import declplan, errors, headers, host, index, judge, ladder, settle, state, twin, world

_BLOCKED = 10  # blocked items named in the brief
WINDOW = 40  # the best-ranked names `next` plans from
_SPELLING = re.compile(r"^[^:]*: (\S+?):(\d+) (.*)$")
_WIDTHS = {"char": 1, "u8": 1, "s8": 1, "short": 2, "u16": 2, "s16": 2, "int": 4, "u32": 4, "s32": 4, "f32": 4}
_WIDTHS |= {"float": 4, "long": 4, "s64": 8, "u64": 8, "f64": 8, "double": 8, "unsigned": 4}


def spellings(rows: tuple[str, ...]) -> list[tuple[str, int, str]]:
    """(file, line, text) of each declaration a conflict names."""
    return [(m[1], int(m[2]), m[3]) for row in rows if (m := _SPELLING.match(row))]


def conflicts(config: Config, snapshot: Snapshot) -> list[Json]:
    """Per conflicting name: its spellings, its landed definition, the files that use it, the items waiting on it."""
    waiting: dict[str, list[str]] = {}
    for item, symbols in sorted(state.blocked(config).items()):
        for name in symbols:
            waiting.setdefault(name, []).append(item)
    rows = []
    for f in headers.disagreements(snapshot)[1]:
        found = f.symptoms.get("definition")
        rows.append(
            {
                "name": f.unit,
                "spellings": spellings(f.missing),
                "definition": (found["path"], found["line"], found["signature"]) if found else None,
                "consumers": f.symptoms.get("consumers", 0),
                "items": waiting.get(f.unit, []),
            }
        )
    return sorted(rows, key=lambda row: (-len(row["items"]), -row["consumers"], row["name"]))


def _width(spelled: str, name: str) -> int | None:
    """The bytes a data spelling gives its name (a scalar, a pointer or an array of those), None when unknown."""
    kind = headers._type_of(spelled, name)
    if not kind or "(" in kind:
        return None
    base = re.sub(r"\b(?:extern|const|volatile)\b", "", kind.split("@")[0])
    size = 4 if "*" in base else _WIDTHS.get(" ".join(w for w in base.split() if w not in ("signed", "unsigned")), 0)
    size = size or (_WIDTHS.get(base.split()[-1], 0) if base.split() else 0)
    count = 1
    for dim in re.findall(r"\[(\w*)\]", kind):
        count *= int(dim, 0) if dim.isdigit() or dim.startswith("0x") else 0
    return size * count if size and count else None


def critical(snapshot: Snapshot, row: Json) -> int:
    """How much work waits on this name: the open functions (asm still in nonmatchings/) that call or touch it, the
    items blocked on it, and a record width mismatch between its spellings, which counts as critical on its own."""
    name = row["name"]
    members = world.of(snapshot).members
    seen = index.get(snapshot)
    users = {*seen.callers.get(name, ()), *(u[0] for u in seen.uses.get(name, ()))}
    open_users = sum(1 for u in users if u in members and not members[u].landed)
    widths = {w for _, _, text in row["spellings"] if (w := _width(text, name)) is not None}
    return open_users + len(row["items"]) + 1000 * (len(widths) > 1)


def ranked(config: Config, snapshot: Snapshot) -> list[Json]:
    """The conflicts, critical-first: names the open functions need (and width mismatches) before data and hygiene."""
    rows = []
    for row in conflicts(config, snapshot):
        rows.append({**row, "critical": critical(snapshot, row)})
    return sorted(rows, key=lambda r: (-r["critical"], -len(r["items"]), -r.get("consumers", 0), r["name"]))


def folder(config: Config, name: str) -> Path:
    return config.project.root / ".unbake/recipe/work" / f"{host.stem(name)}.decl"


def is_item(config: Config, name: str) -> bool:
    return folder(config, name).is_dir()


def _evidence(snapshot: Snapshot, name: str) -> list[str]:
    unit = world.unit_of(snapshot, name)
    source = index.source(snapshot, unit.path) if unit else ""
    head = twin.function(source, name) if source else None
    out = [f"defined in {unit.path}: {head.split('{')[0].strip()}"] if head and unit else []
    return out or ["no landed definition: choose the type from how the callers use it"]


# ---------------------------------------------------------------- the plan


@dataclass(frozen=True)
class Plan:
    """The change set of one name: the files it rewrites (full texts) and what it did."""

    name: str
    edits: dict[str, bytes]
    header: str = ""
    removed: int = 0  # copies of the declaration deleted
    includes: int = 0  # include lines added
    casts: int = 0  # call sites cast back to the type their old declaration took
    critical: int = 0
    cast_needed: bool = False  # an old copy took scalar types the definition does not: a cast variant exists
    blocked: tuple[str, ...] = ()  # differences a cast may not bridge (a pointer view)
    left: tuple[str, ...] = ()  # spellings this plan did not touch (a block-scope or conditional declaration)
    waits: tuple[str, ...] = ()  # names whose own conflicts must be unified before this one can be
    risky: tuple[str, ...] = ()  # the units whose old copy took other types: the ones the screen compiles first
    skip: str = ""  # why no change set was planned

    @property
    def files(self) -> frozenset[str]:
        return frozenset(self.edits)


def family(snapshot: Snapshot, path: str, spelled: Sequence[str] = ()) -> str:
    """The header the definition's unit declares into: the include/ header named after the unit (the one it includes
    by its own stem). A unit with none puts its declaration in the header of its folder that it includes, else in one
    of the headers that declare the name now. "" when the unit reaches no header: a header the tree does not have yet
    cannot be proved, because unbake reads the other files of a change set without it."""
    text = snapshot.peek(path) or b""
    found = world.header_of(path, text)
    if found and snapshot.peek(found) is not None:
        return found
    stem = posixpath.splitext(path)[0].removeprefix("src/").lower()
    named = {p.lower(): p for p in headers.sources(snapshot)}  # the header the folder layout gives the unit
    if conventional := next((named[k] for e in (".h", ".hpp") if (k := f"include/{stem}{e}") in named), ""):
        return conventional
    folder = posixpath.dirname(path).removeprefix("src/").lower()
    included = [
        h
        for n in world.INCLUDE.findall(text.decode(errors="replace"))
        if snapshot.peek(h := posixpath.normpath(posixpath.join("include", n))) is not None
    ]
    near = [h for h in included if posixpath.dirname(h).removeprefix("include/").lower() == folder]
    declaring = [h for h in spelled if headers.is_header(h) and snapshot.peek(h) is not None]
    return next(iter([*near, *declaring, *included]), "")


def _users(snapshot: Snapshot, name: str) -> list[str]:
    """The sources and headers whose text mentions `name`."""

    def build() -> dict[str, frozenset[str]]:
        paths = [*headers.sources(snapshot), *headers.sources(snapshot, "src", ".c")]
        return {p: s["words"] for p, s in zip(paths, headers._scanned(snapshot, paths), strict=True)}

    words = api.memo(("recipe.decl.words", snapshot.digest), build)
    return sorted(p for p, w in words.items() if name in w)


def _typed(snapshot: Snapshot, header: str, prototype: str, text: str, read: Callable[[str], str | None]) -> str:
    """`text` of `header` with an include of every header that declares a type the prototype names, unless the header
    already sees it (or that header would include this one back)."""
    catalog = headers.catalog(snapshot)
    for token in dict.fromkeys(re.findall(r"\b(?:struct|union|enum)\s+\w+|\b[A-Za-z_]\w*", prototype)):
        found = catalog.get(" ".join(token.split()))
        if not found or not re.match(r"(?:typedef|struct|union|enum)\b", found[2]):
            continue
        if (
            found[0] != header
            and not declplan.reaches(header, found[0], read)
            and not declplan.reaches(found[0], header, read)
        ):
            text = declplan.add_include(text, found[0])
    return text


def _clashes(snapshot: Snapshot, header: str, users: list[str], read: Callable[[str], str | None]) -> list[str]:
    """The names a header (with everything it includes) declares differently from a file-scope declaration of one of
    the `users` that is about to include it: the include would stop that unit compiling."""
    closure = [header]
    for path in closure:
        for spelled in world.INCLUDE.findall(read(path) or ""):
            if (found := declplan.resolve(path, spelled, lambda p: read(p) is not None)) and found not in closure:
                closure.append(found)

    def kinds(paths: list[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for scanned in headers._scanned(snapshot, paths):
            for n, _, text in scanned["declared"]:
                if not text.startswith("typedef"):
                    shown = headers._unnamed(headers._normal(headers._type_of(text, n)))
                    out.setdefault(n, set()).add(shown)
        return out

    theirs, bad = kinds([p for p in closure if snapshot.peek(p) is not None]), set()
    for path in users:
        for n, spelled in kinds([path]).items():
            if n in theirs and not spelled <= theirs[n]:
                bad.add(n)
    return sorted(bad)


def plan(snapshot: Snapshot, row: Json, casts: bool = False) -> Plan:
    """The change set that declares `row`'s name once. With `casts`, the call sites whose old declaration took other
    scalar types are cast back to them; the plain plan leaves them alone, and the compiler says whether they matter."""
    name, weight = row["name"], row.get("critical", 0)
    if not row.get("definition"):
        return Plan(name, {}, critical=weight, skip="no landed definition: leave it reported, never guess the type")
    where, _, spelled = row["definition"]
    prototype = " ".join(spelled.split())
    if re.search(r"\b(?:static|inline)\b", prototype):
        return Plan(name, {}, critical=weight, skip=f"the definition is file-local: {prototype}")
    new = declplan.split_signature(prototype, name)
    if new is None:
        return Plan(name, {}, critical=weight, skip=f"the definition is no function prototype: {prototype}")
    header = family(snapshot, where, [p for p, _, _ in row["spellings"]])
    if not header:
        return Plan(name, {}, critical=weight, skip=f"{where} includes no header to declare into")
    texts: dict[str, str] = {}

    def read(path: str) -> str | None:
        return declplan.overlay(texts, snapshot.peek)(path)

    old: dict[str, tuple[str, list[str]]] = {}
    headed: dict[str, tuple[str, list[str]]] = {}
    before = declplan.overlay({}, snapshot.peek)
    removed = 0
    seen: set[tuple[str, int]] = set()
    for path in sorted({p for p, _, _ in row["spellings"]} - {header}):
        text = read(path)
        if text is None:
            continue
        for one in reversed(declplan.copies(text, name)):
            seen.add((path, text.count("\n", 0, one.start) + 1))
            if signature := declplan.split_signature(one.prototype, name):
                (headed if headers.is_header(path) else old).setdefault(path, signature)
            text = declplan.remove(text, one, name)
            removed += 1
        texts[path] = text
    texts[header] = _typed(snapshot, header, prototype, declplan.put(read(header) or "", name, prototype), read)
    added, blocked, cast_count, joining = 0, [], 0, []
    for path in _users(snapshot, name):
        if path == header or not headers.is_source(path):
            continue
        text = read(path)
        if text is not None and not declplan.reaches(path, header, read):
            texts[path] = declplan.add_include(text, header)
            joining.append(path)
            added += 1
    if clash := [n for n in _clashes(snapshot, header, joining, read) if n != name]:
        why = (
            f"waits for {', '.join(clash[:6])}: {header} declares them another way than the units that would include it"
        )
        return Plan(name, {}, header, critical=weight, skip=why, waits=tuple(clash))
    for path in _users(snapshot, name):  # a unit with no copy of its own saw the type the header it includes gave
        if headers.is_source(path) and path not in old:
            given = next((s for h, s in headed.items() if declplan.reaches(path, h, before)), None)
            if given:
                old[path] = given
    differs = {  # a copy that took no parameters says nothing about the types the calls passed
        p: s
        for p, s in old.items()
        if not (declplan.same(s[0], new[0]) and len(s[1]) == len(new[1]) and all(map(declplan.same, s[1], new[1])))
        and (s[1] or not new[1])
    }
    for path, signature in sorted(differs.items()):
        if (text := read(path)) is not None and headers.is_source(path) and path != where:
            done = declplan.cast_calls(text, name, signature, new)
            blocked += done.blocked
            if casts and not done.blocked:
                texts[path], cast_count = done.text, cast_count + done.count
    edits = {p: t.encode() for p, t in texts.items() if t.encode() != snapshot.peek(p)}
    left = tuple(
        f"{p}:{n}"
        for p, n, _ in row["spellings"]
        if p != header and (p, n) not in seen and (p, n) != (where, row["definition"][1])
    )
    if not edits:
        why = (
            "no file-scope copy to merge: " + (", ".join(left[:3]) or "none found") + " are block-scope or conditional"
        )
        return Plan(name, {}, header, critical=weight, skip=why, left=left)
    return Plan(
        name, edits, header, removed, added, cast_count, weight, bool(differs), tuple(dict.fromkeys(blocked)), left,
        risky=tuple(sorted(differs)),
    )  # fmt: skip


def _headers(snapshot: Snapshot, row: Json) -> tuple[list[str], str]:
    """The headers the single declaration may go in, and the one that gets it: the definition's family header, else
    the header that declares the name today, else the first header the defining unit includes."""
    spelled = sorted({p for p, _, _ in row["spellings"]})
    known = set(headers.sources(snapshot))
    unit = world.unit_of(snapshot, row["name"])
    texts = [index.source(snapshot, p) for p in ([unit.path] if unit else spelled) if headers.is_source(p)]
    names = (n for t in texts for n in world.INCLUDE.findall(t))
    included = [p for p in dict.fromkeys(f"include/{n}" for n in names) if p in known]
    declaring = [p for p in spelled if headers.is_header(p)]
    chosen = family(snapshot, row["definition"][0], spelled) if row.get("definition") else ""
    return sorted({*declaring, *included, *([chosen] if chosen else [])}), chosen or next(
        iter([*declaring, *included]), ""
    )


def _brief(snapshot: Snapshot, row: Json, where: Path, made: Plan | None = None) -> str:
    files = sorted({path for path, _, _ in row["spellings"]})
    lines = [f"# {row['name']} (declaration)", "", f"declared {len(row['spellings'])} ways in {len(files)} files", ""]
    if row.get("definition"):
        path, number, text = row["definition"]
        lines += [f"canonical: the landed definition {path}:{number} `{text}`; declare exactly that, once", ""]
    if made and made.edits:
        lines += [
            "## Planned",
            f"The copies under {where} already hold the change set: the prototype in {made.header}, "
            f"{made.removed} other copies deleted, {made.includes} includes added, {made.casts} call sites cast.",
            *(
                [f"A cast variant exists ({made.cast_needed}); pointer differences: {made.blocked}"]
                * bool(made.blocked)
            ),
            "Edit them only to improve it; recipe try proves the lot.",
            "",
        ]
    elif made and made.skip:
        lines += ["## Not planned", made.skip, ""]
    lines += ["## Spellings", *[f"- {p}:{n}  {t}" for p, n, t in row["spellings"]], "", "## Evidence"]
    lines += [f"- {line}" for line in _evidence(snapshot, row["name"])]
    items = row["items"]
    lines += ["", f"## Blocks {len(items)} items", *[f"- {i}" for i in items[:_BLOCKED]]]
    lines += [f"... {len(items) - _BLOCKED} more"] * (len(items) > _BLOCKED)
    candidates, chosen = _headers(snapshot, row)
    lines += ["", "## Headers", *([f"- {p}" for p in candidates] or ["- none: create one and include it"])]
    target = f"Put it in {chosen}." if chosen else "No candidate header: create include/<name>.h and include it."
    lines += [
        "",
        "## Do",
        f"Edit the copies under {where} (same paths as the project). Declare the name once, with one",
        f"type, in a header. {target} Delete every other declaration; keep everything else as it is.",
        "",
    ]
    lines += [f"next: recipe try {row['name']}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- handing out


def next_item(config: Config, snapshot: Snapshot, now: float) -> Json:
    """Lease the best-ranked declaration item that has a change set to plan (else the best one, copies only)."""
    hidden = state.skipped(config, now) | {lease.item for lease in state.leases(config, now)}
    rows = [r for r in ranked(config, snapshot) if r["name"] not in hidden]
    if not rows:
        return {"item": "", "why": "no declaration conflict is open", "next": "recipe status"}
    window = [r for r in rows if r.get("definition")][:WINDOW]
    row = next((r for r in window if (p := plan(snapshot, r)).edits and not p.skip), (window or rows)[0])
    return take(config, snapshot, row, now)


def _write(config: Config, snapshot: Snapshot, row: Json, made: Plan | None, now: float) -> None:
    where = folder(config, row["name"])
    host.clear(where)
    if made and made.edits:
        for path, text in made.edits.items():
            host.write(where / path, text)
    else:
        for path in sorted({p for p, _, _ in row["spellings"]} | set(_headers(snapshot, row)[0])):
            host.write(where / path, snapshot.peek(path) or b"")
    host.write(where / "brief.md", _brief(snapshot, row, where, made).encode())
    host.write(where / "plan.json", json.dumps({"risky": list(made.risky) if made else []}).encode())
    state.take(config, row["name"], now)


def _handed(config: Config, row: Json, made: Plan | None, now: float) -> Json:
    lease = state.lease(config, row["name"])
    where = folder(config, row["name"])
    files = len({p for p, _, _ in row["spellings"]})
    expires = lease.expires if lease else now
    return {
        "item": row["name"],
        "kind": "declaration",
        "size": 0,
        "regions": 0,
        "why": f"declared {len(row['spellings'])} ways in {files} files; blocks {len(row['items'])} items",
        "aims": [],
        "evidence": "",
        "brief": str(where / "brief.md"),
        "planned": bool(made and made.edits),
        "lease_until": state.stamp(expires),
        "idle_minutes": state.settings().idle_minutes,
        "stall_tries": state.settings().stall_tries,
        "next": f"read the brief, edit the copies, then run: recipe try {row['name']}",
    }


def take(config: Config, snapshot: Snapshot, row: Json, now: float) -> Json:
    """Lease the declaration item `row` names and write its work folder: the planned change set, else copies."""
    made = plan(snapshot, row)
    _write(config, snapshot, row, made, now)
    return _handed(config, row, made, now)


# ---------------------------------------------------------------- proving


def _sidecar(config: Config, name: str) -> Json:
    """What `next` left beside the copies: the units to screen first."""
    listing = folder(config, name) / "plan.json"
    return json.loads(listing.read_text()) if listing.is_file() else {}


def _edits(config: Config, snapshot: Snapshot, name: str) -> dict[str, bytes]:
    where = folder(config, name)
    found = {p.relative_to(where).as_posix(): p.read_bytes() for p in sorted(where.rglob("*")) if p.is_file()}
    found.pop("brief.md", None)
    found.pop("plan.json", None)
    return {path: text for path, text in found.items() if text != snapshot.peek(path)}


def _primary(snapshot: Snapshot, edits: dict[str, bytes]) -> str:
    """The landed unit the change set is submitted under: an edited one, else one that includes an edited header."""
    found = ladder.primary(snapshot, edits)
    if found is None:
        raise state.stop(
            "recipe.no-unit", "no landed unit includes the edited files", action="recipe skip NAME --reason other"
        )
    return found


def _changed(old: bytes | None, new: bytes) -> set[int]:
    """The 1-based numbers of the lines of `new` that `old` does not have."""
    before, after = (old or b"").decode(errors="replace").splitlines(), new.decode(errors="replace").splitlines()
    ops = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    return {n for tag, _, _, j1, j2 in ops if tag != "equal" for n in range(j1 + 1, j2 + 1)}


_STATEMENT = re.compile(r"^(?![ \t])[^\n#{};]*;[ \t]*$", re.M)  # file scope: never an indented local


def _normal(line: str) -> str:
    return " ".join(line.split()).removeprefix("extern ")


def _drop_repeats(snapshot: Snapshot, edits: dict[str, bytes], name: str) -> tuple[dict[str, bytes], list[str]]:
    """Every edited source without the file-scope declarations of `name` repeating word for word one in an included
    header."""
    out, notes = dict(edits), []
    for path, data in edits.items():
        text = data.decode(errors="replace")
        if not headers.is_source(path):
            continue
        heads = (
            out[h] if (h := f"include/{n}") in out else snapshot.peek(h) or b"" for n in world.INCLUDE.findall(text)
        )
        given = {_normal(m[0]) for h in heads for m in _STATEMENT.finditer(h.decode(errors="replace"))}
        word = re.compile(rf"\b{re.escape(name)}\b")
        lines = text.splitlines(keepends=True)
        kept = [x for x in lines if not (_STATEMENT.match(x) and word.search(x) and _normal(x) in given)]
        if len(lines) > len(kept):
            out[path] = "".join(kept).encode()
            notes.append(f"dropped {len(lines) - len(kept)} repeated externs from {path}")
    return out, notes


def _score(config: Config, name: str) -> float:
    return max(
        (state.worst(r["score"]) for r in host.rows(config, "recipe/tries") if r["item"] == name),
        default=0.0,
    )


def _banned(snapshot: Snapshot, name: str, edits: dict[str, bytes], record: str = "") -> tuple[str, str, str] | None:
    """(key, reason, action) when an edited source holds text no landing may have on a line the edit touched. A type
    item's own cast of a byte view to the record it defines (`(Rec *)`) is none."""
    declared = index.get(snapshot).declarations
    for path, text in edits.items():
        touched = _changed(snapshot.peek(path), text)
        hits = [
            h
            for h in errors.banned(text.decode(errors="replace"), declared)
            if h[3] in touched and not (record and f"({record} *)" in h[4])
        ]
        if hits and headers.is_source(path):
            return hits[0][0], f"{path} holds {hits[0][1]} on line {hits[0][3]}", f"fix it, then run: recipe try {name}"
    return None


@dataclass
class _Try:
    """One name of a proof: its edits and what the proof said."""

    name: str
    edits: dict[str, bytes]
    fixed: list[str]
    proofs: tuple[Proof, ...] = ()
    landed: str = ""
    refused: list[Json] = field(default_factory=list)
    exact: bool = False
    risky: tuple[str, ...] = ()
    message: str = ""  # a type item's commit message (planned with its change set)
    function: str = ""  # a type item: the function that waits for the type


def _prepare(config: Config, snapshot: Snapshot, name: str) -> _Try:
    """The edits of `name` ready to prove, or its refusal (nothing changed; a banned line)."""
    edits = _edits(config, snapshot, name)
    if not edits:
        raise state.stop(
            "recipe.no-change",
            f"the copies of {name} are unchanged",
            unit=name,
            action=f"edit the copies, then run: recipe try {name}",
        )
    side = _sidecar(config, name)
    if hit := _banned(snapshot, name, edits, side.get("record", "")):
        raise state.stop(hit[0], hit[1], unit=name, action=hit[2])
    edits, fixed = _drop_repeats(snapshot, edits, name)
    return _Try(
        name,
        edits,
        fixed,
        risky=tuple(side.get("risky", ())),
        message=side.get("message", ""),
        function=side.get("function", ""),
    )


def _screen(config: Config, snapshot: Snapshot, tries: list[_Try]) -> None:
    """Measure every change set in one process and mark the ones that are exact in the reference version."""
    units = [world.of(snapshot).units[_primary(snapshot, t.edits)] for t in tries]
    sets = [(unit, t.edits, t.risky) for unit, t in zip(units, tries, strict=True)]
    for t, verdict in zip(tries, ladder.screen(config, snapshot, sets), strict=True):
        t.proofs, t.exact = verdict.proofs, verdict.exact


def _message(one: _Try) -> str:
    """The commit message of a set: the bug it fixes, then what it changed."""
    if one.message:
        return one.message
    word = re.compile(rf"\b{re.escape(one.name)}\b")  # the edited header that now says the name is the one it goes in
    header = next((p for p, t in sorted(one.edits.items()) if headers.is_header(p) and word.search(t.decode())), "")
    where = f" in {header.removeprefix('include/')}" if header else ""
    return (
        f"Declare {one.name} once{where} from its definition\n\n"
        f"{len(one.edits)} files: the other declarations are gone and each unit that used one includes the header.\n"
        "Every version's ROM builds byte-identical (make check)."
    )


def _land(config: Config, snapshot: Snapshot, one: _Try) -> None:
    """Write the set, prove every ROM, commit it: one commit for this name. A refusal says what differs."""
    done = settle.land(config, {p: snapshot.peek(p) for p in one.edits}, one.edits, _message(one))
    one.landed = done.get("commit", "")
    if problem := done.get("problem"):
        one.proofs = (*one.proofs, judge.proof_failed(one.name, problem, "land.not_exact"))
    one.exact = bool(one.landed)


def _retry(config: Config, snapshot: Snapshot, one: _Try) -> _Try | None:
    """The cast variant of a name whose plain plan did not prove exact: its edits when the folder still holds the
    plain plan untouched and a scalar cast can bridge the old types, else None."""
    row = next((r for r in conflicts(config, snapshot) if r["name"] == one.name), None)
    if row is None:
        return None
    plain, cast = plan(snapshot, row), plan(snapshot, row, casts=True)
    if plain.edits != {p: t for p, t in one.edits.items()} or not cast.casts or _banned(snapshot, one.name, cast.edits):
        return None
    return _Try(one.name, dict(cast.edits), [*one.fixed, f"cast {cast.casts} call sites back to their old types"])


def _result(one: _Try, dry: bool) -> Json:
    proofs = one.proofs
    score = {
        v: round(min(p.score for p in proofs if p.version == v), 4) for v in sorted({p.version for p in proofs} - {""})
    }
    word = "exact" if one.landed else "refused" if one.refused else "fuzzy" if any(score.values()) else "failed"
    word = "would-land" if dry and one.exact else word
    why = [x for f in one.refused for x in (f["reason"], *f["missing"])] or [m for p in proofs for m in p.missing][:3]
    return {
        "item": one.name,
        "result": word,
        "compiled": not any("compile.error" in m for p in proofs for m in p.missing),
        "score": score,
        "landed": one.landed,
        "auto_fixes": one.fixed,
        "why": why,
        "compiler": [m.partition(": ")[2] for p in proofs for m in p.missing if "compile.error" in m][:3],
    }


def run(config: Config, name: str, now: float, dry: bool = False) -> Json:
    """Prove the leased change set of `name` and land it as its own commit when exact; one that is not exact is retried
    with casts. With `dry` nothing lands: the set is measured in the reference version only."""
    snapshot = world.capture(config)
    one = _prepare(config, snapshot, name)
    _screen(config, snapshot, [one])
    if one.function and one.exact:  # the bodies a type item's call sites changed carry their provenance
        for path, data in sorted(one.edits.items()):
            if headers.is_source(path):
                state.record(config, name, now, "exact", {"us": 1.0}, text=data.decode(errors="replace"), path=path)
    if not dry:
        if one.exact:
            _land(config, snapshot, one)
        elif variant := _retry(config, snapshot, one):  # alone, with the casts the old declarations need
            _land(config, snapshot, variant)
            one = variant if (variant.landed or variant.exact) else one
        lease = state.lease(config, name)
        score = max((p.score for p in one.proofs), default=0.0)
        if lease is not None:
            if one.refused:
                state.renew(config, name, now)
            else:
                state.count_try(config, name, now, score > _score(config, name))
    mine = _result(one, dry)
    if not dry:
        state.record(config, name, now, mine["result"], mine["score"])
        if one.landed:
            state.release(config, name)
            if one.function:
                from recipemaker import typeitem

                typeitem.done(config, one.function)
    lease = state.lease(config, name)
    rules = state.settings()
    return {
        **mine,
        "stalled": lease.tries if lease else 0,
        "stall_tries": rules.stall_tries,
        "minutes_left": state.minutes_left(lease, now) if lease else 0,
        "saved": "",
        "alike": [],
        "regions": {"matched": 0, "total": 0, "gained": None},
        "file": str(folder(config, name)),
        "brief": str(folder(config, name) / "brief.md"),
        "dry": dry,
        "next": "recipe next" if mine["landed"] else f"edit the copies, then run: recipe try {name}",
    }
