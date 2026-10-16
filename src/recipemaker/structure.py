"""Module structure as JSON moves: evidence from `unbake map`, a proposal from it, batches piped to `unbake move -`."""

from __future__ import annotations

import json
import posixpath
import shutil
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict
from itertools import groupby, pairwise
from pathlib import Path
from typing import Any

from unbake import process, store
from unbake.contracts import Config, Finding, Json, Refusal

from recipemaker import clash, state, table, tree

Segments = str | Collection[str] | None


def _refusal(document: Json) -> Refusal:
    keep = {"versions": (), "missing": ()}
    found = [Finding(**{**f, **{k: tuple(f[k]) for k in keep}}) for f in document["findings"]]
    return Refusal(*(found or [Finding("recipe.unbake", "unbake failed without a finding")]))


def _unbake(
    config: Config, host: Path | None, *args: str, stdin: bytes = b"", clock: list[Json] | None = None
) -> tuple[Json, str]:
    """One unbake command through process.run: its JSON document and its stderr text. Its wall time goes to `clock`."""
    exe, root = str(Path(sys.executable).with_name("unbake")), config.project.root
    argv = [exe, *(["--host", str(host)] if host else []), "--project", str(root), *args]
    began = time.monotonic()
    ran = process.run("unbake", argv, cwd=root, tmp=process.scratch(root), stdin=stdin)
    if clock is not None:
        clock.append({"verb": args[0], "wall_s": round(time.monotonic() - began, 3)})
    lines = ran.stdout.decode(errors="replace").strip().splitlines()
    try:
        return json.loads(lines[-1]), ran.stderr.decode(errors="replace")
    except (ValueError, IndexError) as error:
        raise Refusal(Finding("recipe.unbake", f"unbake {args[0]} printed no JSON")) from error


def evidence(config: Config, host: Path | None = None, clock: list[Json] | None = None) -> Json:
    """Run `unbake map` and load `.unbake/map.json`, the only evidence channel."""
    document, _ = _unbake(config, host, "map", clock=clock)
    if not document["ok"]:
        raise _refusal(document)
    return dict(json.loads((config.project.root / document["result"]["path"]).read_text(encoding="utf-8")))


def _sections(m: Json) -> dict[str, dict[str, Any]]:
    """The placements an atom is ordered by, per version: `.text` of a function, else the first data section it has."""
    section = ".text" if m["kind"] == "function" else min((p["section"] for p in m["placements"]), default="")
    return {p["version"]: p for p in m["placements"] if p["section"] == section}


def atoms(doc: Json, segment: Segments = None, library: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The functions as atoms, and the data units' members: the member plus its placements in one section, segment,
    toolchain and protection. A data member without a unit stays with whatever holds it."""
    modules = {m["name"]: m for m in doc["modules"]}
    tool = {u["path"]: u["toolchain"] for u in doc["units"]}
    opts = {u["path"]: json.dumps(u.get("options"), sort_keys=True) for u in doc["units"]}
    native = {u["path"] for u in doc["units"] if u.get("kind", "c") == "c"}
    found: list[dict[str, Any]] = []
    for m in doc["members"]:
        t = _sections(m)
        unit = m["unit"] or ""
        in_library = bool(library) and unit.startswith(tuple(library))  # libgcc and friends are atoms too (gap 4)
        if t and (m["module"] in modules or in_library or (unit and _padding(m))) and (m["kind"] == "function" or unit):
            mod = modules.get(m["module"], {})
            held = (mod.get("evidence") == "authored" and segment is None) or unit.startswith(tuple(library))
            segment_name = mod["segment"] if mod else _within(doc["segments"], t.values())
            if not segment_name:
                continue
            found.append(
                {
                    **m,
                    "text": t,
                    "section": next(iter(t.values()))["section"],
                    "segment": segment_name,
                    "tool": tool.get(unit),
                    "opts": opts.get(unit),
                    "c": unit in native,
                    "protected": held,
                }
            )
    return found


def _within(segments: Sequence[Json], places: Collection[Json]) -> str:
    """The segment whose ROM range holds a placement: where a unit that no module holds belongs."""
    for s in segments:
        for p in places:
            low, high, _ = s["versions"].get(p["version"], (0, 0, 0))
            if low <= p["rom_start"] < high:
                return str(s["name"])
    return ""


def _padding(a: Json) -> bool:
    """A padding unit: data the linker left between two others, which belongs to the one before it."""
    return a["kind"] != "function" and "_padding_" in a["name"]


def _line(members: list[Json], doc: Json) -> list[Json]:
    """One segment section in names_from ROM order, atoms of other versions inserted after their predecessor."""
    first = doc["names_from"]
    line = [
        a for _, _, a in sorted((a["text"][first]["rom_start"], a["name"], a) for a in members if first in a["text"])
    ]
    index = {id(a): i for i, a in enumerate(line)}
    for v in (v for v in doc["versions"] if v != first):
        at = 0
        for _, _, a in sorted((a["text"][v]["rom_start"], a["name"], a) for a in members if v in a["text"]):
            if id(a) not in index:
                line.insert(at, a)
                index = {id(x): i for i, x in enumerate(line)}
            at = index[id(a)] + 1
    return line


def _apart(a: Json, b: Json, v: str, data: Json) -> bool:
    """Whether the data the two atoms load is not adjacent in version v: both load, and a gap lies between."""
    left = [data[n][v] for n in a["loads"] if v in data.get(n, {})]
    right = [data[n][v] for n in b["loads"] if v in data.get(n, {})]
    return bool(left and right and max(e for _, e in left) < min(s for s, _ in right))


_CUT = frozenset({"pad", "seg", "gap"})


def verdicts(doc: Json) -> dict[tuple[str, str], str] | None:
    """Per pair of functions adjacent in some version, the map's object-edge verdict (`unbake map` edge classes by the
    16-byte .text rule): the classes of every version where the left one directly precedes the right one, a cut class
    anywhere a cut, unal anywhere a join, both a contradiction, else open. None for a map without edge classes."""
    if not any("edge" in m for m in doc["members"]):
        return None
    rows: dict[str, list[tuple[int, str]]] = defaultdict(list)
    edge = {m["name"]: m.get("edge", {}) for m in doc["members"]}
    for m in doc["members"]:
        if m["kind"] == "function":
            for p in m["placements"]:
                if p["section"] == ".text":
                    rows[p["version"]].append((p["rom_start"], m["name"]))
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for v, held in rows.items():
        for (_, a), (_, b) in pairwise(sorted(held)):
            if v in edge[b]:
                seen[a, b].add(edge[b][v])
    out = {}
    for pair, kinds in seen.items():
        cut, join = bool(kinds & _CUT), "unal" in kinds
        out[pair] = "contradiction" if cut and join else "cut" if cut else "join" if join else "open"
    return out


def evidence_rows(root: Path) -> list[Json]:
    """The project's `[[evidence.object]]` rows (config.toml): an original object it proves, first..last function and
    the source path the evidence names (a debug record's __FILE__)."""
    import tomllib

    try:
        doc = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(doc.get("evidence", {}).get("object", ()))


def with_evidence(
    doc: Json, proven: dict[tuple[str, str], str] | None, rows: Sequence[Json]
) -> dict[tuple[str, str], str] | None:
    """The verdicts with each evidence row's object made hard: every pair inside it a join, its two edges cuts."""
    if not rows:
        return proven
    out = dict(proven or {})
    order: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for m in doc["members"]:
        if m["kind"] == "function":
            for p in m["placements"]:
                if p["section"] == ".text":
                    order[p["version"]].append((p["rom_start"], m["name"]))
    for held in order.values():
        names = [n for _, n in sorted(held)]
        at = {n: i for i, n in enumerate(names)}
        for row in rows:
            if row["first"] in at and row["last"] in at:
                low, high = at[row["first"]], at[row["last"]]
                out.update({(names[i], names[i + 1]): "join" for i in range(low, high)})
                if low:
                    out[names[low - 1], names[low]] = "cut"
                if high + 1 < len(names):
                    out[names[high], names[high + 1]] = "cut"
    return out


def evidenced_path(source: str, naming: tree.Naming = tree.PLAIN) -> tuple[str, str]:
    """(subsystem, source path) an evidence path names in the project's naming style: by default
    GAME/ACTORS/PLAYER.CPP is src/game/game/actors/player.c; a style with `evidence = "keep"` keeps its spelling."""
    return naming.evidenced(source)


def _near_callers(run: list[Json], reach: int = 3) -> set[int]:
    """Pair indexes a single near caller joins: a function only one atom of the run calls, at most `reach` atoms away,
    belongs to its caller's object with every atom between (measured 90% RW, 91% BT)."""
    pos = {a["name"]: i for i, a in enumerate(run)}
    callers: dict[int, set[int]] = defaultdict(set)
    for i, a in enumerate(run):
        for c in a["calls"]:
            if c in pos and pos[c] != i:
                callers[pos[c]].add(i)
    joined: set[int] = set()
    for callee, by in callers.items():
        if len(by) == 1 and abs(next(iter(by)) - callee) <= reach:
            low, high = sorted((next(iter(by)), callee))
            joined.update(range(low, high))
    return joined


def _partition(
    run: list[Json], data: Json, settings: state.Structure, proven: dict[tuple[str, str], str] | None = None
) -> tuple[list[list[Json]], list[Json]]:
    """Cut a run where two signals agree in every holder version, then long pieces at their weakest join. With the
    map's edge verdicts (`proven`), a function line cuts at every proven cut and never at a proven join; the signals
    decide only open pairs, a single near caller joins them, and no long piece is chained apart."""
    pos = {a["name"]: i for i, a in enumerate(run)}
    groups: dict[tuple[str, str], set[int]] = defaultdict(set)
    entry = [len(run)] * len(run)  # position of the first caller of each atom
    callers: set[int] = set()  # atoms with a call relation inside the run
    for i, a in enumerate(run):
        for c in (c for c in a["calls"] if c in pos):
            groups["calls", c] |= {i, pos[c]}
            entry[pos[c]] = min(entry[pos[c]], i)
            callers |= {i, pos[c]}
        for t in a["loads"]:
            groups["loads", t].add(i)
    spans = [(k, min(g), max(g)) for (k, _), g in groups.items() if len(g) > 1 and max(g) - min(g) < settings.reach]
    cover: dict[str, list[int]] = {"calls": [0] * len(run), "loads": [0] * len(run)}
    for kind, low, high in spans:
        cover[kind][low:high] = [c + 1 for c in cover[kind][low:high]]
    units = [(i, x["unit"]) for i, a in enumerate(run) for x in a.get("atoms", [a]) if x.get("c")]
    first, last = {u: i for i, u in reversed(units)}, {u: i for i, u in units}  # one compiled file is never cut (gap 8)
    locked = [any(first[u] <= i < last[u] for u in last) for i in range(len(run) - 1)]
    linked = [bool(a["loads"]) for a in run]
    called = [i in callers for i in range(len(run))]
    cut, why = [], []
    soft = _near_callers(run) if proven is not None else set()
    for i, (a, b) in enumerate(pairwise(run)):
        verdict = proven.get((a["name"], b["name"]), "open") if proven is not None else ""
        if verdict in ("cut", "join"):
            why.append([verdict])
            cut.append(verdict == "cut" and not locked[i])  # a C unit across a proven cut stays: B debt in unbake
            continue
        both = set(a["text"]) & set(b["text"])
        padded = a["padded"] and all(
            a["text"][v]["rom_end"] == b["text"][v]["rom_start"] and b["text"][v]["vram"] % 16 == 0 for v in both
        )
        # a relation that is there and does not join the two atoms is a signal; a relation that is absent is none
        on = {
            "calls": called[i] and called[i + 1] and not cover["calls"][i],
            "loads": linked[i] and linked[i + 1] and not cover["loads"][i],
            "reach": entry[i + 1] > i and entry[i + 1] < len(run),
        }
        if proven is None:  # without edge classes the old signals stand; with them both measured as no boundary
            on |= {"holders": set(a["text"]) != set(b["text"]), "padding": bool(both and padded)}
        on["data"] = bool(both and all(_apart(a, b, v, data) for v in both))
        why.append(sorted(k for k, v in on.items() if v))
        cut.append(len(why[-1]) >= 2 and not _padding(b) and not locked[i] and i not in soft)  # padding never leads
    weight = [x + y for x, y in zip(cover["calls"], cover["loads"], strict=True)]
    direct = [(low, high) for _, low, high in spans if not any(cut[low:high])]
    ends = [i for i in range(len(run)) if i == len(cut) or cut[i]]
    pending = list(zip([0, *(e + 1 for e in ends[:-1])], ends, strict=True))
    while pending and proven is None:  # proven cuts replace the cap chain: it made cuts at proven joins
        low, high = pending.pop()
        if high - low + 1 > settings.split_over and not any(a <= low and high <= b for a, b in direct):
            weakest = min(range(low, high), key=lambda i: (locked[i], weight[i], abs(2 * i + 1 - low - high), i))
            cut[weakest] = not locked[weakest]
            pending += [(low, weakest), (weakest + 1, high)]
    pieces: list[list[Json]] = [[run[0]]]
    for i, atom in enumerate(run[1:]):
        pieces += [[]] * cut[i]
        pieces[-1].append(atom)
    names = [
        (run[i]["name"], run[i + 1]["name"], why[i] + ["chain"] * (len(why[i]) < 2 and proven is None))
        for i in range(len(cut))
        if cut[i]
    ]
    return pieces, [{"left": left, "right": right, "signals": s} for left, right, s in names]


def _by_types(piece: list[Json], types: Callable[[str], clash.Types]) -> list[list[Json]]:
    """A module cut where the next whole C unit defines a type differently from the units before it: those units stay
    separate files (one subsystem, one header). A unit is never cut, so the parts keep ROM order."""
    chunks: list[list[Json]] = [[]]
    held: clash.Types = {}
    last = ""
    for a in piece:
        unit = a["unit"] if a["c"] and a["unit"] else ""
        if unit and unit != last:
            found = types(unit)
            if chunks[-1] and clash.conflicts(held, found):
                chunks.append([])
                held = {}
            held |= {k: v for k, v in found.items() if k not in held}
            last = unit
        chunks[-1].append(a)
    return chunks


def _near(native: list[int], at: int) -> int:
    """The nearest index in `native` to `at`, the earlier one on a tie."""
    return min(native, key=lambda n: (abs(n - at), n > at))


def _runs(line: list[Json]) -> list[tuple[bool, list[Json]]]:
    """Hard cuts: protected atoms and any change of (toolchain, compile options) or sdk end a run. An atom without a C
    unit takes the partition of the nearest C atom in the line, so asm joins its neighbours instead of forming runs.
    Each run comes with whether it is protected. A C unit's sdk is its first function's: one file is never cut."""
    native = [i for i, a in enumerate(line) if a["c"]]
    lead = {a["unit"]: a["sdk"] for a in reversed(line) if a["c"]}

    def key(i: int) -> tuple[Any, ...]:
        a = line[_near(native, i)] if native and not line[i]["c"] else line[i]
        return line[i]["protected"], a["tool"], a["opts"], lead.get(a["unit"], a["sdk"])

    return [(held, [line[i] for i in g]) for (held, *_), g in groupby(range(len(line)), key)]


def _anchor(run: list[Json]) -> list[Json]:
    """The run as one atom per C atom: asm atoms join the nearest C atom (the earlier one on a tie), so an asm-only
    stretch never becomes a module and the cuts are judged between C atoms. A run without C stays as it is."""
    native = [i for i, a in enumerate(run) if a["c"]]
    if not native:
        return run
    held: dict[int, list[Json]] = defaultdict(list)
    for i, a in enumerate(run):
        held[_near(native, i)].append(a)
    head = {a["name"]: held[n][0]["name"] for n, atoms in held.items() for a in atoms}
    out: list[Json] = []
    for atoms in held.values():
        text: dict[str, dict[str, Any]] = {}
        for a in atoms:
            for v, p in a["text"].items():
                joined = text.setdefault(v, dict(p))
                joined["vram"] = p["vram"] if p["rom_start"] < joined["rom_start"] else joined["vram"]
                joined["rom_start"] = min(joined["rom_start"], p["rom_start"])
                joined["rom_end"] = max(joined["rom_end"], p["rom_end"])
        calls = sorted({head.get(c, c) for a in atoms for c in a["calls"]})
        loads = sorted({n for a in atoms for n in a["loads"]})
        out.append(
            {
                "name": atoms[0]["name"],
                "kind": atoms[0]["kind"],
                "text": text,
                "calls": calls,
                "loads": loads,
                "padded": atoms[-1]["padded"],
                "atoms": atoms,
            }
        )
    return out


def _users(doc: Json, home: dict[str, str]) -> Callable[[str], frozenset[str]]:
    """For a shared header, the subsystems (headers of the modules) that include it. A consumer is a unit source, a
    module header or another shared header, transitively. The shared headers themselves are no subsystem."""
    by_path = {h["path"]: h for h in doc["headers"]}
    members: dict[str, list[str]] = defaultdict(list)
    for u in doc["units"]:
        members[u["path"]] += u["members"]
    for m in doc["modules"]:
        members[m["header"]] += m["members"]
    seen: dict[str, frozenset[str]] = {}
    base = set(tree.shared_headers())

    def users(path: str, trail: tuple[str, ...] = ()) -> frozenset[str]:
        if path not in seen:
            found: set[str] = set()
            for by in (x for x in by_path[path]["included_by"] if x not in (*trail, path)):
                if by in by_path and not by_path[by]["modules"]:
                    found |= users(by, (*trail, path))
                else:
                    found |= {home[m] for m in members[by] if m in home and home[m] not in base}
            seen[path] = frozenset(found)
        return seen[path]

    return users


def _cluster_header(users: Collection[str], base: str) -> str:
    """`common.h` in the lowest folder holding every using subsystem's header, "" at the include root or where it is the
    base declarations header (the base split of records and declarations applies there)."""
    folder = posixpath.commonpath([posixpath.dirname(u) for u in users])
    path = "" if folder in ("", "include") else f"{folder}/common.h"
    return "" if path == base else path


def headers(doc: Json, share: float, home: dict[str, str] | None = None) -> list[Json]:
    """`file` moves: a header with one using subsystem goes to its header. The others merge (same declarations or same
    users), then by co-usage: used by `shared_threshold` of the subsystems, the base headers (records to types.h,
    declarations to game/common.h), else `common.h` in the lowest folder holding the users, one per cluster."""
    home = home or {}
    dead = set(dead_headers(doc))
    types, common = tree.shared_headers()  # the base headers the others build on: they move into no other header
    shared = sorted(
        (h for h in doc["headers"] if not h["modules"] and h["path"] not in {*dead, types, common}),
        key=lambda h: (-len(h["included_by"]), h["path"]),
    )
    functions = {m["name"] for m in doc["members"] if m["kind"] == "function"}
    users = _users(doc, home)
    own = {h["path"]: next(iter(users(h["path"]))) for h in shared if len(users(h["path"])) == 1}
    shared = [h for h in shared if h["path"] not in own]

    sign = {h["path"]: (sorted(h["declares"]), sorted(h["includes"])) for h in shared}

    def like(a: Json, b: Json) -> bool:
        both = set(a["included_by"]) | set(b["included_by"])
        return bool(a["declares"] and sign[a["path"]] == sign[b["path"]]) or (
            bool(both) and len(set(a["included_by"]) & set(b["included_by"])) >= share * len(both)
        )

    target: dict[str, str] = {}
    for h in shared:
        target[h["path"]] = next((target[x["path"]] for x in shared if x["path"] in target and like(h, x)), h["path"])
    group: dict[str, set[str]] = defaultdict(set)  # what each merged group declares
    cluster: dict[str, set[str]] = defaultdict(set)  # the subsystems that use each merged group
    for h in shared:
        group[target[h["path"]]] |= set(h["declares"])
        cluster[target[h["path"]]] |= users(h["path"])
    subsystems = {u for u in home.values() if u not in (types, common)}
    need = float(table("knowledge/structure.toml")["shared_threshold"]) * len(subsystems)
    final: dict[str, str] = {}
    for t, used in cluster.items():
        local = _cluster_header(used, common) if used and len(used) < need else ""
        final[t] = local or (common if group[t] & functions else types)
    target = {p: final[t] for p, t in target.items()}
    target |= {h["path"]: types for h in shared if not h["included_by"]}  # no includer but a used declaration
    target |= own
    by_path, game = {h["path"]: h for h in doc["headers"]}, {h["path"] for h in doc["headers"] if h["modules"]}
    below: dict[str, bool] = {}  # a game header sits below this one: an SDK header never includes one, so it stays put

    def reaches(path: str) -> bool:
        if path not in below:
            below[path] = False  # an include cycle adds nothing
            below[path] = any(i in game or (i in by_path and reaches(i)) for i in by_path[path]["includes"])
        return below[path]

    target = {p: t for p, t in target.items() if not (t.startswith("include/sdk/") and reaches(p))}
    final = {p: _final(target, t) for p, t in target.items()}
    return [{"op": "file", "subject": p, "target": t} for p, t in sorted(final.items()) if p != t]


def _final(target: dict[str, str], at: str) -> str:
    """Where a header moved to ends up: a target that is itself moved is followed to the end, so no chain of `file`
    moves is proposed (a move may not read a header another move has just made a target). A cycle stops at its start."""
    seen = {at}
    while target.get(at, at) != at and target[at] not in seen:
        at = target[at]
        seen.add(at)
    return at


def _referenced(every: Sequence[Json], home: dict[str, str]) -> set[str]:
    """The headers some atom of another module calls or loads through: the ones a module header is needed for."""
    return {
        home[t]
        for a in every
        for t in (*a["calls"], *a["loads"])
        if t in home and a["name"] in home and home[t] != home[a["name"]]
    }


def propose(
    doc: Json,
    settings: state.Structure,
    segment: Segments = None,
    library: Sequence[str] = (),
    types: Callable[[str], clash.Types] | None = None,
    evidence: Sequence[Json] = (),
    naming: tree.Naming = tree.PLAIN,
) -> Json:
    """New modules with neutral names `code_<VRAM>` (`data_<VRAM>` for data), the boundaries behind them, header merges
    and size estimates. `segment` is one segment, a comma list or a sequence of them. The scope is the segments the
    modules sit in, whatever the map's own segment list holds. `types` gives a source unit's type definitions: units
    that define one type differently never share a module, they stay apart as files of one subsystem."""
    wanted = None if segment is None else frozenset(segment.split(",") if isinstance(segment, str) else segment)
    every = atoms(doc, wanted, library)
    scope = {a["segment"] for a in every if wanted is None or a["segment"] in wanted}
    existing = {m["name"]: m for m in doc["modules"]}
    data = {m["name"]: {p["version"]: (p["rom_start"], p["rom_end"]) for p in m["placements"]} for m in doc["members"]}
    held: dict[str, list[str]] = {n: [] for n in existing}
    by_segment: dict[str, dict[tuple[bool, str], list[Json]]] = defaultdict(lambda: defaultdict(list))
    for a in every:
        held.setdefault(a["module"], []).append(a["name"])
        by_segment[a["segment"]][a["kind"] != "function", a["section"]].append(a)
    modules: list[dict[str, Any]] = []
    boundaries: list[Json] = []
    taken: set[str] = set()
    seg = {a["name"]: a["segment"] for a in every}
    out = {s: {seg.get(c) for k in by_segment[s].values() for a in k for c in a["calls"]} - {s, None} for s in scope}
    proven = with_evidence(doc, verdicts(doc), evidence)
    order = sorted(scope, key=lambda s: (len(out[s]), s))  # leaf segments first
    for name in order:
        for kind in sorted(by_segment[name]):
            before = None  # the module made from the run just before, while nothing else sits between
            for fixed, run in _runs(_line(by_segment[name][kind], doc)):
                if fixed:
                    before = None
                    continue
                edges = proven if proven is not None and not kind[0] else None  # object edges are .text facts
                chain = run if edges is not None else _anchor(run)  # a proven edge may sit between asm atoms
                pieces, found = _partition(chain, data, settings, edges) if len(chain) > 1 else ([chain], [])
                pieces = [[a for x in p for a in x.get("atoms", [x])] for p in pieces]
                family: dict[int, list[Json]] = {}  # the parts of one piece that were cut apart by a type clash
                if types is not None:
                    parts = [_by_types(p, types) for p in pieces]
                    family = {id(c): [] for chunks in parts if len(chunks) > 1 for c in chunks}
                    clan = {id(c): chunks for chunks in parts if len(chunks) > 1 for c in chunks}
                    pieces = [c for chunks in parts for c in chunks]
                boundaries += found
                for piece in pieces:
                    if before is not None and all(_padding(a) for a in piece):  # padding folds into the unit before it
                        before["atoms"] += [a["name"] for a in piece]
                        continue
                    origin = max({a["module"] for a in piece}, key=[a["module"] for a in piece].count)
                    vram = piece[0]["text"].get(doc["names_from"]) or piece[0]["text"][min(piece[0]["text"])]
                    base = f"{'data' if kind[0] else 'code'}_{vram['vram']:08X}"
                    unique = tree.unique(base, taken | (set(existing) - {origin}), piece, doc["names_from"])
                    modules.append({"name": unique, "segment": name, "origin": origin})
                    taken.add(unique)
                    modules[-1]["atoms"] = [a["name"] for a in piece]
                    if id(piece) in family:  # every part of one piece carries the name of the first
                        kin = family[id(clan[id(piece)][0])]
                        modules[-1]["group"] = (kin or [modules[-1]])[0]["name"]
                        kin.append(modules[-1])
                    before = modules[-1]
    extra = tree.place(doc, every, modules, settings, naming)
    for row in evidence:  # the module an evidence row proves takes the folder and file name the evidence gives
        for m in modules:
            if row["first"] in m["atoms"] and row["last"] in m["atoms"]:
                sub, source = evidenced_path(row["source"], naming)
                m |= {"sub": sub, "folder": source.rpartition("/")[0], "source": source, "evidence": row["source"]}
                m["header"] = naming.header(sub) if sub else m["header"]
                m["batch"] = m["folder"]
                if m["batch"] not in extra["order"]:
                    extra["order"].insert(0, m["batch"])  # its own batch, before the folders that call into it
    is_c = {a["name"]: a["c"] for a in every}
    counted = [m for m in modules if any(is_c.get(n) for n in m["atoms"])]  # asm-only modules are no C units
    touched = {a["module"] for a in every if a["segment"] in scope and not a["protected"]}
    header = {m["name"]: m["header"] for m in doc["modules"]}  # every member's header: its module's now ...
    home = {m["name"]: header[m["module"]] for m in doc["members"] if m["module"] in header}
    home |= {n: m["header"] for m in modules for n in m["atoms"]}  # ... or its new module's
    dead = dead_headers(doc)
    kept, merges = len(doc["modules"]) - len(touched), headers(doc, settings.share_jaccard, home)
    rest = len([u for u in doc["units"] if u["module"] not in touched and u.get("kind", "c") == "c"])
    shared = len([h for h in doc["headers"] if not h["modules"]]) - len(merges) - len(dead)
    tracked = len([h for h in doc["headers"] if h["modules"] and not touched & set(h["modules"])])
    fresh = {m["header"] for m in modules if m["library"] or m["sub"]}
    needed = len(_referenced(every, home) & fresh)  # a new module needs a header only where another module reaches in
    pad = {m["name"] for m in doc["members"] if _padding(m)}
    folded = {a for m in modules if not set(m["atoms"]) <= pad for a in m["atoms"] if a in pad}  # rides in a module
    alone = [u for u in doc["units"] if u["members"] and set(u["members"]) <= pad]
    sizes = [len(held[n]) for n in touched if n in held]
    func = [m["name"] for m in modules if m["name"].startswith("func_")]
    padding = len([u for u in alone if not set(u["members"]) <= folded])
    where = tree.stats((m["folder"] for m in modules), (m["source"] for m in modules))
    current = {a["name"]: a["module"] for a in every} | {
        n: next(x["module"] for x in doc["members"] if x["name"] == n) for n in extra["fuzzy"]
    }
    return {
        "scope": order,
        "modules": modules,
        "boundaries": boundaries,
        "merges": merges,
        "dead_headers": dead,
        "symbols": extra["symbols"],
        "order": extra["order"],
        "current": current,
        "existing": {n: {"source": m["source"], "header": m["header"], "atoms": held[n]} for n, m in existing.items()},
        "tree": {
            "c_files": len([m for m in counted if not m["data"]]),
            "data_files": len([m for m in modules if m["data"]]),
            **where,
            "padding_files": padding,
            "func_files": len(func),
            "api_headers": needed,
            "subsystems": len({m["sub"] for m in modules if m["sub"]}),
            "libraries": len({m["library"] for m in modules if m["library"]}),
        },
        "gates": {
            "dir_max_ok": where["dir_max"] <= settings.dir_max,
            "padding_zero": padding == 0,
            "func_files_zero": not func,
        },
        "estimate": {  # (before, after)
            "modules": (len(doc["modules"]), kept + len(modules)),
            "units_estimate": (len([u for u in doc["units"] if u.get("kind", "c") == "c"]), rest + len(counted)),
            "headers": (len(doc["headers"]), shared + tracked + needed),
            "padding_units": (len(alone), padding),
            "single_atom_modules": (sizes.count(1), [len(m["atoms"]) for m in modules].count(1)),
        },
    }


def dead_headers(doc: Json) -> list[str]:
    """Shared headers no source includes and that declare nothing a member calls, loads or is: listed for deletion."""
    used = {m["name"] for m in doc["members"]} | {c for m in doc["members"] for c in (*m["calls"], *m["loads"])}
    return sorted(
        h["path"] for h in doc["headers"] if not h["modules"] and not h["included_by"] and not set(h["declares"]) & used
    )


def _module_moves(entry: Json, existing: Json, current: Json, renamed: Json) -> list[dict[str, Any]]:
    name, origin = entry["name"], entry["origin"]
    same = origin in existing and set(entry["atoms"]) == set(existing[origin]["atoms"])
    out = [{"op": "module", "subject": origin, "target": name}] * (same and origin != name)
    out += [
        {"op": "member", "subject": renamed.get(m, m), "target": name}
        for m in entry["atoms"]
        if current[m] != name and not same
    ]
    have = existing.get(origin if same else name, {})
    for op in ("source", "header"):
        out += [{"op": op, "subject": name, "target": entry[op]}] * (have.get(op) != entry[op])
    return out


def _batches(proposal: Json) -> list[tuple[str, list[dict[str, Any]]]]:
    """N0 symbol renames first, then libraries, subsystems leaf-first (fewest incoming calls) one folder per batch,
    shared and unloaded data last, then the shared header merges."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    renamed = proposal["symbols"]
    for entry in proposal["modules"]:
        by[entry["batch"]] += _module_moves(entry, proposal["existing"], proposal["current"], renamed)
    names = [{"op": "symbol", "subject": n, "target": t} for n, t in sorted(renamed.items())]
    head = [("symbols", names)] if names else []
    tail = [("headers", proposal["merges"])] if proposal["merges"] else []
    return head + [(s, by[s]) for s in proposal["order"] if by[s]] + tail


def _lines(items: Iterable[Json]) -> str:
    return "".join(json.dumps(i, sort_keys=True) + "\n" for i in items)


def run(config: Config, params: Json) -> Json:
    """`recipe structure`: write the proposal, `moves.jsonl` and a reset cursor under `.unbake/recipe/structure/`."""
    library = getattr(config.project, "library_dirs", ())
    clock: list[Json] = []
    document = evidence(config, params["host"], clock)
    types = clash.reader(config.project.root)
    rows = evidence_rows(config.project.root)
    naming = tree.style(config.project.root)
    proposal = dict(propose(document, state.structure(), params["segment"], library, types, rows, naming))
    batches, root = _batches(proposal), config.project.root / ".unbake/recipe/structure"
    stream = _lines(m for _, group in batches for m in group)
    proposal["runs"] = [{"segment": s, "count": len(g)} for s, g in batches]
    files = {"proposal.json": json.dumps(proposal), "moves.jsonl": stream, "cursor.json": "0", "refused.jsonl": ""}
    for name, text in files.items():
        store.write(root / name, text.encode())
    return {
        **{k: dict(zip(("before", "after"), v, strict=True)) for k, v in proposal["estimate"].items()},
        "tree": proposal["tree"],
        "gates": proposal["gates"],
        "dead_headers": proposal["dead_headers"],
        "boundaries": proposal["boundaries"],
        "moves": len(stream.splitlines()),
        "path": str((root / "moves.jsonl").relative_to(config.project.root)),
        "stream": stream,
        "unbake_calls": clock,
        "next": "recipe apply" if stream else "recipe status",
    }


_BACKOFF = (5, 15, 45)  # seconds before the 2nd, 3rd and 4th try of a batch the lock or the journal refused
_TRANSIENT = ("setup.busy", "journal.changed")


def _transient(document: Json) -> bool:
    found = [f["key"] for f in document["findings"]]
    found += [f["key"] for r in (document["result"] or {}).get("refused", []) for f in r["findings"]]
    return bool(found) and all(k in _TRANSIENT for k in found) and not (document["ok"])


def _dirty(root: Path) -> set[str]:
    """Paths `git status --porcelain` reports as changed or untracked (read only); none when root is no repository."""
    git = shutil.which("git")
    argv = [git or "git", "-C", str(root), "status", "--porcelain"]
    ran = process.run("git", argv, cwd=root, tmp=process.scratch(root)) if git else None
    lines = ran.stdout.decode(errors="replace").splitlines() if ran and ran.exit == 0 else []
    return {line[3:].split(" -> ")[-1].strip().strip('"') for line in lines}


def _files(move: Json, proposal: Json) -> set[str]:
    """The tracked files a move writes: its path subject or target, else the source of the module it names."""
    existing, current = proposal["existing"], proposal["current"]
    out = {x for x in (move["subject"], move["target"]) if "/" in x}
    named = {current.get(move["subject"], move["subject"]), move["target"]}
    return out | {existing[x]["source"] for x in named if x in existing}


_FOLLOWED = asdict(Finding("recipe.dependent", "an earlier refusal leaves this move nothing to act on", blocking=False))


def _key(move: Json) -> tuple[str, str, str]:
    return move["op"], move["subject"], move["target"]


def _dead(pending: list[Json], existing: Json, refused: list[Json]) -> list[Json]:
    """Moves that can only fail: the member moves of a symbol whose rename was refused, and the source and header of a
    new module that no member move is left for (every member move of it is dead or refused)."""
    names = {m["target"] for m in refused if m["op"] == "symbol"}
    gone = {_key(m) for m in refused}
    orphan = [m for m in pending if m["op"] == "member" and m["subject"] in names]
    left = {m["target"] for m in pending if m["op"] == "member" and m["subject"] not in names and _key(m) not in gone}
    wanted = {m["target"] for m in pending if m["op"] == "member"}
    return orphan + [
        m for m in pending if m["op"] in ("source", "header") and m["subject"] in wanted - left - set(existing)
    ]


def _follow(batch: list[Json], deferred: list[Json]) -> tuple[list[Json], list[Json]]:
    """A source or header move waits with the member moves of its module: it must not run before them."""
    late = {m["target"] for m in deferred if m["op"] == "member"}
    after = [m for m in batch if m["op"] in ("source", "header") and m["subject"] in late]
    return [m for m in batch if m not in after], [*deferred, *after]


def apply(config: Config, params: Json) -> Json:
    """`recipe apply`: the next batch, or with `--all` the whole rest of the stream as ONE `unbake move --partial -`.
    Each move is proved: unbake keeps a failing move's files as they were, applies the rest in that one pass and exits
    zero; every refused move is recorded with its finding in `refused.jsonl`: nothing is filtered silently."""
    return _batch(config, params, bool(params.get("all")))


def _batch(config: Config, params: Json, everything: bool) -> Json:
    """`recipe apply`: pipe the next batch (with `everything` the whole rest) to `unbake move --partial -`: unbake
    proves each move, drops the refused ones and applies the rest itself, so one call is the pass. A busy lock or a
    changed journal is retried after 5, 15 and 45 s. A move on a file with uncommitted changes waits."""
    root = config.project.root / ".unbake/recipe/structure"
    try:
        proposal = json.loads((root / "proposal.json").read_text(encoding="utf-8"))
        runs = proposal["runs"]
        pending = [json.loads(x) for x in (root / "moves.jsonl").read_text(encoding="utf-8").splitlines()]
        cursor = int(json.loads((root / "cursor.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise state.stop("recipe.structure", "no proposal is saved", action="recipe structure") from error
    ends = [sum(r["count"] for r in runs[: i + 1]) for i in range(len(runs))]
    at = next((i for i, end in enumerate(ends) if cursor < end), len(runs) - 1)
    stop = len(pending) if everything else min(ends[at], cursor + state.structure().batch_moves) if pending else 0
    dirty = _dirty(config.project.root) if pending and not everything else set()
    batch = [m for m in pending[cursor:stop] if not _files(m, proposal) & dirty]
    deferred = [m for m in pending[cursor:stop] if m not in batch]
    batch, deferred = _follow(batch, deferred)
    old = [json.loads(x)["move"] for x in (root / "refused.jsonl").read_text(encoding="utf-8").splitlines()]
    refused: list[Json] = []
    stderr = ""
    clock: list[Json] = []
    document: Json = {}
    while batch:
        dead = _dead(pending, proposal["existing"], [*old, *[r["move"] for r in refused]])
        refused += [{"move": m, "findings": [_FOLLOWED]} for m in batch if m in dead]
        batch = [m for m in batch if m not in dead]
        if not batch:
            break
        for attempt in range(len(_BACKOFF) + 1):
            flags = ("--partial",)  # per move proof: a failing move keeps its old state, the rest land
            document, stderr = _unbake(
                config, params["host"], "move", *flags, "-", stdin=_lines(batch).encode(), clock=clock
            )
            if attempt == len(_BACKOFF) or not _transient(document):
                break
            time.sleep(_BACKOFF[attempt])
        if document["result"] is None:
            raise _refusal(document)
        bad = {r["index"]: r for r in document["result"]["refused"]}
        refused += [{"move": batch[i], "findings": r["findings"]} for i, r in sorted(bad.items())]
        batch = [m for i, m in enumerate(batch) if i not in bad]
        break  # partial: unbake applied every move it did not refuse
    ledger = (root / "refused.jsonl").read_text(encoding="utf-8")
    store.write(root / "refused.jsonl", (ledger + _lines(refused)).encode())
    done_to = max(stop, cursor) - len(deferred)
    if deferred:  # they wait at the end of the stream, in the last batch
        kept = [m for m in pending[cursor:stop] if m not in deferred]
        pending = pending[:cursor] + kept + pending[stop:] + deferred
        runs[at]["count"] -= len(deferred)
        runs[-1]["count"] += len(deferred)
        store.write(root / "moves.jsonl", _lines(pending).encode())
        store.write(root / "proposal.json", json.dumps({**proposal, "runs": runs}).encode())
    store.write(root / "cursor.json", str(done_to).encode())
    done = document["result"] if batch else {}
    git = [x.strip() for x in stderr.splitlines() if x.strip().startswith("git ")] * bool(batch)
    return {
        "batch": "all" if everything else runs[at]["segment"] if runs else None,
        "moves": stop - cursor,
        "applied": len(batch),
        "deferred": [{"subject": m["subject"], "target": m["target"]} for m in deferred],
        "first_refused": refused[0]["move"]["subject"] if refused else None,
        "refused": [
            {"move": r["move"], "key": r["findings"][0]["key"], "reason": r["findings"][0]["reason"]} for r in refused
        ],
        **{k: done.get(k) for k in ("units", "headers", "files")},
        "message": done.get("message") or "no move was applied",
        "remaining": len(pending) - done_to,
        "unbake_calls": clock,
        "next": " && ".join([*git, "recipe apply" if done_to < len(pending) else "recipe structure"]),
    }
