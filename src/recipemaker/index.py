"""What the verbs read of the project (declarations, identities, callers), each built on first read."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

from unbake import effort, headers, infer, pool, store, types, versions
from unbake.contracts import Finding, Json, Refusal, Snapshot, digest

from recipemaker import cparse, ledger

CALL = re.compile(r"\bjal\s+([\w.$]+)")
_MNEMONIC = re.compile(r"^\s*/\*[^*]*\*/\s+([a-z][\w.]*)", re.M)
REFERENCE = re.compile(r"%(?:hi|lo)\(([\w.$+]+)\)")
_IMMEDIATE = re.compile(r"[\s,](-?0x[0-9A-Fa-f]+|-?\d+)\s*(?:$|[,(])", re.M)
_TYPE = re.compile(r"(?:extern\s+)?(?:const\s+)?(?:struct\s+)?(\w+)")


Uses = tuple[tuple[str, str, int], ...]  # (function, mnemonic, byte offset into the symbol)


class Index:
    """What each verb reads of the project, built when first read: `next` reads only the declarations."""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def _once(self, name: str, build: Callable[[Snapshot], Any]) -> Any:
        with effort.stage(f"recipe.{name}"):
            return effort.memo((f"recipe.{name}", self.snapshot.digest), lambda: build(self.snapshot))

    @property
    def declarations(self) -> dict[str, tuple[str, int, str]]:  # name -> (path, line, text) from `headers.catalog`
        return self._once("declarations", lambda s: headers.catalog(s))  # type: ignore[no-any-return]

    @property
    def identities(self) -> tuple[ledger.Identity, ...]:
        return self._once("identities", _identities)  # type: ignore[no-any-return]

    @property
    def callers(self) -> dict[str, tuple[str, ...]]:
        return self._once("callers", _callers)  # type: ignore[no-any-return]

    @property
    def extents(self) -> dict[str, int]:
        """The bytes each symbol of the names version spans, up to the next symbol."""
        return self._once("extents", _extents)  # type: ignore[no-any-return]

    @property
    def uses(self) -> dict[str, Uses]:
        return self._once("uses", _uses)  # type: ignore[no-any-return]

    @property
    def globals(self) -> dict[str, tuple[dict[str, ledger.Global], tuple[str, ...]]]:
        """Per header the variables its `extern` lines declare and the project headers it includes."""
        return self._once("globals", _globals)  # type: ignore[no-any-return]

    @property
    def addresses(self) -> dict[str, int]:
        """The address of each symbol of the names version."""
        return self._once(  # type: ignore[no-any-return]
            "addresses", lambda s: dict(s.versions[s.config.project.names_from].symbols)
        )

    @property
    def shapes(self) -> dict[str, tuple[str, ...]]:
        """Shape -> its functions: mnemonics, calls, %hi/%lo references by first appearance, immediates."""
        return self._once("shapes", lambda s: persisted(s, "recipe.shapes", lambda: _shapes(s)))  # type: ignore[no-any-return]


def _sources(snapshot: Snapshot) -> list[tuple[str, str]]:
    """The digest of each header and types.toml, read once per snapshot."""
    return effort.memo(
        ("recipe.sources", snapshot.digest),
        lambda: [(p, digest(snapshot.peek(p))) for p in [*headers.sources(snapshot), "types.toml"]],
    )


def key(snapshot: Snapshot, kind: str, *, extra: object = (), sources: bool = False) -> str:
    """The content key of a value that is a pure function of the project: what the project is (layout, config, each
    version's ROM and facts, the headers when `sources`), the extra key and the digest of the installed unbake and
    recipemaker code. The snapshot digest is left out: it carries unbake's code_identity, which differs from one
    `recipe` process to the next."""
    facts = [(v.id, v.rom_sha256, versions.facts_digest(v)) for _, v in sorted(snapshot.versions.items())]
    text = _sources(snapshot) if sources else []
    config = snapshot.config.digest
    return digest((kind, snapshot.layout.digest, config, facts, text, extra, store.identity().partition(";")[0]))


def persisted[T](
    snapshot: Snapshot, kind: str, produce: Callable[[], T], *, extra: object = (), sources: bool = False
) -> T:
    """A value that is a pure function of the project, kept in the host content cache so the next command skips it."""
    return store.cached_object(snapshot.config, kind, key(snapshot, kind, extra=extra, sources=sources), produce)


def _join_key(shapes: list[set[tuple[int, int, str]]]) -> str:
    return digest(shapes)


def _scan(item: tuple[str, str, str]) -> tuple[str, str]:
    """The function's shape: a digest of its mnemonics, calls, references by first appearance and immediates."""
    path, name, _ = item
    text = Path(path).read_text(errors="replace")
    calls, references = CALL.findall(text), REFERENCE.findall(text)
    seen: dict[str, int] = {}
    evidence = (calls, [seen.setdefault(r, len(seen)) for r in references], _IMMEDIATE.findall(text))
    return name, digest((_MNEMONIC.findall(text), evidence)) if any(evidence) else ""


def _evidence(snapshot: Snapshot, names: set[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for finding in headers.disagreements(snapshot)[1]:
        text = [row.split(": ", 1)[-1].split(" ", 1)[-1].split("(")[0] for row in finding.missing]
        seen = [m[1] for t in text if (m := _TYPE.search(t))]
        pairs += [(a, b) for a, b in pairwise(seen) if a in names and b in names and a != b]
    return pairs


def _identities(snapshot: Snapshot) -> tuple[ledger.Identity, ...]:
    config = snapshot.config
    views = tuple(cparse.load(snapshot))
    pairs = pool.map(config, "recipe.joins", ledger.shared_pairs, [ledger.view_shapes(views)], _join_key)[0]
    return tuple(ledger.identities(views, _evidence(snapshot, {v.name for v in views}), pairs))


_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]+"([^"]+)"', re.M)


def _globals(snapshot: Snapshot) -> dict[str, tuple[dict[str, ledger.Global], tuple[str, ...]]]:
    out = {}
    for path in headers.sources(snapshot):
        text = source(snapshot, path)
        out[path] = (ledger.globals_in(text, path), tuple(f"include/{m}" for m in _INCLUDE.findall(text)))
    return out


def scope(snapshot: Snapshot, text: str) -> ledger.Scope:
    """What the draft `text` sees: the globals of the headers it includes, transitively, the first declaration of a
    name winning; the symbol addresses; the record globals of every other header."""
    table = get(snapshot).globals
    todo = [f"include/{m}" for m in _INCLUDE.findall(text)]
    seen: set[str] = set()
    visible: dict[str, ledger.Global] = {}
    while todo:
        path = todo.pop(0)
        if path in seen or path not in table:
            continue
        seen.add(path)
        rows, included = table[path]
        for name, row in rows.items():
            visible.setdefault(name, row)
        todo += included
    elsewhere = {
        name: row
        for path, (rows, _) in table.items()
        if path not in seen
        for name, row in rows.items()
        if not row.stars and not row.array and name not in visible
    }
    return ledger.Scope(visible, get(snapshot).addresses, elsewhere)


def resolve(snapshot: Snapshot, text: str) -> ledger.Result:
    """`ledger.resolve` over the project's types with what the draft sees."""
    found = get(snapshot)
    return ledger.resolve(text, found.identities, found.extents, scope(snapshot, text))


def _shapes(snapshot: Snapshot) -> dict[str, tuple[str, ...]]:
    config = snapshot.config
    version = config.project.names_from
    facts = versions.facts_digest(snapshot.versions[version])
    items = [
        (str(versions.asm_path(config, version, name)), name, facts)
        for name, member in sorted(snapshot.layout.members.items())
        if member.kind == "function" and any(p.version == version for p in member.placements)
    ]
    shapes: dict[str, list[str]] = {}
    for name, shape in pool.map(config, "recipe.scan", _scan, items, digest):
        if shape:
            shapes.setdefault(shape, []).append(name)
    return {k: tuple(v) for k, v in shapes.items()}


def _callers(snapshot: Snapshot) -> dict[str, tuple[str, ...]]:
    """Per function the functions of the reference version that call it, from unbake's cached scan."""
    callers: dict[str, list[str]] = {}
    for name, _, called, *_ in infer.scan(snapshot)[snapshot.config.project.names_from]:
        for target in called:
            callers.setdefault(target, []).append(name)
    return {k: tuple(v) for k, v in callers.items()}


def _extents(snapshot: Snapshot) -> dict[str, int]:
    symbols = snapshot.versions[snapshot.config.project.names_from].symbols
    order = sorted(set(symbols.values()))
    after = dict(pairwise(order))
    return {name: after[address] - address for name, address in symbols.items() if address in after}


def _uses(snapshot: Snapshot) -> dict[str, Uses]:
    """Per symbol the functions that read or write it directly, with the mnemonic and offset, from unbake's scan."""
    version = snapshot.config.project.names_from
    symbols = snapshot.versions[version].symbols
    order = sorted(set(symbols.values()))
    names = {address: name for name, address in sorted(symbols.items(), reverse=True)}
    found: dict[str, list[tuple[str, str, int]]] = {}
    for name, *_, use in infer.scan(snapshot)[version]:
        for address, _, mnemonic, how, _ in use["accesses"]:
            at = order[bisect_right(order, address) - 1] if how != "taken" and address >= order[0] else None
            if at is not None and names.get(at):
                found.setdefault(names[at], []).append((name, mnemonic, address - at))
    return {k: tuple(v) for k, v in found.items()}


def reference(snapshot: Snapshot, member: str) -> str:
    """Use names_from if the member holds it, else its lexically first holder, as unbake's Member.reference does."""
    item = snapshot.layout.members.get(member)
    if item is None or not item.placements:
        raise Refusal(Finding("types.usage", f"{member} is in no version of this layout", unit=member))
    return item.reference(snapshot.config.project.names_from)


def asm(snapshot: Snapshot, member: str, version: str = "") -> str:
    """The assembly of `member` in `version`, else in its reference version."""
    version = version or snapshot.layout.members[member].reference(snapshot.config.project.names_from)
    return versions.asm_path(snapshot.config, version, member).read_text(errors="replace")


def source(snapshot: Snapshot, path: str) -> str:
    """The text of a project file in the snapshot, "" when it has none."""
    return (snapshot.peek(path) or b"").decode(errors="replace")


def usage(snapshot: Snapshot, member: str) -> Json:
    """Read usage in names_from when held, else the member's lexically first version; refuse no versions by name."""
    version = reference(snapshot, member)
    return effort.memo(
        ("recipe.usage", snapshot.digest, member, version), lambda: types.usage(snapshot, member, version)
    )


def get(snapshot: Snapshot) -> Index:
    return Index(snapshot)
