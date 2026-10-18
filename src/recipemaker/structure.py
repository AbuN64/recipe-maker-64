"""Module structure as a file patch: evidence from `unbake map`, a proposal from it, one unified diff that moves
functions between files, renames symbols and files and edits includes. `recipe apply` applies it, then runs sync."""

from __future__ import annotations

import difflib
import json
import posixpath
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from itertools import groupby, pairwise
from pathlib import Path
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal

from recipemaker import clash, state, table, tree
from recipemaker import host as recipe_host

Segments = str | Collection[str] | None


def _refusal(document: Json) -> Refusal:
    keep = {"versions": (), "missing": ()}
    found = [Finding(**{**f, **{k: tuple(f[k]) for k in keep}}) for f in document["findings"]]
    return Refusal(*(found or [Finding("recipe.unbake", "unbake failed without a finding")]))


def _unbake(config: Config, host: Path | None, *args: str) -> tuple[Json, str]:
    """One unbake command: its JSON document and its stderr text."""
    exe, root = str(Path(sys.executable).with_name("unbake")), config.project.root
    argv = [exe, *(["--host", str(host)] if host else []), "--project", str(root), *args]
    ran = recipe_host.run(argv, root, config)
    lines = bytes(ran.stdout).decode(errors="replace").strip().splitlines()
    try:
        return json.loads(lines[-1]), bytes(ran.stderr).decode(errors="replace")
    except (ValueError, IndexError) as error:
        raise Refusal(Finding("recipe.unbake", f"unbake {args[0]} printed no JSON")) from error


def evidence(config: Config, host: Path | None = None) -> Json:
    """Read the public map evidence."""
    document, _ = _unbake(config, host, "map")
    if not document["ok"]:
        raise _refusal(document)
    return dict(json.loads((config.project.root / document["result"]["path"]).read_text(encoding="utf-8")))


def _sections(m: Json) -> dict[str, dict[str, Any]]:
    """The placements an atom is ordered by, per version: `.text` of a function, else the first data section it has."""
    section = ".text" if m["kind"] == "function" else min((p["section"] for p in m["placements"]), default="")
    return {p["version"]: p for p in m["placements"] if p["section"] == section}


def atoms(doc: Json, segment: Segments = None, library: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Read function and data atoms with placement, toolchain, options and library protection from the map."""
    modules = {m["name"]: m for m in doc["modules"]}
    tool = {u["path"]: u["toolchain"] for u in doc["units"]}
    opts = {u["path"]: json.dumps(u.get("options"), sort_keys=True) for u in doc["units"]}
    native = {u["path"] for u in doc["units"] if u.get("kind", "c") == "c"}
    found: list[dict[str, Any]] = []
    for m in doc["members"]:
        t = _sections(m)
        unit = m["unit"] or ""
        in_library = any(unit.startswith(p.rstrip("/") + "/") for p in library)  # directory membership
        if t and (m["module"] in modules or in_library or (unit and _padding(m))) and (m["kind"] == "function" or unit):
            mod = modules.get(m["module"], {})
            held = (mod.get("evidence") == "authored" and segment is None) or in_library
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
    """Partition at proven cuts/joins when available; otherwise use shared signals and bounded chains."""
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
    """Split pieces at incompatible type definitions between whole C units, retaining ROM order."""
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
    """Group runs by source protection, toolchain, options and SDK; asm inherits its nearest C partition."""
    native = [i for i, a in enumerate(line) if a["c"]]
    lead = {a["unit"]: a["sdk"] for a in reversed(line) if a["c"]}

    def key(i: int) -> tuple[Any, ...]:
        a = line[_near(native, i)] if native and not line[i]["c"] else line[i]
        return line[i]["protected"], a["tool"], a["opts"], lead.get(a["unit"], a["sdk"])

    return [(held, [line[i] for i in g]) for (held, *_), g in groupby(range(len(line)), key)]


def _anchor(run: list[Json]) -> list[Json]:
    """Anchor asm to its nearest C atom (earlier on ties); leave wholly asm runs unchanged."""
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


def _users(doc: Json, home: dict[str, str], naming: tree.Naming = tree.PLAIN) -> Callable[[str], frozenset[str]]:
    """Find each shared header's consuming subsystem headers through sources and transitive header includes."""
    by_path = {h["path"]: h for h in doc["headers"]}
    members: dict[str, list[str]] = defaultdict(list)
    for u in doc["units"]:
        members[u["path"]] += u["members"]
    for m in doc["modules"]:
        members[m["header"]] += m["members"]
    seen: dict[str, frozenset[str]] = {}
    base = set(naming.shared_headers)

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
    """The configured common basename below the users' common folder, or empty when a base header serves them."""
    folder = posixpath.commonpath([posixpath.dirname(u) for u in users])
    path = "" if folder in ("", "include") else f"{folder}/{posixpath.basename(base)}"
    return "" if path == base else path


def headers(
    doc: Json, share: float, home: dict[str, str] | None = None, naming: tree.Naming = tree.PLAIN
) -> list[Json]:
    """Merge shared headers by declarations and consuming subsystems; preserve the configured base header roles."""
    home = home or {}
    dead = set(dead_headers(doc))
    types, common = naming.shared_headers  # the base headers the others build on: they move into no other header
    base_moves = {
        old: new
        for old, new in zip(tree.shared_headers(), naming.shared_headers, strict=True)
        if old != new and any(h["path"] == old for h in doc["headers"])
    }
    shared = sorted(
        (h for h in doc["headers"] if not h["modules"] and h["path"] not in {*dead, types, common, *base_moves}),
        key=lambda h: (-len(h["included_by"]), h["path"]),
    )
    functions = {m["name"] for m in doc["members"] if m["kind"] == "function"}
    users = _users(doc, home, naming)
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
    target |= base_moves  # preserve the tool defaults' types/declarations roles when the project relocates them
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
    """Propose modules, boundaries, paths and header merges from the map's evidence.
    Scope is all segments or the requested segment names; `types` detects source definition clashes."""
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
                    origin = max(sorted({a["module"] for a in piece}), key=[a["module"] for a in piece].count)
                    vram = piece[0]["text"].get(doc["names_from"]) or piece[0]["text"][min(piece[0]["text"])]
                    base = f"{'data' if kind[0] else 'code'}_{vram['vram']:08X}"
                    unique, address = tree.identity(base, taken | (set(existing) - {origin}), piece, doc["names_from"])
                    modules.append({"name": unique, "segment": name, "origin": origin, "address": address})
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
    kept, merges = len(doc["modules"]) - len(touched), headers(doc, settings.share_jaccard, home, naming)
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


# ------------------------------------------------------------------------------------------------ the patch

_TEXT = (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".s", ".inc")
_STAND_IN = r"^[ \t]*INCLUDE_(?:ASM|DATA|RODATA|BSS)\([ \t]*[^,()]+,[ \t]*{name}\b[^\n]*\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")


def _blocks(lines: list[str]) -> list[tuple[int, int, str]]:
    """Every `#if` block as (first line, last line, condition), from its `#if` to its `#endif`."""
    stack: list[tuple[int, str]] = []
    out: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#"):
            continue
        word = line.lstrip().removeprefix("#").lstrip()
        if word.startswith("if"):
            stack.append((i, word))
        elif word.startswith("endif") and stack:
            start, cond = stack.pop()
            out.append((start, i, cond))
    return out


def _brace_end(text: str, at: int) -> int:
    """The index after the `}` that closes the `{` at `at`, skipping strings, characters and comments."""
    depth, i = 0, at
    while i < len(text):
        two, one = text[i : i + 2], text[i]
        if two == "/*":
            i = text.find("*/", i + 2) + 1 or len(text)
        elif two == "//":
            i = text.find("\n", i) if "\n" in text[i:] else len(text)
        elif one in "\"'":
            i += 1
            while i < len(text) and text[i] != one:
                i += 1 + (text[i] == "\\")
        elif one == "{":
            depth += 1
        elif one == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def cut(text: str, name: str) -> tuple[str, str] | None:
    """(the text without the unit of `name`, the unit): its INCLUDE_* line or its C definition, or the whole
    `#ifdef NON_MATCHING` block that holds either. None when the text has neither."""
    found = re.search(_STAND_IN.format(name=re.escape(name)), text, re.M)
    if found:
        lo, hi = found.start(), found.end()
    else:
        head = re.search(rf"^[ \t]*(?:static[ \t]+)?[\w \t*]*?\b{re.escape(name)}[ \t]*\([^;{{]*\)\s*\{{", text, re.M)
        if not head:
            return None
        lo, hi = head.start(), _brace_end(text, head.end() - 1)
        hi += text[hi : hi + 1] == "\n"
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    first = next(i for i in range(len(lines)) if starts[i + 1] > lo)
    held = [b for b in _blocks([x.rstrip("\n") for x in lines]) if b[0] <= first <= b[1] and "NON_MATCHING" in b[2]]
    if held:
        start, end, _ = min(held, key=lambda b: b[1] - b[0])
        lo, hi = starts[start], starts[end + 1]
    return text[:lo] + text[hi:], text[lo:hi]


def _preamble(text: str) -> str:
    """The `#include` lines a file opens with: a new file that takes a unit from it starts from the same ones."""
    return "".join(x for x in text.splitlines(keepends=True) if x.lstrip().startswith("#include"))


class Files:
    """The project's source files as text, edited in memory; `diff()` is the git diff of every change."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.text: dict[str, str | None] = {}
        self.orig: dict[str, str | None] = {}
        self.origin: dict[str, str] = {}

    def get(self, path: str) -> str | None:
        if path not in self.text:
            file = self.root / path
            self.text[path] = self.orig[path] = file.read_text(encoding="utf-8") if file.is_file() else None
        return self.text[path]

    def put(self, path: str, text: str) -> None:
        self.get(path)
        self.text[path] = text

    def rename(self, old: str, new: str) -> None:
        text = self.get(old)
        if text is None or self.get(new) is not None:
            return
        self.text[new], self.text[old] = text, None
        self.origin[new] = self.origin.get(old, old)

    def everything(self) -> list[str]:
        """The source and header files under src/ and include/, by their current names."""
        found = {
            p.relative_to(self.root).as_posix()
            for d in ("src", "include")
            for p in (self.root / d).rglob("*")
            if p.is_file() and p.suffix.lower() in _TEXT
        }
        gone = {p for p, t in self.text.items() if t is None}
        return sorted((found | {p for p, t in self.text.items() if t is not None}) - gone)

    def replace(self, pattern: re.Pattern[str], new: str | Callable[[re.Match[str]], str], paths: Iterable[str]) -> int:
        count = 0
        for path in paths:
            text = self.get(path)
            if text is not None:
                changed, n = pattern.subn(new, text)
                if n:
                    self.put(path, changed)
                    count += n
        return count

    def changed(self) -> list[str]:
        return sorted(p for p in self.text if self.text[p] != self.orig[p] or p in self.origin)

    def diff(self) -> str:
        moved = set(self.origin.values())
        out: list[str] = []
        for path in self.changed():
            new, was = self.text[path], self.origin.get(path, path)
            if new is None and path in moved:
                continue
            out.append(_file_diff(path, was, self.orig[was], new))
        return "".join(out)


def _file_diff(path: str, was: str, old: str | None, new: str | None) -> str:
    head = f"diff --git a/{was} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
    elif new is None:
        head += "deleted file mode 100644\n"
    elif was != path:
        head += f"similarity index {int(difflib.SequenceMatcher(None, old, new, autojunk=False).ratio() * 100)}%\n"
        head += f"rename from {was}\nrename to {path}\n"
    before, after = (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True)
    if before == after:
        return head
    names = ("/dev/null" if old is None else f"a/{was}", "/dev/null" if new is None else f"b/{path}")
    body = [
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(before, after, *names, lineterm="\n")
    ]
    return head + "".join(body)


def build(root: Path, moves: Sequence[Json], owner: Callable[[str], str | None], modules: Mapping[str, Json]) -> Json:
    """A unified diff of the moves, as `{"patch", "files", "moves"}`. `owner` gives the file now holding a function
    (the tree's), `modules` the source and header path of each module the moves name.

    - member: the function's C body or INCLUDE_* line leaves its owner file for the module's file, made if absent;
    - symbol: its `versions/*/symbol_addrs.txt` lines and, word-bounded, every identifier in src/ and include/;
    - type: the identifier only;
    - module, source, header: the file renames, with the includes that name the old header;
    - file: the includes of a header move to the header it goes to."""
    files, applied = Files(root), 0
    where: dict[str, str] = {}
    path = {n: m["source"] for n, m in modules.items()}
    header = {n: m["header"] for n, m in modules.items()}
    final = {m["subject"]: m["target"] for m in moves if m["op"] == "source"}

    def at(name: str) -> str | None:
        if name not in where and (found := owner(name)):
            where[name] = found
        return where.get(name)

    def include(old: str, new: str) -> int:
        pattern = re.compile(
            rf'(#[ \t]*include[ \t]*[<"])({re.escape(old)}|{re.escape(old.removeprefix("include/"))})([>"])'
        )

        def spell(m: re.Match[str]) -> str:
            return f"{m[1]}{new if m[2] == old else new.removeprefix('include/')}{m[3]}"

        return files.replace(pattern, spell, files.everything())

    for move in moves:
        op, subject, target = move["op"], move["subject"], move["target"]
        if op in ("symbol", "type"):
            hits = 0
            if op == "symbol":
                found = sorted((root / "versions").glob("*/symbol_addrs.txt"))
                line = re.compile(rf"(?m)^([ \t]*){re.escape(subject)}(?![\w])")
                hits += files.replace(line, r"\g<1>" + target, [p.relative_to(root).as_posix() for p in found])
            hits += files.replace(_word(subject), target, files.everything())
            applied += bool(hits)
            if subject in where:
                where[target] = where.pop(subject)
        elif op == "module":
            for names in (path, header):
                if subject in names:
                    names[target] = names.pop(subject)
        elif op == "member":
            src, dest = at(subject), path.get(target) or final.get(target)
            text = files.get(src) if src else None
            taken = cut(text, subject) if text is not None and src and dest and src != dest else None
            if taken is None or not src or not dest:
                continue
            files.put(src, taken[0])
            have = files.get(dest)
            files.put(dest, (_preamble(text or "") + "\n" if have is None else have + "\n") + taken[1])
            path[target], where[subject] = dest, dest
            applied += 1
        elif op in ("source", "header"):
            names = path if op == "source" else header
            old = names.get(subject)
            names[subject] = target
            if old and old != target and files.get(old) is not None:
                files.rename(old, target)
                if op == "header":
                    include(old, target)
                else:
                    where.update({n: target for n, w in list(where.items()) if w == old})
                applied += 1
        elif op == "file":
            applied += bool(include(subject, target))
            if files.get(subject) is not None and files.get(target) is None:
                files.rename(subject, target)
    return {"patch": files.diff(), "files": files.changed(), "moves": applied}


def ops(proposal: Json) -> list[Json]:
    """The flat move list of a proposal: renames of symbols first, then one folder after the other, then headers."""
    return [m for _, group in _batches(proposal) for m in group]


def modules_of(doc: Json) -> dict[str, Json]:
    return {m["name"]: {"source": m["source"], "header": m["header"]} for m in doc["modules"]}


def save(config: Config, built: Json) -> str:
    """The patch as the next `patch-N.diff` under `.unbake/recipe/patches/`; its path relative to the project."""
    root = config.project.root / ".unbake/recipe/patches"
    found = [int(m[1]) for p in root.glob("patch-*.*") if (m := re.match(r"patch-(\d+)\.", p.name))]
    path = root / f"patch-{max(found, default=0) + 1}.diff"
    write(path, built["patch"])
    return str(path.relative_to(config.project.root))


def stream(built: Json) -> str:
    """What the verb prints on stdout: the patch, the files it touches and the count of moves."""
    return json.dumps({k: built[k] for k in ("patch", "files", "moves")}) + "\n"


def run(config: Config, params: Json) -> Json:
    """`recipe structure`: write the proposal as a patch under `.unbake/recipe/patches/` and print it as JSON."""
    root = config.project.root
    document = evidence(config, params["host"])
    proposal = dict(
        propose(
            document,
            state.structure(),
            params["segment"],
            getattr(config, "libraries", ()),
            clash.reader(root),
            (),
            tree.style(root),
        )
    )
    captured = api.capture(config)
    built = build(root, ops(proposal), lambda n: captured.owner(document["names_from"], n), modules_of(document))
    return {
        **{k: dict(zip(("before", "after"), v, strict=True)) for k, v in proposal["estimate"].items()},
        "tree": proposal["tree"],
        "gates": proposal["gates"],
        "dead_headers": proposal["dead_headers"],
        "boundaries": proposal["boundaries"],
        **{k: built[k] for k in ("patch", "files", "moves")},
        "path": save(config, built),
        "stream": stream(built),
        "next": "recipe apply" if built["moves"] else "recipe status",
    }


def apply(config: Config, params: Json) -> Json:
    """`recipe apply`: `git apply` the oldest saved patch (or PATCH), then `unbake sync`; sync's result comes back.
    The one explicit operator action that writes."""
    root = config.project.root
    saved = sorted(
        (root / ".unbake/recipe/patches").glob("patch-*.diff"),
        key=lambda p: int("".join(c for c in p.name if c.isdigit())),
    )
    patch = Path(params["patch"]) if params.get("patch") else saved[0] if saved else None
    if patch is None or not patch.is_file():
        raise state.stop("recipe.apply", "no patch is saved", action="recipe structure")
    ran = recipe_host.run(["git", "-C", str(root), "apply", str(patch.resolve())], root, config)
    if ran.exit:
        tail = bytes(ran.stderr).decode(errors="replace").strip().splitlines()[-1:] or ["git apply failed"]
        raise state.stop("recipe.apply", f"git apply refused {patch.name}: {tail[0]}", action="recipe structure")
    patch.rename(patch.with_suffix(".applied"))
    document, _ = _unbake(config, params["host"], "sync")
    if not document["ok"]:
        raise _refusal(document)
    left = len([p for p in saved if p != patch])
    return {
        **(document["result"] or {}),
        "patch": patch.name,
        "left": left,
        "next": "recipe apply" if left else "recipe structure",
    }
