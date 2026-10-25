"""Slices: one owner per placeholder target, computed, not claimed (CLEAN-PLAN-O1 T3).

`recipe name slices --lanes N` cuts the project's functions into SLICES (80-100 functions, about 20 KB of code, in
address order inside one group: a module of the module map, else a source folder), hands every datum read only by one
slice's functions to that slice and every datum read by two or more (or by none) to a GLOBALS slice, and every record
with unnamed members to a STRUCT slice by defining file. The result is one JSON document (`slices.json`, in the
names directory with the ledger). The ledger asks it, per submitted row, whether the submitting lane owns the target
(`check`); what a lane learns about a target another slice owns goes in as a `suggest` row (`add_suggestion`), which
nothing applies: the owner reads it in its next pack (`suggestions_for`).

Keys are the ledger's stable row keys (`rowkeys`), so a slice survives renames, moves and matches under it."""

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from unbake.contracts import Json

from recipemaker import access, cparse, host, locals_, namebook
from recipemaker import members as members_

FORMAT = 1
KSEG0 = 0x80000000  # a symbol below it is a ROM offset spelled like a datum, not a place in memory
MAX_FNS = 100  # a slice holds at most this many functions ...
MAX_BYTES = 26000  # ... and about this much code
MIN_FNS = 40  # a smaller tail joins the slice next to it in the same top folder
PER_KIND = 8  # one GLOBALS and one STRUCT slice per this many lanes (at least one each)
# an address-named datum a lane can name: blob and auto-split members are data typing work, jump tables are generated
DATUM = re.compile(r"D_[0-9A-F]{8}(?:_[a-z0-9]+)*")
UNKNOWN = re.compile(r"(?:unk|pad)_?[0-9A-Fa-f]+")


def names_dir() -> Path:
    """Where the ledger, slices and suggestions live: the name book's store (NAMES_STATE, else ~/rig-state/names)."""
    return namebook.store()


def fn_key(version: str, vram: int) -> str:
    return f"fn:{version}:{vram:08X}"


def datum_key(version: str, vram: int) -> str:
    return f"datum:{version}:{vram:08X}"


def slot_key(function: str, slot: str) -> str:
    """A parameter or local (P0-CONTRACT 1): `slot:VER:VRAM8:OLD`, its function's key without the `fn:` and the slot."""
    return f"slot:{function.removeprefix('fn:')}:{slot}"


def member_key(tag: str, offset: int) -> str:
    return f"member:{tag}+0x{offset:X}"


def fn_of_slot(key: str) -> str:
    """The function key a slot key belongs to."""
    return "fn:" + key.removeprefix("slot:").rsplit(":", 1)[0]


def _first(places: Sequence[Mapping[str, Any]], first: str) -> tuple[str, int] | None:
    """(version, vram) of a member: the names_from placement, else the first version that places it."""
    found = [(p["version"], p["vram"]) for p in places]
    pick = next((f for f in found if f[0] == first), found[0] if found else None)
    return pick


def chunks(items: Sequence[Any], size: Callable[[Any], int]) -> list[list[Any]]:
    """ITEMS (kept in order) cut into the fewest slices within MAX_FNS and MAX_BYTES, balanced by a weight that counts
    both limits so no slice is a sliver; a slice still over a limit is halved by count."""
    if not items:
        return []
    total = sum(size(i) for i in items)
    parts = max(-(-len(items) // MAX_FNS), -(-total // MAX_BYTES), 1)

    def cost(item: Any) -> float:
        return 1 / MAX_FNS + size(item) / MAX_BYTES

    out: list[list[Any]] = []
    left, rest = list(items), sum(cost(i) for i in items)
    for k in range(parts, 0, -1):
        goal = rest / k
        got, taken = 0.0, 0
        for i, item in enumerate(left):
            if k > 1 and got >= goal and taken:
                break
            if k > 1 and len(left) - i <= k - 1:
                break
            got += cost(item)
            taken += 1
        out.append(left[:taken])
        rest -= got
        left = left[taken:]
    fitted: list[list[Any]] = []
    while out:
        one = out.pop(0)
        if len(one) > 1 and (len(one) > MAX_FNS or sum(size(i) for i in one) > MAX_BYTES):
            half = len(one) // 2
            out[:0] = [one[:half], one[half:]]
        elif one:
            fitted.append(one)
    return fitted


def read_map(path: Path) -> dict[str, str]:
    """A module map: `unit<TAB>module` lines (a first line `unit	module` and `#` comments are skipped). The slicer
    groups the functions of a unit under its module; a unit the map does not list keeps its folder (the input is
    pluggable: MODULES.tsv adds the real module names, nothing here depends on its other columns)."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[0] and not cols[0].startswith("#") and cols[0] != "unit":
            out[cols[0]] = cols[1]
    return out


def compute(
    functions: Sequence[Json],
    data: Sequence[Json],
    records: Sequence[Json],
    lanes: int,
    previous: Json | None = None,
    module_of: Mapping[str, str] | None = None,
    first: str = "us-rev1",
) -> Json:
    """The slice document.

    functions  [{key, name, group, parent, vram, size}] every function a lane may name (the group is its folder or
               module; the parent its top folder)
    data       [{key, name, readers}] every datum with the names of the functions that read it
    records    [{key, tag, path, unknown}] every record with unnamed members, by defining file
    lanes      the lane count: one GLOBALS and one STRUCT slice per PER_KIND lanes
    previous   the document before, whose lane assignments carry over to slices that still exist"""
    by_group: dict[str, list[Json]] = defaultdict(list)
    for f in functions:
        by_group[(module_of or {}).get(f.get("unit", ""), f["group"])].append(f)
    cut: list[tuple[str, str, list[Json]]] = []  # (group, parent, functions) in address order of the group
    for group in sorted(by_group, key=lambda g: min(f["vram"] for f in by_group[g])):
        fns = sorted(by_group[group], key=lambda f: f["vram"])
        cut += [(group, fns[0]["parent"], c) for c in chunks(fns, lambda f: f["size"])]
    merged: list[tuple[list[str], str, list[Json]]] = []
    for group, parent, fns in cut:  # a sliver joins the slice before it when both fit and share a top folder
        if merged and len(fns) < MIN_FNS and merged[-1][1] == parent:
            before = merged[-1][2]
            if len(before) + len(fns) <= MAX_FNS and sum(f["size"] for f in before + fns) <= MAX_BYTES:
                merged[-1] = (merged[-1][0] + [group], parent, before + fns)
                continue
        merged.append(([group], parent, fns))
    slices: list[Json] = []
    owner: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for n, (groups, parent, fns) in enumerate(merged, 1):
        sid = f"S{n:03d}"
        slices.append(
            {
                "id": sid,
                "kind": "slice",
                "groups": list(dict.fromkeys(groups)),
                "parent": parent,
                "fns": [f["key"] for f in fns],
                "bytes": sum(f["size"] for f in fns),
                "data": [],
            }
        )
        for f in fns:
            owner[f["key"]] = sid
            by_name[f["name"]] = f["key"]
    fn_slice = {f["name"]: owner[f["key"]] for _, _, fns in merged for f in fns}
    shared: list[Json] = []
    unread: list[str] = []
    byid = {s["id"]: s for s in slices}
    for d in sorted(data, key=lambda d: d["key"]):
        readers = {fn_slice[r] for r in d["readers"] if r in fn_slice}
        by_name[d["name"]] = d["key"]
        if len(readers) == 1:
            sid = next(iter(readers))
            byid[sid]["data"].append(d["key"])
            owner[d["key"]] = sid
        elif readers:
            shared.append(d)
        else:
            unread.append(d["key"])  # no function of a slice reads it: no code evidence, nobody to own it
    kinds = max(1, round(lanes / PER_KIND))
    globals_ = [
        {"id": f"G{n:02d}", "kind": "globals", "data": [d["key"] for d in part], "fns": []}
        for n, part in enumerate(_even(shared, kinds), 1)
    ]
    for g in globals_:
        owner.update({k: g["id"] for k in g["data"]})
    ranked = sorted(records, key=lambda r: (r["path"], r["tag"]))
    structs = [
        {
            "id": f"T{n:02d}",
            "kind": "struct",
            "headers": sorted({r["path"] for r in part}),
            "records": [r["key"] for r in part],
            "unknown": sum(r["unknown"] for r in part),
        }
        for n, part in enumerate(_even(ranked, kinds, lambda r: max(1, r["unknown"])), 1)
    ]
    tags: dict[str, str] = {}
    for t in structs:
        part = {r["key"]: r for r in ranked}
        for key in t["records"]:
            tags[key] = t["id"]
            tags[part[key]["tag"]] = t["id"]
    assigned = {
        lane: [s for s in ids if s in {*byid, *(g["id"] for g in globals_), *(t["id"] for t in structs)}]
        for lane, ids in ((previous or {}).get("assigned") or {}).items()
    }
    return {
        "format": FORMAT,
        "names_from": first,
        "lanes": lanes,
        "slices": [*slices, *globals_, *structs],
        "owner": owner,
        "by_name": by_name,
        "records": tags,
        "unread": unread,
        "assigned": {k: v for k, v in assigned.items() if v},
    }


def _even(items: Sequence[Any], parts: int, weight: Callable[[Any], int] = lambda _: 1) -> list[list[Any]]:
    """ITEMS in order cut into PARTS runs of about equal weight (an empty run is dropped)."""
    total = sum(weight(i) for i in items) or 1
    out: list[list[Any]] = [[] for _ in range(parts)]
    run = 0
    for item in items:
        out[min(parts - 1, run * parts // total)].append(item)
        run += weight(item)
    return [o for o in out if o]


def path(directory: Path | None = None) -> Path:
    return (directory or names_dir()) / "slices.json"


def load(directory: Path | None = None) -> Json | None:
    try:
        return json.loads(path(directory).read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError):
        return None


def save(doc: Json, directory: Path | None = None) -> Path:
    """The document written whole by rename, so a reader never sees half of it."""
    target = path(directory)
    host.write(target, json.dumps(doc, separators=(",", ":")).encode())
    return target


def assign(doc: dict[str, Any], lane: str, ids: Iterable[str]) -> dict[str, Any]:
    """LANE owns the slices IDS and no other (a slice is held by one lane: it leaves any lane that held it)."""
    known = {s["id"] for s in doc["slices"]}
    wanted = [i for i in ids if i in known]
    held = {k: [s for s in v if s not in wanted] for k, v in doc.get("assigned", {}).items() if k != lane}
    doc["assigned"] = {k: v for k, v in {**held, lane: wanted}.items() if v}
    return doc


def lane_slices(doc: Json, lane: str) -> list[str]:
    """The slice ids a lane submits for: its assigned ones, or the slice whose id the lane is named by."""
    ids = {s["id"] for s in doc["slices"]}
    return list(doc.get("assigned", {}).get(lane, [])) or ([lane] if lane in ids else [])


def key_of(doc: Json, row: Mapping[str, Any]) -> str:
    """The key of the target a row names: its own `key`, else the document's key for the target's name; a local is
    keyed by its function; a field by its record."""
    if isinstance(row.get("key"), str) and row["key"]:
        return str(row["key"])
    kind, target = row.get("kind"), str(row.get("target") or "")
    by_name = doc.get("by_name", {})
    if kind in ("local", "param"):
        return slot_key(by_name.get(str(row.get("function") or ""), "fn:?"), target)
    if kind == "field":
        return str(doc.get("records", {}).get(str(row.get("struct") or ""), ""))
    return str(by_name.get(target, ""))


def owner_of(doc: Json, key: str) -> str:
    """The slice id that owns KEY, "" when none does."""
    if key.startswith("slot:"):
        key = fn_of_slot(key)
    if key.startswith("member:"):
        return str(doc.get("records", {}).get(key.removeprefix("member:").rsplit("+", 1)[0], ""))
    if key.count(":") == 1:  # the short form `fn:80123456` is the names_from version
        key = f"{key.split(':')[0]}:{doc.get('names_from', '')}:{key.split(':')[1]}"
    return str(doc["owner"].get(key, ""))


def check(doc: Json | None, lane: str, row: Mapping[str, Any]) -> tuple[str, str]:
    """("", "") when LANE may name the row's target, else (rule, reason): the `owner` rule. A `suggest` row is allowed
    to any lane that holds a slice (it names a target another slice owns; nothing applies it). Subsystem and module
    rows are not placeholder targets: they stay with the planner."""
    if doc is None:
        return "", ""  # no slice file: the pass has not started, the ledger is not gated
    if row.get("kind") in ("subsystem", "module"):
        return "", ""
    mine = lane_slices(doc, lane)
    if not mine:
        return "owner", f"lane {lane} holds no slice: ask the planner for one (recipe name slices --assign {lane}=ID)"
    if row.get("kind") == "suggest":
        return "", ""
    key = key_of(doc, row)
    if row.get("kind") == "field":
        owner = str(doc.get("records", {}).get(str(row.get("struct") or ""), ""))
    else:
        owner = owner_of(doc, key) if key else ""
    if not owner:
        return "owner", f"{row.get('target')} belongs to no slice (not a placeholder target of this pass)"
    if owner not in mine:
        return "owner", (
            f"{row.get('target')} belongs to slice {owner}, not to {lane} (slices {', '.join(mine)}): "
            f"submit it as a `suggest` row and its owner sees it in the next pack"
        )
    return "", ""


def suggestions_path(directory: Path | None = None) -> Path:
    return (directory or names_dir()) / "suggest.jsonl"


def canon_key(key: str, first: str) -> str:
    """The long form of a key: `fn:80123456` is `fn:<names_from>:80123456` (a slot's too); other keys as they are."""
    head, _, rest = key.partition(":")
    if head in ("fn", "datum") and rest.count(":") == 0 and rest:
        return f"{head}:{first}:{rest}"
    if head == "slot" and rest.count(":") == 1:
        vram, _, slot = rest.partition(":")
        return f"slot:{first}:{vram}:{slot}"
    return key


def suggestions_for(
    keys: Iterable[str], directory: Path | None = None, first: str = "us-rev1"
) -> dict[str, list[Json]]:
    """The `suggest` rows the ledger kept (suggest.jsonl, written by `submit --ledger`) for KEYS; a slot's are held
    under its function's key too. Newest last. A suggestion is a note for the owner: nothing applies it."""
    wanted = set(keys)
    out: dict[str, list[Json]] = defaultdict(list)
    try:
        lines = suggestions_path(directory).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        key = canon_key(str(entry.get("key", "")), first)
        for k in (key, fn_of_slot(key) if key.startswith("slot:") else ""):
            if k in wanted:
                out[k].append(entry)
    return dict(out)


def summary(doc: Json) -> list[str]:
    kinds: dict[str, list[Json]] = defaultdict(list)
    for s in doc["slices"]:
        kinds[s["kind"]].append(s)
    sl = kinds.get("slice", [])
    gl, st = kinds.get("globals", []), kinds.get("struct", [])
    biggest = max((len(s["fns"]) for s in sl), default=0)
    heaviest = max((s["bytes"] for s in sl), default=0)
    return [
        f"{len(sl)} slices, {sum(len(s['fns']) for s in sl)} functions, {sum(len(s['data']) for s in sl)} slice data"
        f" (largest {biggest} fns, {heaviest} bytes)",
        f"{len(gl)} GLOBALS slices holding {sum(len(s['data']) for s in gl)} data,"
        f" {len(doc.get('unread', []))} data no function reads",
        f"{len(st)} STRUCT slices holding {sum(s['unknown'] for s in st)} members",
    ]


def _size(member: Json, first: str) -> int:
    places = member["placements"]
    pick = next((p for p in places if p["version"] == first), places[0] if places else None)
    return pick["rom_end"] - pick["rom_start"] if pick else 0


def collect(ctx: Any, root: Path) -> tuple[list[Json], list[Json], list[Json]]:
    """(functions, data, records) for `compute`, read from the naming evidence map CTX and the sources under ROOT.
    Functions the SDK table names are left to the auto-namers; data is every unnamed datum with its readers."""
    first = ctx.doc["names_from"]
    functions: list[Json] = []
    data: list[Json] = []
    for name, m in ctx.members.items():
        where = _first(m["placements"], first)
        if where is None:
            continue
        if m["kind"] == "function":
            if ctx.sdk_name(name):
                continue
            folder = ctx.folder_of(name) or "?"
            functions.append(
                {
                    "key": fn_key(*where),
                    "name": name,
                    "group": folder,
                    "parent": re.sub(r"_\d+$", "", folder),
                    "vram": where[1],
                    "size": _size(m, first),
                    "unit": m.get("unit") or "",
                }
            )
        elif DATUM.fullmatch(name) and where[1] >= KSEG0:
            data.append({"key": datum_key(*where), "name": name, "readers": list(ctx.callers.get(name, ()))})
    seen = {d["name"] for d in data}
    for (
        version,
        table,
    ) in ctx.tables.items():  # a neutral datum no module owns (bss): keyed by the first table that has it
        for name, row in table.items():
            if (
                name not in seen
                and name not in ctx.members
                and DATUM.fullmatch(name)
                and row[0] >= KSEG0
                and ctx.canon(name) == name
                and ctx.symbol_kind(name) == "global"
            ):
                seen.add(name)
                data.append(
                    {"key": datum_key(version, row[0]), "name": name, "readers": list(ctx.callers.get(name, ()))}
                )
    for name, readers in source_readers(ctx, root, {d["name"] for d in data if not d["readers"]}).items():
        next(d for d in data if d["name"] == name)["readers"] = readers  # `body` evidence: the C that spells it
    return functions, data, records_of(root)


def source_readers(ctx: Any, root: Path, wanted: set[str]) -> dict[str, list[str]]:
    """The functions whose C body spells a datum of WANTED (the assembly graph has no reader of it): name -> readers."""
    if not wanted:
        return {}
    token = re.compile(r"\bD_[0-9A-F]{8}(?:_[a-z0-9]+)*\b")
    index = {w: w for w in wanted}
    found: dict[str, list[str]] = {}
    units = {m.get("unit") for m in ctx.members.values() if m["kind"] == "function" and m.get("unit")}
    for unit in sorted(u for u in units if u):
        try:
            text = (root / unit).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not token.search(text):
            continue
        masked = access.mask(text)
        for fn in locals_.functions(text):
            for t in set(token.findall(masked[fn.start : fn.end])):
                if (name := index.get(t) or index.get(ctx.canon(t), "")) and fn.name in ctx.members:
                    found.setdefault(name, []).append(fn.name)
    return found


def records_of(root: Path) -> list[Json]:
    """The records with unnamed members (unkNN, padNN) of every header and source, by defining file."""
    out: list[Json] = []
    for path_, text in sorted(members_.texts_of(root).items()):
        for alias, tag, fields, keyword in cparse.scan(text)[0]:
            name = alias or tag
            unknown = sum(1 for f in fields if f[2] and UNKNOWN.fullmatch(f[2]))
            if name and unknown:
                out.append({"key": alias or f"{keyword} {tag}", "tag": name, "path": path_, "unknown": unknown})
    return out


def lane_file(doc: Json, lane: str) -> Json:
    """The ownership file of a lane (P0-CONTRACT 3): the keys its slices own. `records` are the record tags of a STRUCT
    slice. A lane holding several slices owns the union."""
    mine = [s for s in doc["slices"] if s["id"] in lane_slices(doc, lane)]
    held = [t for t, sid in doc.get("records", {}).items() if sid in {s["id"] for s in mine}]
    return {
        "lane": lane,
        "slice": ",".join(s["id"] for s in mine),
        "kind": mine[0]["kind"] if mine else "",
        "fns": [k for s in mine for k in s.get("fns", [])],
        "data": [k for s in mine for k in s.get("data", [])],
        "records": sorted(h for h in held if not h.startswith(("struct ", "union "))),
    }


def write_lane_files(doc: Json, directory: Path | None = None) -> list[Path]:
    """One `slices/LANE.json` per assigned lane, whole by rename; a lane no longer assigned loses its file."""
    folder = (directory or names_dir()) / "slices"
    folder.mkdir(parents=True, exist_ok=True)
    wrote: list[Path] = []
    for lane in doc.get("assigned", {}):
        target = folder / f"{lane}.json"
        host.write(target, json.dumps(lane_file(doc, lane), separators=(",", ":")).encode())
        wrote.append(target)
    for old in folder.glob("*.json"):
        if old not in wrote:
            old.unlink()
    return wrote
