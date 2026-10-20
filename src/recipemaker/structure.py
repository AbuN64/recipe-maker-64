"""Module structure as a file patch: evidence from unbake's public surface, a proposal from it, one unified diff that
moves functions between files, renames files and edits includes. `recipe apply` applies it, then runs sync.

The evidence is the slim `unbake map` (module paths, segment ownership, stand-ins, fuzzy names) with the placements of
the captured tree's symbol tables. It carries no call, load or object-edge facts and no header graph, so nothing here
proposes from them: a module is cut only where an evidence row or the size cap says, keeps its origin's folder, and no
header is merged or deleted."""

from __future__ import annotations

import difflib
import json
import os
import posixpath
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal, file_facts

from recipemaker import clash, state, tree, world
from recipemaker import host as recipe_host

Segments = str | Collection[str] | None
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


def evidence(config: Config, host: Path | None = None) -> Json:
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
    ends = {v: world.extents(t) for v, t in tables.items()}
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
) -> Json:
    """Propose one module per object, named by its address, in the folder of the file it comes from.
    Scope is all segments or the requested segment names; `types` splits an object whose C files define a type
    differently."""
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
    for a in sorted(free, key=lambda a: (a["segment"], *_place(a, first), a["name"])):
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
) -> Json:
    """A unified diff of the moves, as `{"patch", "files", "moves"}`. `owner` gives the file now holding a function
    (the tree's), `modules` the source and header path of each module the moves name.

    - member: the function's C body or INCLUDE_* line leaves its owner file for the module's file, made if absent;
    - symbol: its `versions/*/symbol_addrs.txt` lines and, word-bounded, every identifier in src/ and include/;
    - type: the identifier only;
    - module, source, header: the file renames, with the includes that name the old header;
    - file: the includes of a header move to the header it goes to.

    `note` hears a progress line at most every `BEAT` seconds."""
    files, applied, beat = Files(root), 0, time.monotonic()
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

        return files.replace(pattern, spell, files.including(old, old.removeprefix("include/")))

    for done, move in enumerate(moves):
        if note and time.monotonic() - beat >= BEAT:
            beat = time.monotonic()
            note(f"structure > build {done}/{len(moves)} moves")
        op, subject, target = move["op"], move["subject"], move["target"]
        if op in ("symbol", "type"):
            hits = 0
            if op == "symbol":
                found = sorted((root / "versions").glob("*/symbol_addrs.txt"))
                line = re.compile(rf"(?m)^([ \t]*){re.escape(subject)}(?![\w])")
                hits += files.replace(line, r"\g<1>" + target, [p.relative_to(root).as_posix() for p in found])
                # the literals row of a function (aliases.txt, splat.yaml) follows its name; a word match skips it
                rows = [
                    p.relative_to(root).as_posix()
                    for v in ("aliases.txt", "splat.yaml")
                    for p in (root / "versions").glob(f"*/{v}")
                ]
                hits += files.replace(_word(LITERALS + subject), LITERALS + target, sorted(rows))
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
    if note:
        note(f"structure > build diff of {len(files.changed())} files")
    return {"patch": files.diff(), "files": files.changed(), "moves": applied}


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
    document = evidence(config, params["host"])
    paths = [_rel(root, p) for p in params.get("paths") or ()]
    library = getattr(config.project, "libraries", ())
    settings, types, naming = state.structure(), clash.reader(root), tree.style(root)
    with narrated("propose"):
        proposal = dict(propose(document, settings, params["segment"], library, types, (), naming))
    found: list[str] = []
    if paths:  # a scope is a suggestion that never blocks: a refusal is a finding and the proposal is empty
        with narrated("scope"):
            try:
                closure = scope_modules(document, proposal, paths)
                proposal = dict(scoped(proposal, closure))
                confine(proposal, closure)
            except Refusal as refusal:
                found = [f.reason for f in refusal.findings]
                proposal = {**proposal, "modules": []}
    captured = api.capture(config)
    with narrated("build"):
        built = build(
            root, ops(proposal), lambda n: captured.owner(document["names_from"], n), modules_of(document), say
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
