"""The folder tree over proposed modules: segments, libraries, call-graph subsystems and data ownership.

Everything here is a pure function of the map document's facts. Folders are labels: a community may interleave with
others in ROM order, and the layout keeps the link order. Rules S, L, Y and D are those of the structure design."""

from __future__ import annotations

import statistics
import tomllib
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from unbake import config as configuration
from unbake import effort
from unbake.contracts import Json

from recipemaker import state, table

Mod = dict[str, Any]
Pair = tuple[str, str]
Hit = tuple[str, str]  # (library, module) of a catalog hit


def shared_headers() -> tuple[str, str]:
    """The types header and the declarations header: `repo.toml` `[layout] shared_headers`."""
    rows = effort.memo(
        ("recipe.shared_headers",), lambda: tuple(configuration.load_resource("repo.toml")["layout"]["shared_headers"])
    )
    return rows[0], rows[1]


@dataclass(frozen=True)
class Naming:
    """The project's naming style for the paths recipe assigns (config.toml `[naming]`, the folder templates from its
    `[layout]` over unbake's repo.toml). The defaults are the tool's own lower-case scheme. Templates take the fields
    {name} (module), {vram} (8 hex digits), {label} (segment label), {segment}, {subsystem} and, for `subsystem`,
    {vram} of the subsystem. `case` ("as-is", "upper", "lower") applies to every field value, never to a template's
    own text; `stem_max` (0 = none) bounds a file stem; `evidence` ("keep", "lower", "upper") is how an evidence
    row's path is spelled ("keep": as the evidence gives it, its own suffix included). Library code keeps its names."""

    case: str = "as-is"
    stem_max: int = 0
    source_ext: str = ".c"
    stem: str = "{name}"
    subsystem: str = "{label}/sys_{vram}"
    shared_dir: str = "src/game/{label}/shared"
    data_dir: str = "src/data/{segment}"
    part: str = "part_{vram}"
    evidence: str = "lower"
    subsystem_source: str = "src/game/{subsystem}"
    subsystem_header: str = "include/game/{subsystem}.h"

    def cased(self, text: str, case: str | None = None) -> str:
        how = case or self.case
        return text.upper() if how == "upper" else text.lower() if how == "lower" else text

    def fill(self, template: str, **values: str) -> str:
        """`template` with every field value cased (the template's own text stays as written)."""
        return template.format(**{k: self.cased(v) for k, v in values.items()})

    def file(self, folder: str, module: Mod) -> str:
        """`folder`/<stem><source_ext> of an assigned game or data module."""
        stem = self.fill(self.stem, name=module["name"], vram=f"{module['vram']:08X}", label=module.get("label", ""))
        if self.stem_max and len(stem) > self.stem_max:
            raise ValueError(f"recipe naming: stem {stem!r} of {module['name']} is over stem_max {self.stem_max}")
        return f"{folder}/{stem}{self.source_ext}"

    def evidenced(self, source: str) -> tuple[str, str]:
        """(subsystem, source path) an evidence path names, spelled the way `evidence` says."""
        parts = source.replace("\\", "/").strip("/").split("/")
        if self.evidence != "keep":
            parts = [self.cased(p, self.evidence) for p in parts]
            parts[-1] = parts[-1].rsplit(".", 1)[0] + self.source_ext
        sub = "/".join(parts[:-1])
        where = self.subsystem_source.format(subsystem=sub) if sub else self.subsystem_source.partition("/{")[0]
        return sub, f"{where}/{parts[-1]}"

    def header(self, sub: str) -> str:
        return self.subsystem_header.format(subsystem=sub)


PLAIN = Naming()  # the tool's own scheme, for projects without a [naming] table


def style(root: Path | None) -> Naming:
    """The project's Naming: config.toml `[naming]` plus its `[layout]` subsystem templates; defaults when absent."""
    try:
        doc = tomllib.loads((root / "config.toml").read_text(encoding="utf-8")) if root else {}
    except (OSError, ValueError):
        doc = {}
    known = {f.name for f in fields(Naming)}
    layout = {k: v for k, v in doc.get("layout", {}).items() if k in ("subsystem_source", "subsystem_header")}
    unknown = set(doc.get("naming", {})) - known
    if unknown:
        raise ValueError(f"recipe naming: unknown [naming] keys {sorted(unknown)}")
    return Naming(**{**layout, **doc.get("naming", {})})


def labels(doc: Json) -> dict[str, str]:
    """Rule S: the label of every map segment. Segments that start at one VRAM are one (the same overlay at other ROM
    offsets in other versions). The one holding the lowest function VRAM is `main`, the others `ovl_<VRAM>`, and a game
    with a single code segment has no level (empty label). A segment the map does not list is labelled by its own
    name, and it is another level."""
    start = {s["name"]: min((v[2] for v in s["versions"].values()), default=0) for s in doc["segments"]}
    code = {m["name"] for m in doc["members"] if m["kind"] == "function"}
    held = {m["segment"] for m in doc["modules"] if code & set(m["members"])}
    starts = sorted({start[s] for s in held if s in start} or set(start.values()))
    if len(starts) + len(held - set(start)) <= 1:
        return dict.fromkeys(start, "")
    names = {v: ("main" if i == 0 else f"ovl_{v:08X}") for i, v in enumerate(starts)}
    return {s: names.get(v, f"ovl_{v:08X}") for s, v in start.items()}


def segments(doc: Json) -> dict[str, str]:
    """Module -> segment label (rule S)."""
    label = labels(doc)
    return {m["name"]: label.get(m["segment"], m["segment"]) for m in doc["modules"]}


def vram(atom: Json, first: str) -> int:
    """The VRAM of an atom: its placement in the version that names things, else the lowest version's."""
    text = atom["text"]
    return int((text.get(first) or text[min(text)])["vram"])


def _suffix(atoms: Sequence[Json], first: str) -> str:
    """The collision suffix of a module: the VRAM of its second atom, never a counter (one atom: its ROM start)."""
    if len(atoms) > 1:
        return f"{vram(atoms[1], first):08X}"
    text = atoms[0]["text"]
    return f"{int((text.get(first) or text[min(text)])['rom_start']):X}"


def unique(base: str, taken: Collection[str], atoms: Sequence[Json], first: str) -> str:
    """`base`, or `base_<VRAM of the second atom>` when it is taken, then more atom VRAMs while it still is."""
    name, more = base, iter([_suffix(atoms, first), *(f"{vram(a, first):08X}" for a in atoms)])
    for extra in more:
        if name not in taken:
            return name
        name = f"{base}_{extra}"
    return name


def libraries(modules: Sequence[Mod], calls: dict[Pair, int], settings: state.Structure) -> dict[str, str]:
    """Rule L: leaf-closed regions. A region is a ROM run (in one segment) of modules whose outgoing calls are at most
    `library_leak` of its calls, that is called from outside at least `library_fan_in` times as often as it calls out,
    has at least `library_min_modules` modules and fewer than half the segment. Largest first, then lowest VRAM, but a
    region holding a region with four times its fan-in ratio is a layer of the program, not a library, and is skipped.
    Modules with a preset `library` (catalog hits) are fixed; a region around them is named after them. Returns the
    library of each module that has one: `lib_<VRAM of the first module>` unless a catalog name rules."""
    out: dict[str, str] = {m["name"]: m["library"] for m in modules if m.get("library")}
    by_segment: dict[str, list[Mod]] = defaultdict(list)
    for m in modules:
        by_segment[m["label"]].append(m)
    for line in by_segment.values():
        line.sort(key=lambda m: (m["vram"], m["name"]))
        pos = {m["name"]: i for i, m in enumerate(line)}
        callees: dict[int, list[tuple[int, int]]] = defaultdict(list)
        callers: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for (src, dst), w in calls.items():
            if src in pos:
                callees[pos[src]].append((pos.get(dst, -1), w))
            if dst in pos:
                callers[pos[dst]].append((pos.get(src, -1), w))
        found: dict[tuple[int, int], float] = {}
        for i in range(len(line)):
            inside, leak, entry = 0, 0, 0  # calls within, out of and into the run [i, j]
            for j in range(i, len(line)):
                for at, w in callers[j]:
                    if i <= at < j:
                        leak, inside = leak - w, inside + w
                    elif at < i or at > j:
                        entry += w
                for at, w in callees[j]:
                    if at == j:
                        inside += w
                    elif i <= at < j:
                        entry, inside = entry - w, inside + w
                    else:
                        leak += w
                size, total = j - i + 1, inside + leak
                ok = size >= settings.library_min_modules and 2 * size < len(line) and total > 0
                if (
                    ok
                    and leak <= settings.library_leak * total
                    and entry > 0
                    and entry >= settings.library_fan_in * leak
                ):
                    found[i, j] = entry / max(leak, 1)
        best: dict[tuple[int, int], float] = {}  # the best fan-in ratio of any region inside [i, j]
        for size in range(1, len(line) + 1):
            for i in range(len(line) - size + 1):
                j = i + size - 1
                best[i, j] = max(found.get((i, j), 0.0), best.get((i + 1, j), 0.0), best.get((i, j - 1), 0.0))
        # a region that holds a much tighter one is a layer of the program around a library, not the library
        limit = float(table("knowledge/structure.toml")["dominance"])  # recipe knowledge knob
        tight = [
            k
            for k, r in found.items()
            if max(best.get((k[0] + 1, k[1]), 0.0), best.get((k[0], k[1] - 1), 0.0)) < limit * r
        ]
        taken: set[int] = set()
        for i, j in sorted(tight, key=lambda k: (k[0] - k[1], line[k[0]]["vram"])):
            if taken & set(range(i, j + 1)):
                continue
            taken |= set(range(i, j + 1))
            named = Counter(out[m["name"]] for m in line[i : j + 1] if m["name"] in out)
            name = max(sorted(named), key=named.get) if named else f"lib_{line[i]['vram']:08X}"  # type: ignore[arg-type]
            out |= {m["name"]: out.get(m["name"], name) for m in line[i : j + 1]}
    return out


def _strongest(group: Collection[str], other: Collection[str], w: dict[Pair, float]) -> float:
    return sum(w.get((a, b), 0.0) for a in group for b in other)


def _cnm(nodes: Sequence[str], w: dict[Pair, float], first: dict[str, int]) -> list[list[str]]:
    """Greedy modularity agglomeration (Clauset-Newman-Moore): merge the adjacent pair with the best gain until none
    gains. Ties go to the lowest first VRAM, so equal input gives equal output."""
    m = sum(w.values())
    if m <= 0:
        return [[n] for n in nodes]
    adj: dict[str, dict[str, float]] = {n: {} for n in nodes}
    for (a, b), x in w.items():
        adj[a][b] = adj[a].get(b, 0.0) + x
        adj[b][a] = adj[b].get(a, 0.0) + x
    strength = {n: sum(adj[n].values()) for n in nodes}
    members = {n: [n] for n in nodes}
    low = dict(first)
    while True:
        best: tuple[Any, ...] | None = None
        for a, row in adj.items():
            for b, x in row.items():
                if a < b:
                    gain = x / m - strength[a] * strength[b] / (2 * m * m)
                    key = (-round(gain, 12), min(low[a], low[b]), max(low[a], low[b]), a, b)
                    best = key if best is None or key < best else best
        if best is None or best[0] >= 0:
            break
        a, b = best[3], best[4]
        for c, x in adj.pop(b).items():
            if c != a:
                adj[a][c] = adj[a].get(c, 0.0) + x
                adj[c][a] = adj[c].get(a, 0.0) + x
            adj[c].pop(b, None)
        adj[a].pop(b, None)
        strength[a] += strength[b]
        members[a] += members.pop(b)
        low[a] = min(low[a], low[b])
    return [sorted(g, key=lambda n: (first[n], n)) for g in members.values()]


def _bisect(
    group: list[str], w: dict[Pair, float], files: dict[str, int], first: dict[str, int], lo: int
) -> tuple[list[str], list[str]]:
    """Cut a community in two at its weakest internal cut: nodes in maximum-adjacency order, the prefix with the least
    cut per pair of files (balanced on a tie) whose both sides keep at least `lo` files."""
    order, left = [group[0]], set(group[1:])
    link = {n: w.get((order[0], n), 0.0) + w.get((n, order[0]), 0.0) for n in left}
    while left:
        nxt = min(left, key=lambda n: (-link[n], first[n], n))
        left.discard(nxt)
        order.append(nxt)
        for n in left:
            link[n] += w.get((nxt, n), 0.0) + w.get((n, nxt), 0.0)
    total, whole = (
        sum(files[n] for n in group),
        {n: sum(w.get((n, o), 0.0) + w.get((o, n), 0.0) for o in group) for n in group},
    )
    best: tuple[tuple[float, int, int], int] | None = None
    cut, size, seen = 0.0, 0, set[str]()
    for i, n in enumerate(order[:-1]):
        to_seen = sum(w.get((n, o), 0.0) + w.get((o, n), 0.0) for o in seen)
        cut, size = cut + whole[n] - 2 * to_seen, size + files[n]
        seen.add(n)
        if size >= lo and total - size >= lo:
            key = (cut / (size * (total - size)), abs(total - 2 * size), i)
            best = (key, i) if best is None or key < best[0] else best
    at = best[1] if best else len(order) // 2 - 1
    return sorted(order[: at + 1], key=lambda n: (first[n], n)), sorted(order[at + 1 :], key=lambda n: (first[n], n))


def subsystems(modules: Sequence[Mod], graph: dict[Pair, float], settings: state.Structure) -> list[list[str]]:
    """Rule Y for the game modules of one segment: call-graph communities, then bounded by FILE count (a module counts
    its `files`: itself and the data files that will sit beside it). Over `dir_max` splits by bisection at the weakest
    cut, under `dir_min` merges into the neighbour with the strongest edge. Each community lists names in VRAM order,
    communities in order of their lowest VRAM. Edge weights are the sum of `graph` in either direction."""
    first = {m["name"]: m["vram"] for m in modules}
    files = {m["name"]: m.get("files", 1) for m in modules}
    w: dict[Pair, float] = defaultdict(float)
    for (a, b), x in graph.items():
        if a != b and a in first and b in first:
            w[(a, b) if a < b else (b, a)] += x
    names = sorted(first, key=lambda n: (first[n], n))
    pending = _cnm(names, dict(w), first)
    both = {**dict(w), **{(b, a): x for (a, b), x in w.items()}}
    groups: list[list[str]] = []
    while pending:
        g = pending.pop()
        if sum(files[n] for n in g) > settings.dir_max and len(g) > 1:
            pending += list(
                _bisect(g, both, files, first, min(settings.dir_min, max(1, sum(files[n] for n in g) // 2)))
            )
        else:
            groups.append(g)
    return _absorb(groups, both, files, first, settings)


def _absorb(
    groups: list[list[str]], w: dict[Pair, float], files: dict[str, int], first: dict[str, int], s: state.Structure
) -> list[list[str]]:
    def size(g: list[str]) -> int:
        return sum(files[n] for n in g)

    while True:
        small = sorted((g for g in groups if size(g) < s.dir_min), key=lambda g: (size(g), first[g[0]]))
        for g in small:
            fits = [h for h in groups if h is not g and size(g) + size(h) <= s.dir_max]
            if fits:
                near = min(fits, key=lambda h: (-_strongest(g, h, w), abs(first[h[0]] - first[g[0]]), first[h[0]]))
                groups.remove(g)
                near[:] = sorted([*near, *g], key=lambda n: (first[n], n))
                break
        else:
            return sorted(groups, key=lambda g: (first[g[0]], g[0]))


def adjacent(start: dict[str, int], owner: str, ends: dict[tuple[str, str], set[int]]) -> bool:
    """Rule D with owner 3: the data starts, in every version that holds it, where the owner's atoms end."""
    return bool(start) and all(r in ends.get((owner, v), ()) for v, r in start.items())


def data_owner(
    loaders: Mapping[str, Collection[str]], home: dict[str, str], room: dict[str, int], dir_max: int
) -> dict[str, str]:
    """Rule D for data loaded by several modules: the subsystem making at least twice the loads of the runner-up owns it
    (a `data_<VRAM>.c` in its folder) while its folder has room. Returns data module -> subsystem, or "" when shared.
    `loaders` lists the loading modules per data module, `home` maps a module to its subsystem, `room` is the files
    each subsystem folder has, updated as data joins."""
    out: dict[str, str] = {}
    for name in sorted(loaders):
        votes = Counter(home.get(m, f"?{m}") for m in loaders[name])
        top = [*votes.most_common(2), ("", 0)]
        win = top[0][0]
        if not win.startswith("?") and win in room and top[0][1] >= 2 * top[1][1] and room[win] < dir_max:
            out[name], room[win] = win, room[win] + 1
        else:
            out[name] = ""
    return out


def fuzzy_home(doc: Json, line: Sequence[Mod], first: str, home: dict[str, str]) -> dict[str, str]:
    """Gap 5: a fuzzy row with no placed module goes to the module of its ROM predecessor (the placed atom before it in
    names_from `.text` order). Returns member -> module. `home` maps the placed atoms to their modules."""
    text = sorted((a["text"][first]["rom_start"], a["name"]) for a in line if first in a["text"] and a["name"] in home)
    starts = [t for t, _ in text]
    real = {m["name"] for m in doc["modules"]}
    out: dict[str, str] = {}
    for m in doc["members"]:
        place = next((p for p in m["placements"] if p["version"] == first and p["section"] == ".text"), None)
        if m.get("fuzzy") and m["name"] not in home and m["module"] not in real and place and text:
            out[m["name"]] = home[text[max(0, bisect_left(starts, place["rom_start"]) - 1)][1]]
    return out


def chunks(names: Sequence[str], cap: int) -> list[list[str]]:
    """Names in order cut into folders of at most `cap` files each."""
    return [list(names[i : i + cap]) for i in range(0, len(names), cap)] or [[]]


def folder(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def paths(entry: Mod, naming: Naming = PLAIN) -> tuple[str, str]:
    """`source` and `header` of a placed module entry: game code in its subsystem, library code in its library,
    shared and unloaded data in `shared/` and `src/data/`. Library code keeps its own names; the rest takes `naming`."""
    stem, where = entry["name"], entry["folder"]
    if entry["library"]:
        return f"{where}/{stem}.c", f"include/sdk/{entry['library']}.h"
    source = naming.file(where, entry)
    if entry["sub"]:
        return source, naming.header(entry["sub"])
    return source, shared_headers()[1]


def stats(folders: Iterable[str], depth_of: Iterable[str]) -> dict[str, Any]:
    """Files per folder (max, median) and the deepest folder below the repo root, counting its `src/..` parts."""
    count = Counter(folders)
    return {
        "dir_max": max(count.values(), default=0),
        "dir_median": statistics.median(count.values()) if count else 0,
        "depth": max((p.count("/") for p in depth_of), default=0),
    }


def _pieces(modules: list[Mod], by_atom: dict[str, Mod], taken: set[str], first: str) -> list[Mod]:
    """Catalog hits override: a module's hit atoms leave it as one module per catalog (library, module), named after the
    catalog module. A module mixing libraries therefore splits by library; its other atoms keep the module."""
    out: list[Mod] = []
    for m in modules:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        lead: dict[str, Mod] = {}  # a C unit takes its first function's catalog hit: one compiled file, one module
        for n in m["atoms"]:
            a = lead.setdefault(by_atom[n]["unit"], by_atom[n]) if by_atom[n].get("c") else by_atom[n]
            groups[(a["sdk_library"], a.get("sdk_module") or "") if a.get("sdk_library") else ("", "")].append(n)
        if list(groups) == [("", "")]:
            out.append(m)
            continue
        for (library, stem), names in sorted(groups.items(), key=lambda g: vram(by_atom[g[1][0]], first)):
            piece = {**m, "atoms": names, "library": library}
            if library:
                base = stem or f"code_{vram(by_atom[names[0]], first):08X}"
                piece["name"] = unique(base, taken - {m["origin"]}, [by_atom[n] for n in names], first)
            else:
                piece["name"] = m["name"]
            taken.add(piece["name"])
            out.append(piece)
    return out


def place(
    doc: Json, every: Sequence[Mod], modules: list[Mod], settings: state.Structure, naming: Naming = PLAIN
) -> Json:
    """Give every proposed module its library or subsystem, folder, `source` and `header`. `modules` (name, segment,
    origin, atoms) is changed in place: catalog hits split, adjacent owned data merged into its module. Returns the
    facts the proposal needs: the call graph, the symbol renames, the batch order and the fuzzy rows given a home."""
    first = doc["names_from"]
    by_atom = {a["name"]: a for a in every}
    label = labels(doc)
    taken = {m["name"] for m in modules} | {m["name"] for m in doc["modules"]}
    modules[:] = _pieces(modules, by_atom, taken, first)
    for m in modules:
        atoms = [by_atom[n] for n in m["atoms"]]
        m |= {"label": label.get(m["segment"], m["segment"]), "vram": min(vram(a, first) for a in atoms)}
        m |= {"data": all(a["kind"] != "function" for a in atoms), "sub": "", "files": 1}
        m.setdefault("library", "")
    home = {n: m["name"] for m in modules for n in m["atoms"]}
    fuzzy = fuzzy_home(doc, every, first, home)
    for member, module in fuzzy.items():
        next(m for m in modules if m["name"] == module)["atoms"].append(member)
        by_atom.setdefault(member, next(x for x in doc["members"] if x["name"] == member))
    owner: dict[str, str] = {a["name"]: str(home.get(a["name"]) or a["module"] or "") for a in every}
    calls: Counter[Pair] = Counter((owner[a["name"]], owner[c]) for a in every for c in a["calls"] if c in owner)
    code = [m for m in modules if not m["data"]]
    libs = libraries(code, dict(calls), settings)
    for m in code:
        m["library"] = libs.get(m["name"], "")
    by_name = {m["name"]: m for m in modules}
    loaded: dict[str, set[str]] = defaultdict(set)
    for a in every:
        for t in a["loads"]:
            loaded[t].add(owner[a["name"]])
    ends: dict[tuple[str, str], set[int]] = defaultdict(set)
    for m in modules:
        for n in m["atoms"]:
            for p in by_atom[n]["placements"]:
                ends[m["name"], p["version"]].add(p["rom_end"])
    single: dict[str, str] = {}
    several: dict[str, set[str]] = {}
    for d in sorted((m for m in modules if m["data"]), key=lambda m: (m["label"], m["vram"], m["name"])):
        users = {u for n in d["atoms"] for u in loaded.get(n, ())}
        start = {p["version"]: p["rom_start"] for p in by_atom[d["atoms"][0]]["placements"]}
        who = next(iter(users)) if len(users) == 1 else ""
        if who in by_name and not by_name[who]["data"] and adjacent(start, who, ends):
            by_name[who]["atoms"] += d["atoms"]  # owned and adjacent: the data rides in the module
            for (mod, v), rom in list(ends.items()):
                if mod == d["name"]:
                    ends[who, v] |= rom
            modules.remove(d)
        elif who in by_name and not by_name[who]["data"]:
            single[d["name"]] = who
            by_name[who]["files"] += 1
        elif users:
            several[d["name"]] = users
    by_label: dict[str, list[Mod]] = defaultdict(list)
    for m in code:
        if m in modules and not m["library"]:
            by_label[m["label"]].append(m)
    for label_name, line in sorted(by_label.items()):
        graph: dict[Pair, float] = defaultdict(float)
        names = {m["name"] for m in line}
        graph.update({k: float(v) for k, v in calls.items() if k[0] in names and k[1] in names})
        for users in loaded.values():
            near = sorted(users & names)
            if 2 <= len(near) <= 8:
                for i, u in enumerate(near):
                    for v in near[i + 1 :]:
                        graph[u, v] += 1 / (len(near) - 1)
        gid: dict[str, str] = {
            m["name"]: str(m.get("group", m["name"])) for m in line
        }  # files cut apart by a type clash stay together
        kin: dict[str, list[str]] = defaultdict(list)
        for n, g in gid.items():
            kin[g].append(n)
        reps = [
            {"name": g, "vram": min(by_name[n]["vram"] for n in ns), "files": sum(by_name[n]["files"] for n in ns)}
            for g, ns in kin.items()
        ]
        joined: dict[Pair, float] = defaultdict(float)
        for (x, y), w in graph.items():
            if gid[x] != gid[y]:
                joined[gid[x], gid[y]] += w
        for group in subsystems(reps, dict(joined), settings):
            names_in = [n for g in group for n in kin[g]]
            low = f"{min(by_name[n]['vram'] for n in names_in):08X}"
            sub = naming.fill(naming.subsystem, label=label_name, vram=low).strip("/")
            for n in names_in:
                by_name[n] |= {"sub": sub, "folder": naming.subsystem_source.format(subsystem=sub)}
    room: Counter[str] = Counter()
    for m in code:
        room[m["sub"]] += m["files"] * bool(m["sub"])
    where = {m["name"]: m["sub"] for m in code if m["sub"]}
    for name, winner in data_owner(several, where, room, settings.dir_max).items():
        by_name[name]["owner_sub"] = winner
    for d in (m for m in modules if m["data"]):
        host = by_name.get(single.get(d["name"], ""))
        won = d.pop("owner_sub", "")
        if host:
            d |= {"sub": host["sub"], "library": host["library"], "folder": host.get("folder", "")}
        elif won:
            d |= {"sub": won, "folder": naming.subsystem_source.format(subsystem=won)}
        elif d["name"] in several:
            d["folder"] = (
                naming.fill(naming.shared_dir, label=d["label"], segment=d["segment"]).replace("//", "/").rstrip("/")
            )
        else:
            d["folder"] = (
                naming.fill(naming.data_dir, label=d["label"], segment=d["segment"]).replace("//", "/").rstrip("/")
            )
    for m in modules:
        if m["library"]:
            m["folder"] = f"src/sdk/{m['library']}"
    _split(modules, settings.dir_max, naming)
    where = {m["name"]: m["sub"] for m in modules}
    leaving: Counter[str] = Counter()  # calls a subsystem makes into another one: a leaf makes none
    for (x, y), n in calls.items():
        leaving[where.get(x, "")] += n * (where.get(x) != where.get(y) and bool(where.get(y)))
    for m in modules:
        m["source"], m["header"] = paths(m, naming)
        m["batch"] = m["folder"]
    twice = [p for p, n in Counter(m["source"].lower() for m in modules).items() if n > 1]
    if twice:  # a style whose stems drop the module name may give two modules one file
        raise ValueError(f"recipe naming: {len(twice)} paths name two modules, e.g. {twice[0]}")
    rank = {
        m["folder"]: (0 if m["library"] else 1 if m["sub"] else 2, leaving[m["sub"]] if m["sub"] else 0)
        for m in modules
    }
    symbols = {
        a["name"]: a["sdk_name"]
        for a in every
        if a["kind"] == "function" and a.get("sdk_name") not in (None, "", a["name"])
    }
    claimed = Counter(symbols.values())
    taken = {a["name"] for a in every}  # a name is renamed to only when no other member claims it or already has it
    symbols = {n: t for n, t in symbols.items() if claimed[t] == 1 and t not in taken}
    return {
        "order": sorted(rank, key=lambda f: (*rank[f], f)),
        "symbols": {n: t for n, t in symbols.items() if n in home},
        "fuzzy": fuzzy,
        "leaving": dict(leaving),
        "calls": calls,
    }


def _split(modules: list[Mod], cap: int, naming: Naming = PLAIN) -> None:
    """Libraries, shared data and unloaded data folders over `cap` files split in `part_<VRAM>` folders by ROM order."""
    by: dict[str, list[Mod]] = defaultdict(list)
    for m in modules:
        if m["library"] or not m["sub"]:
            by[m["folder"]].append(m)
    for where, line in by.items():
        line.sort(key=lambda m: (m["vram"], m["name"]))
        if len(line) > cap:
            for part in chunks([m["name"] for m in line], cap):
                head = next(m for m in line if m["name"] == part[0])
                for m in line:
                    piece = naming.fill(naming.part, vram=f"{head['vram']:08X}") if not m["library"] else ""
                    piece = piece or f"part_{head['vram']:08X}"
                    m["folder"] = f"{where}/{piece}" if m["name"] in part else m["folder"]
