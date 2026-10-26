"""Module structure as a file patch: evidence from unbake's public surface, a proposal from it, one unified diff that
moves functions between files, renames files and edits includes. `recipe apply` applies it, then runs sync.

The evidence is the slim `unbake map` (module paths, segment ownership, stand-ins, fuzzy names) with the placements of
the captured tree's symbol tables. It carries no call, load or object-edge facts and no header graph, so nothing here
proposes from them: a module is cut only where an evidence row or the size cap says, keeps its origin's folder, and no
header is merged or deleted."""

import contextlib
import difflib
import json
import os
import posixpath
import re
import signal
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib.util import find_spec
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal, digest, file_facts

from recipemaker import access, clash, cparse, debug, repair, state, tree, world
from recipemaker import host as recipe_host

Segments = str | Collection[str] | None
PROSE = ("docs/sweep.toml",)  # text outside src/ and include/ that names symbols in sentences: renamed with them
LITERALS = "literals_"  # compiler-emitted read-only data a fact gives the file of its function: it moves with that file


def say(text: str) -> None:
    """One line on stderr for whoever watches the run (stdout carries the JSON)."""
    sys.stderr.write(f"{text}\n")
    sys.stderr.flush()


@contextmanager
def narrated(name: str) -> Iterator[None]:
    """A stage of `recipe structure`, told on stderr in unbake's spelling (`LABEL started`, then `LABEL 1.2s 1.00
    cores 0 jobs`) and recorded in unbake's stage tree, so a long run is never silent."""
    label, began, spent = f"structure > {name}", time.monotonic(), sum(os.times()[:4])
    say(f"{label} started")
    with recipe_host.stage(f"structure.{name}"):
        yield
    wall = time.monotonic() - began
    say(f"{label} {wall:.1f}s {(sum(os.times()[:4]) - spent) / max(wall, 1e-9):.2f} cores 0 jobs")


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


def _state(config: Config) -> str:
    """A cheap fingerprint of the tree state and of the code that reads it: HEAD, each changed or untracked file's
    stat, the extracted assembly's marker files, and the stat of the Recipe and unbake sources."""
    root = config.project.root
    try:
        head = recipe_host.git(config, "rev-parse", "HEAD").stdout
        dirty = recipe_host.git(config, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    except Refusal:
        return ""  # no git state to name: nothing is reused
    paths = [root / p[3:].decode() for p in bytes(dirty).split(b"\0") if len(p) > 3]
    paths += [m for m in (root / "asm").glob("*/.*") if m.is_file()]
    unbake = find_spec("unbake")
    code = [Path(__file__).parent, Path(unbake.origin).parent if unbake and unbake.origin else root]
    paths += [*code[0].rglob("*.py"), *code[1].rglob("*.py")]
    stats = [(str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.is_file()]
    return digest([bytes(head).decode(), sorted(stats)])


def evidence(config: Config, host: Path | None = None) -> Json:
    """The structure evidence of the tree as it is, built once per tree state: every naming step and submit reads the
    document of the last call when HEAD, the changed files and the Recipe and unbake sources are the same."""
    key, path = _state(config), config.project.root / ".unbake" / "recipe" / "evidence.json"
    try:
        if not key:
            raise KeyError(key)
        held = json.loads(path.read_text(encoding="utf-8"))
        if held["key"] == key:
            say("structure > evidence cached")
            return held["document"]  # type: ignore[no-any-return]
    except (OSError, ValueError, KeyError):
        pass
    document = _evidence(config, host)
    if key:
        recipe_host.write(path, json.dumps({"key": key, "document": document}, separators=(",", ":")).encode())
    return document


def _evidence(config: Config, host: Path | None) -> Json:
    """The structure evidence: `unbake map` for the modules and their segments, the captured tree of the same digest
    for sections and `api.symbols` for placements."""
    with narrated("map"):
        document, _ = _unbake(config, host, "map")
    if not document["ok"]:
        raise _refusal(document)
    slim = json.loads((config.project.root / document["result"]["path"]).read_text(encoding="utf-8"))
    with narrated("capture"):
        captured = api.capture(config)
        if captured.digest != slim["digest"]:
            raise Refusal(Finding("recipe.evidence", "the tree changed between unbake map and its capture"))
        tables = {v: api.symbols(config, v) for v in slim["versions"]}
    with narrated("document"):
        return document_of(config, slim, captured, tables)


def document_of(config: Config, slim: Json, captured: api.Tree, tables: Mapping[str, Mapping[str, Any]]) -> Json:
    """The evidence document: per module (one source file) its segment, header and members in names_from file order;
    per module with C definitions its unit (toolchain and options of its [file] row); per member its kind, module,
    unit (the module when it is C, None behind a stand-in or NON_MATCHING) and placements per version."""
    project, first = config.project, slim["names_from"]
    sections = {m.path: m.sections for m in captured.modules}
    owned = {n for row in slim["modules"] for names in row["owns"].values() for n in names}
    ends = {v: world.extents(t, owned) for v, t in tables.items()}
    modules: list[Json] = []
    units: list[Json] = []
    members: list[Json] = []
    for row in slim["modules"]:
        path, owns = row["path"], row["owns"]
        order: list[str] = []
        for v in sorted(owns, key=lambda v: (v != first, v)):
            order += [n for n in owns[v] if n not in order and not n.startswith(LITERALS)]
        stand = {v: set(row["stand_ins"].get(v, ())) for v in owns}
        held, landed = [], []
        for name in order:
            places = []
            for v in sorted(owns):
                if name not in owns[v] or name not in tables.get(v, {}):
                    continue
                vram, rom, size, kind = tables[v][name]
                section = sections.get(path, {}).get(v, {}).get(name) or (".text" if kind == "func" else ".data")
                start, extent = rom or 0, size or ends[v].get(name, 0)
                end = start if section == ".bss" else start + extent
                places.append({"version": v, "section": section, "rom_start": start, "rom_end": end, "vram": vram})
            if not places:
                continue
            c = name not in row["fuzzy"] and not any(name in stand[p["version"]] for p in places)
            held.append(name)
            landed += [name] * c
            members.append(
                {
                    "name": name,
                    "kind": "function" if tables[places[0]["version"]][name][3] == "func" else "data",
                    "module": path,
                    "unit": path if c else None,
                    "fuzzy": path if name in row["fuzzy"] else "",
                    "segment": row["segment"],
                    "placements": places,
                }
            )
        if not held:
            continue
        file = project.root / path
        header = world.header_of(path, file.read_bytes() if file.is_file() else b"")
        modules.append({"name": path, "source": path, "header": header, "segment": row["segment"], "members": held})
        if landed:
            facts = file_facts(project, path)
            options = {"add": list(facts.cflags), "omit": list(facts.omit)}
            tool = facts.toolchain or project.toolchain
            units.append({"path": path, "module": path, "members": landed, "toolchain": tool, "options": options})
    versions = list(slim["versions"])
    return {"digest": slim["digest"], "names_from": first, "versions": versions, **_rows(modules, units, members)}


def _rows(modules: list[Json], units: list[Json], members: list[Json]) -> Json:
    return {"modules": modules, "units": units, "members": members}


def _sections(m: Json) -> dict[str, dict[str, Any]]:
    """The placements an atom is ordered by, per version: `.text` of a function, else the first data section it has."""
    section = ".text" if m["kind"] == "function" else min((p["section"] for p in m["placements"]), default="")
    return {p["version"]: p for p in m["placements"] if p["section"] == section}


def _inside(path: str, folders: Sequence[str]) -> bool:
    return any(path.startswith(p.rstrip("/") + "/") for p in folders)  # directory membership, not a name prefix


def atoms(doc: Json, library: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The function atoms and the C data atoms with placement, toolchain, options and library protection."""
    unit = {u["path"]: u for u in doc["units"]}
    found: list[dict[str, Any]] = []
    for m in doc["members"]:
        t = _sections(m)
        if t and (m["kind"] == "function" or m["unit"]):
            u = unit.get(m["unit"] or "", {})
            found.append(
                {
                    **m,
                    "text": t,
                    "section": next(iter(t.values()))["section"],
                    "tool": u.get("toolchain"),
                    "opts": json.dumps(u.get("options"), sort_keys=True) if u else None,
                    "c": bool(m["unit"]),
                    "protected": _inside(m["module"], library),
                }
            )
    return found


def _padding(a: Json) -> bool:
    """A padding unit: data the linker left between two others, which belongs to the one before it."""
    return a["kind"] != "function" and "_padding_" in a["name"]


def _place(a: Json, first: str) -> tuple[str, int]:
    """Where an atom sits: (version, ROM start) in names_from, else in the lowest version that holds it."""
    v = first if first in a["text"] else min(a["text"])
    return v, int(a["text"][v]["rom_start"])


def evidenced_path(source: str, naming: tree.Naming = tree.PLAIN) -> tuple[str, str]:
    """(subsystem, source path) an evidence path names in the project's naming style: by default
    GAME/ACTORS/PLAYER.CPP is src/game/game/actors/player.c; a style with `evidence = "keep"` keeps its spelling."""
    return naming.evidenced(source)


def _objects(every: Sequence[Json], first: str, rows: Sequence[Json]) -> dict[str, Any]:
    """The object of each atom. A source file is one object; an evidence row's span of functions (first to last in
    .text order of a version holding both) is one object across files, which also cuts it from the rest of theirs;
    a file of padding only belongs to the object of the atom before it. Nothing else joins or cuts: where one object
    ends is a call, load or object-edge fact, and unbake's map carries none."""
    key: dict[str, Any] = {a["name"]: a["module"] for a in every}
    text: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for a in every:
        if a["kind"] == "function":
            for v, p in a["text"].items():
                text[v].append((p["rom_start"], a["name"]))
    for held in text.values():
        names = [n for _, n in sorted(held)]
        at = {n: i for i, n in enumerate(names)}
        for i, row in enumerate(rows):
            if row["first"] in at and row["last"] in at:
                key |= {n: ("row", i) for n in names[at[row["first"]] : at[row["last"]] + 1]}
    pad = {a["module"] for a in every} - {a["module"] for a in every if not _padding(a)}
    for v in sorted({_place(a, first)[0] for a in every}):
        line = sorted((p["rom_start"], a["name"], a) for a in every if (p := a["text"].get(v)))
        for (_, _, a), (_, _, b) in pairwise(line):
            if b["module"] in pad and key[b["name"]] == b["module"] and a["module"] != b["module"]:
                key |= {n: key[a["name"]] for n, k in key.items() if k == b["module"]}
                pad.discard(b["module"])
    return key


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


def propose(
    doc: Json,
    settings: state.Structure,
    segment: Segments = None,
    library: Sequence[str] = (),
    types: Callable[[str], clash.Types] | None = None,
    evidence: Sequence[Json] = (),
    naming: tree.Naming = tree.PLAIN,
    sdk: Mapping[tuple[str, int], str] | None = None,
) -> Json:
    """Propose one module per object, named by its address, in the folder of the file it comes from.
    Scope is all segments or the requested segment names; `types` splits an object whose C files define a type
    differently. An object holding a function `sdk` names ((version, vram) -> SDK name) is a library object and goes
    under the library folder (`_library_folder`)."""
    wanted = None if segment is None else frozenset(segment.split(",") if isinstance(segment, str) else segment)
    first = doc["names_from"]
    every = atoms(doc, library)
    scope = sorted({a["segment"] for a in every if wanted is None or a["segment"] in wanted})
    existing = {m["name"]: m for m in doc["modules"]}
    held: dict[str, list[str]] = {n: [] for n in existing}
    for a in every:
        held.setdefault(a["module"], []).append(a["name"])
    free = [a for a in every if a["segment"] in scope and not a["protected"]]
    key = _objects(free, first, evidence)
    groups: dict[Any, list[Json]] = defaultdict(list)
    # an object's atoms in the names_from ROM order, then those only other versions hold: its first atom (which names
    # and addresses the module) is where it starts in names_from, never an atom (a padding, a version's own function)
    # that only another version holds and that sorts first by that version's name
    for a in sorted(free, key=lambda a: (a["segment"], first not in a["text"], *_place(a, first), a["name"])):
        groups[key[a["name"]]].append(a)
    pieces: list[list[Json]] = []
    for piece in groups.values():
        units = {a["unit"] for a in piece if a["c"]}
        pieces += _by_types(piece, types) if types is not None and len(units) > 1 else [piece]
    modules: list[dict[str, Any]] = []
    taken: set[str] = set()
    kept: set[str] = set()  # the files a module keeps: renamed, not emptied
    for piece in sorted(pieces, key=lambda p: (p[0]["segment"], *_place(p[0], first))):
        origin = max(sorted({a["module"] for a in piece}), key=[a["module"] for a in piece].count)
        data = all(a["kind"] != "function" for a in piece)
        vram = piece[0]["text"].get(first) or piece[0]["text"][min(piece[0]["text"])]
        base = f"{'data' if data else 'code'}_{vram['vram']:08X}"
        unique, address = tree.identity(base, taken | (set(existing) - {origin}), piece, first)
        modules.append({"name": unique, "segment": piece[0]["segment"], "origin": origin, "address": address})
        modules[-1] |= {"atoms": [a["name"] for a in piece], "keeps": origin not in kept}
        kept.add(origin)
        taken.add(unique)
    order = tree.place(doc, every, modules, naming)
    for row in evidence:  # the module an evidence row proves takes the folder and file name the evidence gives
        for m in modules:
            if row["first"] in m["atoms"] and row["last"] in m["atoms"]:
                sub, source = evidenced_path(row["source"], naming)
                m |= {"sub": sub, "folder": source.rpartition("/")[0], "source": source, "evidence": row["source"]}
                m["header"] = naming.header(sub) if sub else m["header"]
                m["batch"] = m["folder"]
                if m["batch"] not in order:
                    order.insert(0, m["batch"])  # its own batch, before the folders the map already holds
    by_atom = {a["name"]: a for a in every}
    for m in modules:  # a library object found in a game folder: the vendor's archive member, never game code
        named = [n for a in m["atoms"] if (n := _sdk_name(by_atom[a], sdk or {}))]
        if named and not _inside(m["origin"], library):
            folder = _library_folder(named, [x["source"] for x in doc["modules"]], library)
            m |= {"folder": folder, "source": naming.file(folder, m), "sdk": named}
            m["header"] = _sdk_header(m["source"], m["header"]) if m["header"] else ""
            m["batch"] = folder
            if folder not in order:
                order.append(folder)
    proposed_paths: set[str] = set()
    for m in modules:
        if why := api.name_issues(m["name"], "file"):
            raise state.stop("recipe.structure", why[0])
        for path_key in ("source", "header"):
            value = m.get(path_key, "")
            if value:
                if why := api.name_issues(value, "file", sorted(proposed_paths - {value})):
                    raise state.stop("recipe.structure", why[0])
                proposed_paths.add(value)
    refused = _data_guard(modules, existing, {a["name"]: a for a in every})
    is_c = {a["name"]: a["c"] for a in every}
    counted = [m for m in modules if any(is_c.get(n) for n in m["atoms"])]  # asm-only modules are no C units
    touched = {a["module"] for a in free}
    pad = {m["name"] for m in doc["members"] if _padding(m)}
    folded = {a for m in modules if not set(m["atoms"]) <= pad for a in m["atoms"] if a in pad}  # rides in a module
    alone = [u for u in doc["units"] if u["members"] and set(u["members"]) <= pad]
    padding = len([u for u in alone if not set(u["members"]) <= folded])
    sizes = [len(held[n]) for n in touched if n in held]
    func = [m["name"] for m in modules if m["name"].startswith("func_")]
    where = tree.stats((m["folder"] for m in modules), (m["source"] for m in modules))
    rest = len([u for u in doc["units"] if u["module"] not in touched])
    return {
        "scope": scope,
        "modules": modules,
        "boundaries": [dict(row) for row in evidence],
        "refused": refused,
        "order": order,
        "current": {a["name"]: a["module"] for a in every},
        "existing": {n: {"source": m["source"], "header": m["header"], "atoms": held[n]} for n, m in existing.items()},
        "tree": {
            "c_files": len([m for m in counted if not m["data"]]),
            "data_files": len([m for m in modules if m["data"]]),
            **where,
            "padding_files": padding,
            "func_files": len(func),
        },
        "gates": {
            "dir_max_ok": where["dir_max"] <= settings.dir_max,
            "padding_zero": padding == 0,
            "func_files_zero": not func,
        },
        "estimate": {  # (before, after)
            "modules": (len(doc["modules"]), len(doc["modules"]) - len(touched) + len(modules)),
            "units_estimate": (len(doc["units"]), rest + len(counted)),
            "padding_units": (len(alone), padding),
            "single_atom_modules": (sizes.count(1), [len(m["atoms"]) for m in modules].count(1)),
        },
    }


def _data_guard(modules: list[dict[str, Any]], existing: Mapping[str, Json], atoms: Mapping[str, Json]) -> list[str]:
    """A function never moves into a DATA module: the padding there is ROM another version's layout needs. The functions
    a module would take from other files are dropped from it (a module left with no atom is dropped), and told."""
    refused: list[str] = []
    for m in list(modules):
        home = m["origin"] if m["keeps"] and m["origin"] in existing else m["name"]
        source = m.get("source") or existing.get(m["origin"], {}).get("source", "")
        if "DATA" in map(str.upper, PurePosixPath(source).parts):
            stay = [a for a in m["atoms"] if atoms[a]["kind"] == "function" and atoms[a]["module"] != home]
            refused += [f"{a} stays in {atoms[a]['module']}: {source} is a data module" for a in stay]
            m["atoms"] = [a for a in m["atoms"] if a not in stay]
            if not m["atoms"]:
                modules.remove(m)
    return refused


_LIBGCC = re.compile(r"__\w+[sdt]i[23]|__(?:fix|float)\w+")  # the compiler's runtime: __udivdi3, __fixunsdfdi


def sdk_names(root: Path, versions: Sequence[str]) -> dict[tuple[str, int], str]:
    """(version, vram) -> SDK name of each row of `versions/<v>/libultra_names.tsv` (vram, name, ...)."""
    found: dict[tuple[str, int], str] = {}
    for version in versions:
        table = root / "versions" / version / "libultra_names.tsv"
        for cells in (r.split("\t") for r in (table.read_text().splitlines()[1:] if table.is_file() else ())):
            try:
                found[(version, int(cells[0], 16))] = cells[1].strip()
            except (ValueError, IndexError):
                continue
    return found


def _sdk_name(atom: Json, sdk: Mapping[tuple[str, int], str]) -> str:
    """The SDK name a function atom's address has in a version that places it, "" for none."""
    if atom["kind"] != "function":
        return ""
    return next((n for v, p in sorted(atom["text"].items()) if (n := sdk.get((v, int(p["vram"]))))), "")


def _library_folder(named: Sequence[str], sources: Sequence[str], library: Sequence[str]) -> str:
    """Where a library object goes: the compiler runtime's names (libgcc) or the SDK's (libultra), into the folder
    of that library the project already has under a library root (src/sdk/libgultra, src/libgcc), else
    `<first library root>/libgcc` or `/libultra`."""
    gcc = all(_LIBGCC.fullmatch(n) for n in named)
    words = ("libgcc",) if gcc else ("libgultra", "libultra")
    roots = [r.rstrip("/") for r in library] or ["src/sdk"]
    have = sorted(
        {d for s in sources for d in [posixpath.dirname(s)] if _inside(s, roots) and posixpath.basename(d) in words},
        key=lambda d: (words.index(posixpath.basename(d)), d),
    )
    return have[0] if have else f"{roots[0]}/{words[-1]}"


def _sdk_header(source: str, header: str) -> str:
    """The header of a module moved into a library folder: include/ beside the source's folder, the same stem."""
    folder = posixpath.dirname(source).removeprefix("src/")
    stem = posixpath.splitext(posixpath.basename(source))[0]
    return f"include/{folder}/{stem}{posixpath.splitext(header)[1]}"


def _module_moves(entry: Json, existing: Json, current: Json) -> list[dict[str, Any]]:
    """A module that keeps its origin file renames it and takes in the atoms of other files; any other module is a
    new file every atom moves into."""
    name, origin, keeps = entry["name"], entry["origin"], entry["keeps"] and entry["origin"] in existing
    out = [{"op": "module", "subject": origin, "target": name}] * (keeps and origin != name)
    home = origin if keeps else name
    out += [{"op": "member", "subject": m, "target": name} for m in entry["atoms"] if current[m] != home]
    have = existing[origin] if keeps else {}
    for op in ("source", "header"):
        out += [{"op": op, "subject": name, "target": entry[op]}] * bool(entry[op] and have.get(op) != entry[op])
    return out


def _batches(proposal: Json) -> list[tuple[str, list[dict[str, Any]]]]:
    """One folder per batch, in the proposal's folder order."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in proposal["modules"]:
        by[entry["batch"]] += _module_moves(entry, proposal["existing"], proposal["current"])
    return [(s, by[s]) for s in proposal["order"] if by[s]]


# ------------------------------------------------------------------------------------------------ the patch

BEAT = 10.0  # seconds between the progress lines of a long step
_C = (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp")
_TEXT = (*_C, ".s", ".inc")
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
        lo, hi = head.start(), cparse.close(text, head.end() - 1) + 1
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


def _needed(source: str, body: str, have: str) -> str:
    """The file-level declarations of `source` (extern data, typedefs, prototypes) that `body` spells by name and `have`
    does not hold: a function moved into a file of its own keeps the externs the file it left declared."""
    words, held = set(re.findall(r"[A-Za-z_]\w*", body)), " ".join(have.split())
    out = ""
    for lo, hi, directive in cparse.spans(source):
        stmt = source[lo:hi].strip()
        name = re.search(r"(\w+)\s*\(", stmt) or re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*;$", stmt)
        wanted = stmt.startswith(("extern", "typedef")) or re.search(r"\)\s*;$", stmt)
        if not directive and wanted and name and name[1] in words and not re.search(r"[={]|^INCLUDE_", stmt):
            out += "" if " ".join(stmt.split()) in held else stmt + "\n"
    return out


_INCLUDE_SPELLING = re.compile(r'#[ \t]*include[ \t]*[<"]([^>"]*)[>"]')


class Files:
    """The project's source files as text, edited in memory; `diff()` is the git diff of every change."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.text: dict[str, str | None] = {}
        self.orig: dict[str, str | None] = {}
        self.origin: dict[str, str] = {}
        self._disk: set[str] | None = None  # the source and header files on disk, listed once: nothing here writes them
        self._spelled: dict[str, set[str]] | None = None  # include spelling -> the files that name it, once asked for
        self._names: dict[str, frozenset[str]] = {}  # file -> the spellings it names

    def get(self, path: str) -> str | None:
        if path not in self.text:
            file = self.root / path
            self.text[path] = self.orig[path] = file.read_text(encoding="utf-8") if file.is_file() else None
        return self.text[path]

    def put(self, path: str, text: str) -> None:
        self.get(path)
        self.text[path] = text
        self._reindex(path)

    def rename(self, old: str, new: str) -> None:
        text = self.get(old)
        if text is None or self.get(new) is not None:
            return
        self.text[new], self.text[old] = text, None
        self.origin[new] = self.origin.get(old, old)
        self._reindex(old)
        self._reindex(new)

    def _reindex(self, path: str) -> None:
        """Bring the include index up to date with the current text of `path`."""
        if self._spelled is None:
            return
        for spelling in self._names.pop(path, ()):
            self._spelled[spelling].discard(path)
        text = self.text.get(path)
        if text is not None:
            names = frozenset(_INCLUDE_SPELLING.findall(text))
            self._names[path] = names
            for spelling in names:
                self._spelled.setdefault(spelling, set()).add(path)

    def including(self, *spellings: str) -> list[str]:
        """The files, in path order, whose text names one of the include spellings. The first call reads every file
        once; after it only the files an edit touches are read again, so a rename costs its readers, not the tree."""
        if self._spelled is None:
            self._spelled = {}
            for path in self.everything():
                self.get(path)
                self._reindex(path)
        return sorted(set().union(*(self._spelled.get(s, set()) for s in spellings)))

    def everything(self) -> list[str]:
        """The source and header files under src/ and include/, by their current names."""
        if self._disk is None:
            self._disk = {
                p.relative_to(self.root).as_posix()
                for d in ("src", "include")
                for p in (self.root / d).rglob("*")
                if p.is_file() and p.suffix.lower() in _TEXT
            }
        gone = {p for p, t in self.text.items() if t is None}
        return sorted((self._disk | {p for p, t in self.text.items() if t is not None}) - gone)

    def replace(
        self,
        pattern: re.Pattern[str],
        new: str | Callable[[re.Match[str]], str],
        paths: Iterable[str],
        needle: str = "",
    ) -> int:
        """Substitute the pattern in each file; needle is a literal every match contains: a file without it is skipped
        by one substring search, not read through the regex (a rename over the whole tree was 12 s of regex time)."""
        count = 0
        for path in paths:
            text = self.get(path)
            if text is not None and needle in text:
                changed, n = pattern.subn(new, text)
                if n:
                    self.put(path, changed)
                    count += n
        return count

    def identifiers(
        self, old: str, new: str, paths: Iterable[str], local: str = "", comments: bool = False, line: int = 0
    ) -> int:
        """Rename C tokens, preserving string bytes and unrelated local scopes; with COMMENTS a whole-word mention in a
        comment too (a symbol's old name left in a banner or note is a stale reference: 10-25 CLEAN-PAK-2 fixed 24 files
        by hand). Comment mentions are not counted as uses."""
        count = 0
        token = re.compile(rf"\b{re.escape(old)}\b")
        for path in paths:
            text = self.get(path)
            if text is None or old not in text:  # no occurrence at all: neither a token nor an asm string
                continue
            span = (
                (repair.span_at(text, local, line) if line else repair.local_span(text, local))
                if local
                else (0, len(text))
            )
            if span is None:
                continue
            a, b = span
            masked = access.mask(text)
            hits = [
                m
                for m in token.finditer(masked, a, b)
                if not local or not re.search(r"(?:\.|->)\s*$", masked[a : m.start()])
            ]
            for hit in reversed(hits):
                text = text[: hit.start()] + new + text[hit.end() :]
            asm = noted = 0
            if not local:  # a name in an inline-assembly string (`jal old`) is a use too: left alone it breaks the link
                text, asm = rename_in_asm(text, token, new)
            if comments:
                text, noted = rename_in_comments(text, token, new)
            if hits or asm or noted:
                self.put(path, text)
                count += len(hits) + asm
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


def rename_in_comments(text: str, token: re.Pattern[str], new: str) -> tuple[str, int]:
    """TEXT with TOKEN renamed inside comments only (strings and code untouched); (new text, renames)."""
    hits = 0

    def one(m: re.Match[str]) -> str:
        nonlocal hits
        if not m[0].startswith(("/*", "//")):
            return m[0]
        body, n = token.subn(new, m[0])
        hits += n
        return body

    return access.SKIP.sub(one, text), hits


_ASM_STATEMENT = re.compile(r"\b(?:__asm__|__asm|asm)\b\s*(?:(?:volatile|__volatile__)\b\s*)?\(")


def rename_in_asm(text: str, token: re.Pattern[str], new: str) -> tuple[str, int]:
    """TEXT with TOKEN renamed inside every inline-assembly statement (its strings: `jal old`, `la $4, old`);
    (new text, renames). The rest of the file was renamed as C tokens, which leave string bytes alone."""
    out: list[str] = []
    at = hits = 0
    for found in _ASM_STATEMENT.finditer(text):
        if found.start() < at:
            continue
        i, depth = found.end(), 1
        while i < len(text) and depth:
            c = text[i]
            if c == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        body, n = token.subn(new, text[found.end() : i])
        out += [text[at : found.end()], body]
        at, hits = i, hits + n
    out.append(text[at:])
    return "".join(out), hits


def _similarity(old: str, new: str) -> int:
    """Percent of the two texts that match, by whole lines weighted by their length (git's own index is by chunks).
    Character matching is quadratic in the file size and took minutes on a project of headers."""
    if old == new:
        return 100
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    same = sum(
        sum(map(len, a[m.a : m.a + m.size]))
        for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    )
    return int(200 * same / (len(old) + len(new)))


def _file_diff(path: str, was: str, old: str | None, new: str | None) -> str:
    head = f"diff --git a/{was} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
    elif new is None:
        head += "deleted file mode 100644\n"
    elif was != path:
        head += f"similarity index {_similarity(old, new)}%\n"
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


def build(
    root: Path,
    moves: Sequence[Json],
    owner: Callable[[str], str | None],
    modules: Mapping[str, Json],
    note: Callable[[str], None] | None = None,
    evidenced: Collection[str] = (),
) -> Json:
    """A unified diff of the moves, as `{"patch", "files", "moves"}`. `owner` gives the file now holding a function
    (the tree's), `modules` the source and header path of each module the moves name, `evidenced` the original names
    the project's SDK tables declare (a reserved name among them is admitted).

    - member: the function's C body or INCLUDE_* line leaves its owner file for the module's file, made if absent;
    - symbol: its `versions/*/symbol_addrs.txt` lines and, word-bounded, every identifier in src/ and include/;
    - type: the identifier only;
    - module, source, header: the file renames, with the includes that name the old header;
    - file: the includes of a header move to the header it goes to;
    - field: a member of the one record body that spells it, and its `.` and `->` accesses.

    `note` hears a progress line at most every `BEAT` seconds."""
    files, applied, beat = Files(root), 0, time.monotonic()
    for move in moves:
        kind = "symbol" if move["op"] in ("symbol", "type", "field", "merge") else "file"
        name = (
            re.sub(r"^(?:struct|union|enum)\s+", "", move["target"])
            if move["op"] in ("merge", "type")
            else move["target"]
        )
        if name_why := api.name_issues(name, kind, implementation=True, evidenced=name in evidenced):
            raise state.stop("recipe.structure", name_why[0])
    member_words: dict[str, int] | None = None  # how many record bodies spell each word, read at the first field move
    types: access.Types | None = None  # the records and the data they type, read at the first shared member move
    skipped: list[Json] = []  # the moves that changed nothing, with the reason
    refused: set[tuple[str, str, str]] = set()  # the records (path, tag, alias) a member rename was refused on
    where: dict[str, str] = {}
    path = {n: m["source"] for n, m in modules.items()}
    header = {n: m["header"] for n, m in modules.items()}
    final = {m["subject"]: m["target"] for m in moves if m["op"] == "source"}
    born: dict[str, str] = {}  # a source file a member move creates -> the file its first member came from

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

        return files.replace(pattern, spell, files.including(old, old.removeprefix("include/")))

    for done, move in enumerate(moves):
        if note and time.monotonic() - beat >= BEAT:
            beat = time.monotonic()
            note(f"structure > build {done}/{len(moves)} moves")
        op, subject, target = move["op"], move["subject"], move["target"]
        if op in ("symbol", "type"):
            hits = 0
            if op == "symbol":
                found = sorted(
                    p for pattern in ("*/symbol_addrs.txt", "*/aliases.txt") for p in (root / "versions").glob(pattern)
                )
                line = re.compile(rf"(?m)^([ \t]*){re.escape(subject)}(?![\w])")
                hits += files.replace(line, r"\g<1>" + target, [p.relative_to(root).as_posix() for p in found], subject)
                # the literals name of a function follows its name wherever it is (its symbol_addrs.txt row,
                # aliases.txt, the splat.yaml piece, a source that declares it); a word match of the function skips it
                rows = [
                    p.relative_to(root).as_posix()
                    for v in ("symbol_addrs.txt", "aliases.txt", "splat.yaml")
                    for p in (root / "versions").glob(f"*/{v}")
                ]
                hits += files.replace(
                    _word(LITERALS + subject),
                    LITERALS + target,
                    [*sorted(rows), *files.everything()],
                    LITERALS + subject,
                )  # the needle keeps a rename from running the regex over every file of the tree
            hits += files.identifiers(subject, target, files.everything(), comments=True)
            if op == "symbol":
                hits += files.replace(_word(subject), target, PROSE, subject)  # a rule's prose names the function too
                hits += _asm_file(files, subject, target)
                hits += _derived(files, subject, target)
            applied += bool(hits)
            if subject in where:
                where[target] = where.pop(subject)
        elif op == "local":
            applied += bool(
                files.identifiers(subject, target, [move["path"]], move["function"], line=int(move.get("line") or 0))
            )
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
            if have is None and files.orig.get(dest) is None:  # a new piece: it is compiled as the file it leaves
                born.setdefault(dest, files.origin.get(src, src))
            need = _needed(text or "", taken[1], have or "")
            files.put(dest, (_preamble(text or "") + "\n" if have is None else have + "\n") + need + taken[1])
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
        elif op in ("field", "merge"):
            if member_words is None:
                member_words = _member_words(files)
            if types is None and (op == "merge" or member_words.get(subject, 0) > 1):
                types = _types(files)
            if op == "field":
                why = _field(files, move, member_words, types)
                refused |= {(move["path"], move.get("tag", ""), move.get("alias", ""))} if why else set()
            else:
                why = _merge(files, move, types, refused)
            applied += not why
            skipped += [{**move, "why": why}] if why else []
        elif op == "file":
            applied += bool(include(subject, target))
            if files.get(subject) is not None and files.get(target) is None:
                files.rename(subject, target)
    _tidy(files, {m["target"] for m in moves if m["op"] == "symbol"})
    _rekey(files)
    _inherit(files, born)
    changed = set(files.changed())
    if changed:
        before = {p: text for p in files.everything() if (text := files.orig.get(p)) is not None}
        after = {p: text for p in files.everything() if (text := files.get(p)) is not None}
        before.update({p: text for p, text in files.orig.items() if text is not None})
        census = root / ".unbake" / "recipe" / "naming-census.pkl"  # each file's census, kept between runs
        existing = {(f.path, f.reason) for f in api.naming_findings(before, census, evidenced)}
        added = [f for f in api.naming_findings(after, census, evidenced) if (f.path, f.reason) not in existing]
        if added:
            raise state.stop("recipe.structure", added[0].reason, path=added[0].path)
    if note:
        note(f"structure > build diff of {len(files.changed())} files")
    return {"patch": files.diff(), "files": files.changed(), "moves": applied, "skipped": skipped}


_RECORD_HEAD = re.compile(r"\b(?:struct|union)\s*(\w+)?\s*\{")


def bodies(text: str) -> Iterator[tuple[int, int, str, str]]:
    """(start, end, tag, typedef alias) of the inside of every record body a text defines."""
    for head in _RECORD_HEAD.finditer(text):
        end = cparse.close(text, head.end() - 1) + 1  # just past the closing brace
        alias = re.match(r"\s*(\w+)\s*;", text[end:])
        typedef = text[: head.start()].rstrip().endswith("typedef")
        yield head.end(), end - 1, head[1] or "", alias[1] if alias and typedef else ""


def _types(files: Files) -> access.Types:
    texts = {p: files.get(p) or "" for p in files.everything()}
    found = access.Types(texts)
    found.learn(texts)
    return found


def _spot(files: Files, path: str, tag: str, alias: str) -> tuple[int, int] | None:
    """The inside of the one record body of `path` that has this tag or typedef name."""
    text = files.get(path)
    wanted = [
        (a, b)
        for a, b, t, x in (bodies(text) if text is not None else ())
        if (tag and t == tag) or (alias and x == alias)
    ]
    return wanted[0] if len(wanted) == 1 else None


def _field(files: Files, move: Json, members: dict[str, int], types: access.Types | None) -> str:
    """A member rename: its declarator in the one record body of the project that has a member of that spelling
    (the record `tag` or typedef `alias` in `path`), and every `.old` and `->old` of the project. Another body that
    spells the member too: each access must have a base expression typed as this record or another (`access`); one
    that is not typed stops the move, unless the move says `loose`: the caller compiles afterwards and fixes the uses
    left (`settle`), so only the typed accesses of this record are renamed. Returns why nothing moved, empty when the
    member is renamed."""
    old, new, path = move["subject"], move["target"], move["path"]
    spot = _spot(files, path, move.get("tag", ""), move.get("alias", ""))
    if spot is None:
        return f"no one record body in {path} is {move.get('tag') or move.get('alias')}"
    shared = members.get(old, 0) > 1
    if members.get(old, 0) < 1 or (shared and types is None):
        return f"no record body spells {old}"
    key = ""
    if shared and types is not None:
        key = types.key.get(move.get("alias") or "") or types.key.get(move.get("tag") or "") or ""
        if (key, old) in types.twice:
            return f"more than one definition of {key} spells {old}"
        found = types.sites({p: files.get(p) or "" for p in files.everything() if p.lower().endswith(_C)}, [old])
        if (loose := [s for s in found if not s.owner]) and not move.get("loose"):
            return (
                f"{len(loose)} of {len(found)} accesses of {old} have a base of no known record type ({loose[0].path})"
            )
        by_file: dict[str, list[int]] = defaultdict(list)
        for site in found:
            if site.owner == key:
                by_file[site.path].append(site.at)
        for file, ats in by_file.items():
            text = files.get(file) or ""
            for at in sorted(ats, reverse=True):
                text = text[:at] + new + text[at + len(old) :]
            files.put(file, text)
        types.rename(key, old, new)
    text, spot = files.get(path) or "", _spot(files, path, move.get("tag", ""), move.get("alias", "")) or spot
    a, b = spot
    files.put(path, text[:a] + _word(old).sub(new, text[a:b]) + text[b:])
    members[old], members[new] = members[old] - shared, members.get(new, 0) + 1
    if not shared:
        access_ = re.compile(rf"(\.|->)(\s*){re.escape(old)}(?![\w])")
        files.replace(access_, lambda m: f"{m[1]}{m[2]}{new}", files.everything(), old)
        members[old] = 0
    return ""


def _merge(files: Files, move: Json, types: access.Types | None, refused: Collection[tuple[str, str, str]]) -> str:
    """A duplicate record folded into the one it equals: its definition and forward declarations go, every spelling of
    it becomes `target`, and its header includes the survivor's header. The member moves before it gave the duplicate
    the survivor's member names; one refused stops the merge. Returns why nothing moved."""
    tag, alias, path, into = move.get("tag", ""), move.get("alias", ""), move["path"], move["into_path"]
    dup, keep = _spot(files, path, tag, alias), _spot(files, into, move.get("into_tag", ""), move.get("into_alias", ""))
    if dup is None or keep is None:
        return f"the body of {alias or tag if dup is None else move['target']} is not found"
    if types is not None and types.defined[types.key.get(alias or tag, "")] > 1:
        return f"{alias or tag} is defined more than once"
    if (path, tag, alias) in refused:
        return f"a member of {alias or tag} could not take its original name"
    if path != into and not into.startswith("include/"):
        return f"{into} is not a header under include/"
    spelled = f"{into.removeprefix('include/')}"
    if path != into and re.search(
        rf'#[ \t]*include[ \t]*[<"]{re.escape(path.removeprefix("include/"))}[>"]', files.get(into) or ""
    ):
        return f"{into} includes {path}"
    text = files.get(path) or ""
    head = next(h for h in _RECORD_HEAD.finditer(text) if h.end() == dup[0])
    lo, hi = head.start(), dup[1] + 1
    before = text[:lo].rstrip()
    lo = len(before) - len("typedef") if before.endswith("typedef") else lo
    tail = re.match(r"\s*\w*\s*;[ \t]*\n?", text[hi:])
    hi += tail.end() if tail else 0
    note = (
        ""
        if path == into or re.search(rf'#[ \t]*include[ \t]*"{re.escape(spelled)}"', text)
        else f'#include "{spelled}"\n'
    )
    text = text[:lo] + text[hi:]
    if note:  # above every use, in the opening include block (the definition's old place can sit below a caller)
        at = _top_includes_end(text)
        text = text[:at] + note + text[at:]
    files.put(path, text)
    bare = {alias} - {""}
    for word in filter(None, (tag, alias)):
        one = re.escape(word)
        typedef = re.compile(rf"(?m)^[ \t]*typedef[ \t]+(?:struct|union)[ \t]+{one}[ \t]+(\w+)[ \t]*;\s*\n")
        bare |= {m[1] for p in files.everything() for m in typedef.finditer(files.get(p) or "")}
        forward = rf"(?m)^[ \t]*(?:struct|union)[ \t]+{one}[ \t]*;\s*\n"
        files.replace(re.compile(forward), "", files.everything())
        files.replace(typedef, "", files.everything())
        files.replace(re.compile(rf"\b(?:struct|union)\s+{one}\b"), move["target"], files.everything(), word)
    for word in bare - {move["target"]}:
        files.replace(_word(word), move["target"], files.everything(), word)
    if types is not None:
        types.key.update({w: types.key.get(move["target"].removeprefix("struct "), "") for w in (tag, alias) if w})
    return ""


def _top_includes_end(text: str) -> int:
    """Where a new `#include` goes: after the last include of the file's opening block (blank lines, comments and
    preprocessor lines before the first line of code), so it sits above every use. Never after an include found
    mid-file, below the functions that need what it declares (10-24 GOBJINST_5: the callers lost their prototype and
    took a float argument as a double). 0 when the block holds no include."""
    at = end = 0
    comment = False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if comment or s.startswith("/*"):
            comment = "*/" not in s
        elif s and not s.startswith(("//", "#")):
            break
        elif s.startswith("#") and re.match(r"#[ \t]*include\b", s):
            end = at + len(line)
        at += len(line)
    return end


def _tidy(files: Files, names: Collection[str]) -> None:
    """After a rename: the `#define NAME fz_OLD` shims a fuzzy file kept go (the name is real now), together with the
    `#undef NAME` lines of the same file, so a NON_MATCHING block and its twin are treated alike (MENU_2 80408CB0_28:
    the define went, the renamed #undef stayed). A rename never changes how a unit declares a symbol: local `extern`s
    stay (swapping them for header includes broke 27 units, 10-24 REV1-ONE-27); typing them is the data gate's job."""
    if not names:
        return
    alt = "|".join(map(re.escape, names))
    shim = re.compile(
        rf"(?m)^[ \t]*#[ \t]*define[ \t]+(?:{alt})[ \t]+fz_?\w+[ \t]*\n"
    )  # the alias a rename derived (fz_OLD) may already read fzNew
    undef = re.compile(rf"(?m)^[ \t]*#[ \t]*undef[ \t]+(?:{alt})[ \t]*\n")
    for path in (p for p in files.everything() if p.lower().endswith(_C)):
        text = files.get(path) or ""
        if shim.search(text):
            files.put(path, undef.sub("", shim.sub("", text)))


def _member_words(files: Files) -> dict[str, int]:
    """Per word, how many record bodies of the project spell it."""
    out: dict[str, int] = defaultdict(int)
    for path in files.everything():
        text = files.get(path) or ""
        for a, b, _, _ in bodies(text):
            for word in set(re.findall(r"[A-Za-z_]\w*", text[a:b])):
                out[word] += 1
    return out


_FILE_ROW = re.compile(r"""(?m)^([ \t]*\[[ \t]*file[ \t]*\.[ \t]*)(["'])([^"'\n]+)\2([ \t]*\])""")


_NEUTRAL_FN = re.compile(r"func_[0-9A-F]{8}(?:_[a-z]+(?:_[a-z0-9]+)?)?\Z")


def _derived(files: Files, subject: str, target: str) -> int:
    """The names a tool derived from a neutral function name embed it whole: `Player_func_80209308_de` (a type the
    function's record is named for) and `func_8020A028_S4` follow the function to its new name, so no type keeps the
    address form after the function is named. Only a neutral address name has derived names; a word match of any other
    name (`Foo` inside `Bar_Foo`) is another identifier."""
    if not _NEUTRAL_FN.match(subject):
        return 0
    word, hits = re.escape(subject), 0
    before = re.compile(rf"(?<![A-Za-z0-9_$])([A-Za-z][A-Za-z0-9]*)_{word}(?![A-Za-z0-9_$])")
    after = re.compile(rf"(?<![A-Za-z0-9_$]){word}_([A-Z][A-Za-z0-9_]*)(?![A-Za-z0-9$])")
    paths = files.everything()
    joined = target.replace(
        "_", ""
    )  # a type name has no underscore (style type-name): Player_Brain_Think -> PlayerBrainThink
    hits += files.replace(before, lambda m: f"{m[1]}{joined}", paths, subject)
    hits += files.replace(after, lambda m: f"{joined}{m[1].replace('_', '')}", paths, subject)
    return hits


def _asm_file(files: Files, subject: str, target: str) -> int:
    """A handwritten assembly file named for the function it holds (src/sdk/.../func_802BCF30_de.s) follows the
    function's new name, with the entries that key on its path: the linker script and the text sizes name it."""
    moved = [
        p for p in files.everything() if PurePosixPath(p).suffix in (".s", ".S") and PurePosixPath(p).stem == subject
    ]
    for old in moved:
        new = str(PurePosixPath(old).with_name(target + PurePosixPath(old).suffix))
        files.rename(old, new)
        keyed = [
            p.relative_to(files.root).as_posix()
            for pattern in ("*.ld", "versions/*/text_sizes.mk")
            for p in files.root.glob(pattern)
        ]
        files.replace(re.compile(re.escape(old)), new, keyed, old)
    return len(moved)


_FOLDER_ROW = re.compile(r"""(?m)^([ \t]*\[[ \t]*(?:dir|library)[ \t]*\.[ \t]*)(["'])([^"'\n]+/)\2([ \t]*\])""")


def _folders(files: Files, moved: Mapping[str, str]) -> dict[str, str]:
    """Folder -> new folder (both with a trailing slash) for each folder whose files all moved to one other folder."""
    target: dict[str, set[str]] = {}
    for was, now in moved.items():
        target.setdefault(str(PurePosixPath(was).parent) + "/", set()).add(str(PurePosixPath(now).parent) + "/")
    kept = {str(PurePosixPath(p).parent) + "/" for p in files.everything() if p not in moved.values()}
    return {old: next(iter(new)) for old, new in target.items() if len(new) == 1 and old not in kept}


def _rekey(files: Files) -> None:
    """The per-file rules of config.toml (`[file."src/a.c"]`: toolchain, flags) follow the files the moves rename: a
    rule left on the old path matches nothing, and the file silently builds with the project's defaults."""
    moved = {was: now for now, was in files.origin.items() if files.text.get(now) is not None and was != now}
    if moved:
        files.replace(_FILE_ROW, lambda m: f"{m[1]}{m[2]}{moved.get(m[3], m[3])}{m[2]}{m[4]}", ["config.toml"])
        folders = _folders(files, moved)
        if folders:  # the [dir."src/x/"] and [library."src/x/"] rows follow a folder every file of which moved together
            files.replace(_FOLDER_ROW, lambda m: f"{m[1]}{m[2]}{folders.get(m[3], m[3])}{m[2]}{m[4]}", ["config.toml"])


def _inherit(files: Files, born: Mapping[str, str]) -> None:
    """A file a split creates is compiled as the file its members come from: that file's `[file."..."]` rule of
    config.toml (toolchain, flags) is copied to it, else the piece silently builds with the project's defaults
    (RW src/IO/802013D8_2.CPP took the default compiler while 802013D8.CPP is KMC)."""
    text = files.get("config.toml")
    if not text or not born:
        return
    blocks = {m[3]: m for m in _FILE_ROW.finditer(text)}
    added = []
    for new, was in sorted(born.items()):
        if new in blocks or was not in blocks or files.get(new) is None:
            continue
        start = blocks[was].start()
        later = [m.start() for m in _FILE_ROW.finditer(text) if m.start() > start] + [len(text)]
        rule = text[start : later[0]].rstrip("\n")
        added.append(
            rule.replace(blocks[was][0], f"{blocks[was][1]}{blocks[was][2]}{new}{blocks[was][2]}{blocks[was][4]}", 1)
        )
    if added:
        files.put("config.toml", text.rstrip("\n") + "\n\n" + "\n\n".join(added) + "\n")


def ops(proposal: Json) -> list[Json]:
    """The flat move list of a proposal: one folder after the other."""
    return [m for _, group in _batches(proposal) for m in group]


def _rel(root: Path, path: str) -> str:
    """A scope path as the project-relative POSIX spelling the map uses."""
    given = Path(path)
    return posixpath.normpath((given.relative_to(root) if given.is_absolute() else given).as_posix())


def _links(proposal: Json, entry: Json) -> set[str]:
    """The modules one proposed module ties together: its origin, its name and the holder of each of its atoms."""
    return {entry["origin"], entry["name"], *(proposal["current"].get(a, entry["origin"]) for a in entry["atoms"])}


def scope_modules(doc: Json, proposal: Json, files: Collection[str]) -> frozenset[str]:
    """The closure of a file scope: the modules holding the files (as source, header or compiled unit), plus every
    module the proposal links to them. Applied until nothing more joins, so a contiguous block is whole."""
    held = set(files)
    seed = {m["name"] for m in doc["modules"] if held & {m["source"], m["header"]}}
    seed |= {u["module"] for u in doc["units"] if u["path"] in held and u.get("module")}
    if not seed:
        raise Refusal(Finding("recipe.scope", f"no module holds {', '.join(sorted(held))}"))
    links = [_links(proposal, e) for e in proposal["modules"]]
    closure = set(seed)
    while grown := [x for x in links if x & closure and not x <= closure]:
        for x in grown:
            closure |= x
    return frozenset(closure)


def scoped(proposal: Json, closure: Collection[str]) -> Json:
    """The proposal limited to the closure: only the proposed modules linked to it."""
    return {**proposal, "modules": [e for e in proposal["modules"] if _links(proposal, e) & set(closure)]}


def confine(proposal: Json, closure: Collection[str]) -> None:
    """Refuse a move that touches a file outside the closure, naming the move and the file."""
    inside = set(closure)
    current, existing = proposal["current"], proposal["existing"]
    made = {e["name"]: e for e in proposal["modules"]}

    def file_of(module: str) -> str:
        have = existing.get(module) or made.get(module) or {}
        return str(have.get("source") or module)

    for move in ops(proposal):
        op, subject, target = move["op"], move["subject"], move["target"]
        touched = [current.get(subject, ""), target] if op == "member" else [subject]
        for module in touched:
            if module not in inside:
                raise Refusal(
                    Finding("recipe.scope", f"{op} {subject} -> {target} touches {file_of(module)}, outside the scope")
                )


def modules_of(doc: Json) -> dict[str, Json]:
    return {m["name"]: {"source": m["source"], "header": m["header"]} for m in doc["modules"]}


def save(config: Config, built: Json) -> str:
    """The patch as the next `patch-N.diff` under `.unbake/recipe/patches/`; its path relative to the project."""
    root = config.project.root / ".unbake/recipe/patches"
    found = [int(m[1]) for p in root.glob("patch-*.*") if (m := re.match(r"patch-(\d+)\.", p.name))]
    path = root / f"patch-{max(found, default=0) + 1}.diff"
    write(path, built["patch"])
    return str(path.relative_to(config.project.root))


def _saved(config: Config, built: Json) -> str:
    with narrated("save"):
        return save(config, built)


def stream(built: Json) -> str:
    """What the verb prints on stdout: the patch, the files it touches and the count of moves."""
    return json.dumps({k: built[k] for k in ("patch", "files", "moves")}) + "\n"


def run(config: Config, params: Json) -> Json:
    """`recipe structure`: write the proposal as a patch under `.unbake/recipe/patches/` and print it as JSON."""
    root = config.project.root
    if params.get("map"):  # T7: a module map (MODULES.tsv) applied as one patch
        from recipemaker import modmap

        with narrated("map"):
            built = modmap.run(config, params)
        kept = bool(built["moves"])
        return {
            **built,
            "path": _saved(config, built) if kept else "",
            "stream": stream(built),
            "next": "recipe apply" if kept else "recipe structure --map",
        }
    document = evidence(config, params["host"])
    paths = [_rel(root, p) for p in params.get("paths") or ()]
    library = getattr(config.project, "libraries", ())
    settings, types, naming = state.structure(), clash.reader(root), tree.style(root)
    catalog = debug.load(config)  # the original source file of each function its debug record places: a boundary
    rows = debug.boundaries(catalog, document) if catalog is not None else []
    with narrated("propose"):
        sdk = sdk_names(root, document["versions"])
        proposal = dict(propose(document, settings, params["segment"], library, types, rows, naming, sdk))
    found: list[str] = []
    if paths:  # a scope is a suggestion that never blocks: a refusal is a finding and the proposal is empty
        with narrated("scope"):
            try:
                closure = scope_modules(document, proposal, paths)
                proposal = dict(scoped(proposal, closure))
                confine(proposal, closure)
                held = {x for m in document["modules"] for x in (m["source"], m["header"])}
                found = [
                    f"no module holds {p}" for p in paths if p not in held | {u["path"] for u in document["units"]}
                ]
            except Refusal as refusal:
                found = [f.reason for f in refusal.findings]
                proposal = {**proposal, "modules": []}
    captured = api.capture(config)
    with narrated("build"):
        built = build(
            root,
            ops(proposal),
            lambda n: captured.owner(document["names_from"], n),
            modules_of(document),
            say,
            frozenset(sdk.values()),
        )
    kept = bool(built["moves"]) or not paths  # a scoped proposal with nothing to move saves no patch
    return {
        **{k: dict(zip(("before", "after"), v, strict=True)) for k, v in proposal["estimate"].items()},
        "tree": proposal["tree"],
        "gates": proposal["gates"],
        "boundaries": proposal["boundaries"],
        **{k: built[k] for k in ("patch", "files", "moves")},
        "path": _saved(config, built) if kept else "",
        "stream": stream(built),
        **({"suggestions": ops(proposal), "findings": found} if paths else {}),
        "next": "recipe apply" if built["moves"] else "recipe name next",
    }


def _made_paths(patch: str) -> list[str]:
    """The paths a git diff creates: new files, rename targets and copy targets."""
    made: list[str] = []
    lines = patch.splitlines()
    for number, line in enumerate(lines):
        for prefix in ("rename to ", "copy to "):
            if line.startswith(prefix):
                made.append(line[len(prefix) :])
        if line == "--- /dev/null" and number + 1 < len(lines) and lines[number + 1].startswith("+++ b/"):
            made.append(lines[number + 1][6:])
    return list(dict.fromkeys(made))


def _portable_patch(root: Path, patch: str) -> None:
    """Read the result of a unified text patch before git mutates the project."""
    files = Files(root)
    before = {p: text for p in files.everything() if (text := files.get(p)) is not None}
    after = dict(before)
    blocks = (
        re.split(r"(?=^diff --git )", patch, flags=re.M)
        if "diff --git " in patch
        else re.split(r"(?=^--- )", patch, flags=re.M)
    )
    for block in blocks:
        if not block.strip():
            continue
        old = re.search(r"^--- (?:a/)?([^\n\t]+)", block, re.M)
        new = re.search(r"^\+\+\+ (?:b/)?([^\n\t]+)", block, re.M)
        rename = re.search(r"^rename from ([^\n]+)\nrename to ([^\n]+)", block, re.M)
        if not (old and new) and not rename:
            continue  # no source text changed (e.g. a mode-only patch)
        src, dst = (old[1], new[1]) if old and new else (rename[1], rename[2]) if rename else ("", "")
        for path in (src, dst):
            if path != "/dev/null" and (why := api.name_issues(path, "file")):
                raise state.stop("recipe.apply", why[0])
        if not any(PurePosixPath(p).suffix.lower() in _TEXT for p in (src, dst)):
            continue
        original = before.get(src, "").splitlines(keepends=True)
        result: list[str] = []
        cursor = 0
        hunks = list(re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@[^\n]*\n", block, re.M))
        for index, hunk in enumerate(hunks):
            start = int(hunk[1]) - (hunk[2] != "0")
            result.extend(original[cursor:start])
            cursor = start
            end = hunks[index + 1].start() if index + 1 < len(hunks) else len(block)
            for line in block[hunk.end() : end].splitlines(keepends=True):
                if line.startswith((" ", "-")):
                    if cursor >= len(original) or original[cursor].rstrip("\n") != line[1:].rstrip("\n"):
                        raise state.stop(
                            "recipe.apply", "patch context moved; regenerate the patch before naming admission"
                        )
                    cursor += 1
                if line.startswith((" ", "+")):
                    result.append(line[1:])
                elif line.startswith("\\ No newline") and result:
                    result[-1] = result[-1].removesuffix("\n")
        result.extend(original[cursor:])
        after.pop(src, None)
        if dst != "/dev/null":
            after[dst] = "".join(result)
    census = root / ".unbake" / "recipe" / "naming-census.pkl"
    known = api.naming_findings(before, census)
    held = {(f.path, f.reason) for f in known}
    named = {f.reason for f in known}  # a name the tree already spells somewhere moves with its text (a merged file)
    added = [f for f in api.naming_findings(after, census) if (f.path, f.reason) not in held and f.reason not in named]
    if added:
        raise state.stop("recipe.apply", added[0].reason, path=added[0].path)


def apply(config: Config, params: Json) -> Json:
    """`recipe apply`: `git apply` the newest saved patch that applies (or PATCH), its new files intent-to-add, then
    `unbake sync`; sync's result comes back.
    The one explicit operator action that writes."""
    root = config.project.root
    saved = sorted(
        (root / ".unbake/recipe/patches").glob("patch-*.diff"),
        key=lambda p: int("".join(c for c in p.name if c.isdigit())),
    )
    patch = Path(params["patch"]) if params.get("patch") else None
    # the newest saved patch is the one the caller just made (an older one was made over an older tree or name table)
    for candidate in [] if patch else reversed(saved):  # one saved before the tree changed no longer applies: set aside
        if not recipe_host.run(
            ["git", "-C", str(root), "apply", "--check", str(candidate.resolve())], root, config
        ).exit:
            patch = candidate
            break
        candidate.rename(candidate.with_suffix(".stale"))
        saved = [p for p in saved if p != candidate]
    if patch is None or not patch.is_file():
        raise state.stop("recipe.apply", "no saved patch applies to the tree", action="recipe structure")
    # the patch edits the work tree only: `git apply --intent-to-add` without `--index` replaces the whole index with
    # the new files alone (10-20: ~6,420 entries lost per apply), so the index stays as it was and the files the patch
    # makes are then marked intent-to-add one by one
    text = patch.read_text(errors="replace")
    _portable_patch(root, text)
    with _atomic(config, root, patch, params["host"]) as mark:
        return _apply_patch(config, params, root, patch, text, saved, mark)


class _Stopped(BaseException):
    """A termination signal that arrived while a patch was half applied (raised in the main thread by `_atomic`)."""


@contextmanager
def _atomic(config: Config, root: Path, patch: Path, host: Any) -> Iterator[Callable[[], None]]:
    """The patch and the `unbake sync` after it land together or not at all: a SIGTERM/SIGINT/SIGHUP between the
    first edit and the end of the sync (a lane's `timeout`) puts the work tree back (`git apply -R`, the patch file
    restored, one more sync) and exits 143, instead of leaving renamed sources beside stale generated rows
    (10-25: CLEAN-CAMERA-1 got source renames without the docs/map rows, and renameproof refused the pack)."""
    state_: dict[str, bool] = {"edited": False}

    def stop(signum: int, frame: Any) -> None:
        raise _Stopped(signum)

    old: dict[int, Any] = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old[sig] = signal.signal(sig, stop)
    except ValueError:  # not the main thread: no handler, the apply is as it was
        old = {}
    try:
        yield lambda: state_.__setitem__("edited", True)
    except _Stopped as stopped:
        for sig in old:
            signal.signal(sig, signal.SIG_IGN)  # the rollback finishes whatever else arrives
        if state_["edited"]:
            sys.stderr.write("recipe apply: interrupted, putting the tree back (git apply -R, sync)\n")
            with contextlib.suppress(Exception):
                recipe_host.run(["git", "-C", str(root), "apply", "-R", str(_restored(patch).resolve())], root, config)
                with contextlib.suppress(OSError):
                    if patch.with_suffix(".applied").exists():
                        patch.with_suffix(".applied").rename(patch)
                _unbake(config, host, "sync")
        raise SystemExit(128 + int(stopped.args[0])) from None
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _restored(patch: Path) -> Path:
    """The patch file wherever the interrupted apply left it (renamed to .applied once edits landed)."""
    return patch if patch.exists() else patch.with_suffix(".applied")


def _apply_patch(
    config: Config, params: Json, root: Path, patch: Path, text: str, saved: list[Path], mark: Callable[[], None]
) -> Json:
    sys.stderr.write(f"recipe apply: {patch.name}, then unbake sync (~30 s; stop it and the tree is put back)\n")
    ran = recipe_host.run(["git", "-C", str(root), "apply", str(patch.resolve())], root, config)
    mark()
    if ran.exit:
        tail = bytes(ran.stderr).decode(errors="replace").strip().splitlines()[-1:] or ["git apply failed"]
        raise state.stop("recipe.apply", f"git apply refused {patch.name}: {tail[0]}", action="recipe structure")
    made = _made_paths(text)
    if made:
        # a file the patch makes (a module renamed or split off) is intent-to-add: sync and the project's [file] rows
        # read tracked files, and the tree must sync as the patch leaves it
        marked = recipe_host.run(["git", "-C", str(root), "add", "--intent-to-add", "--", *made], root, config)
        if marked.exit:
            tail = bytes(marked.stderr).decode(errors="replace").strip().splitlines()[-1:] or ["git add failed"]
            raise state.stop(
                "recipe.apply", f"git add -N refused after {patch.name}: {tail[0]}", action="recipe name next"
            )
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
