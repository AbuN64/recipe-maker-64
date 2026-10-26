"""`recipe name`: evidence packs for an outside naming pass, then rows validated against the style and the evidence,
turned into one file patch (symbol, module, source and header renames) that `decomp ship --patch` lands. Tag-named
subsystems (N2) need no model. No name is accepted without evidence a reader can check.

The evidence is the structure document (`structure.evidence`: modules, segments, members, placements, units) plus the
calls and loads of the extracted assembly of each member's version (`api.asm_path`). Unbake produces no string,
catalog-name or header-graph facts, so there is no catalog step (N0), no string evidence and no type renames."""

import json
import re
import sys
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Json, Refusal

from recipemaker import (
    access,
    autoname,
    cparse,
    debug,
    host,
    locals_,
    namebook,
    prefixes,
    repair,
    settle,
    slicepack,
    slices,
    state,
    structure,
    style,
    table,
    tree,
)
from recipemaker import (
    members as members_,
)

# Compiler builtins: cc1 expands a call by one of these names inline (+11 words for sqrtf, func_802B72B0_de),
# so a game function carrying one no longer compiles to its own body. The SDK table line may still spell one.
_BUILTIN_WORDS = (
    "abs labs llabs fabs fabsf fabsl sqrt sqrtf sqrtl sin sinf sinl cos cosf cosl tan tanf atan atanf atan2 atan2f",
    "asin asinf acos acosf exp expf log logf log10 pow powf floor floorf ceil ceilf fmod fmodf memcpy memmove",
    "memset memcmp memchr strcpy strncpy strcat strncat strcmp strncmp strlen strchr strrchr strstr strpbrk strspn",
    "strcspn bcopy bzero bcmp alloca ffs index rindex malloc calloc free printf sprintf puts putchar",
)
BUILTINS = frozenset(w for line in _BUILTIN_WORDS for w in line.split())
_ORDER = {"subsystem": 0, "module": 1, "function": 2, "global": 2, "static": 2}
# style.toml evidence_forms; whatever follows the name (a parenthesized note, nested parentheses, `*`, `=`, a `:
# reason`)
# is the reader's, the name alone is checked
_EVIDENCE = re.compile(r"^(callee|caller|reads|tag|debug) ([^\s():;,]+)(?:[:;,]?(?:\s+.*)?)$")
# `jal NAME` and a tail `j NAME`; a local label starts with a dot, a register with a dollar sign
_CALL = re.compile(r"\*/\s+j(?:al)?\s+([A-Za-z_]\w*)")
_LOAD = re.compile(r"%(?:hi|lo)\(([A-Za-z_]\w*)")
_SOURCES = ("*.H", "*.h", "*.CPP", "*.cpp", "*.c")
_CARD_LIMIT = 12
_SHIP = "decomp ship --patch LANE={}:MESSAGE"  # a naming patch lands as any change to the shared tree does
Graph = dict[str, tuple[list[str], list[str]]]


def _root(config: Config) -> Path:
    return config.project.root / ".unbake/recipe/name"


def _bytes(member: Json, version: str) -> int:
    return next((p["rom_end"] - p["rom_start"] for p in member["placements"] if p["version"] == version), 0)


def _directory(config: Config, version: str, names: Iterable[str]) -> str:
    """The folder of a segment's extracted assembly, from the first member unbake can place; "" when none can."""
    for name in names:
        try:
            return str(PurePosixPath(api.asm_path(config, version, name)).parent)
        except Refusal:
            continue
    return ""


def _tables(config: Config, versions: Sequence[str]) -> dict[str, Any]:
    """Per version the symbol table (name -> (vram, rom, size, kind)) a spelling is resolved through."""
    return {v: api.symbols(config, v) for v in versions}


def _within(spans: Sequence[tuple[int, int, str]], rom: int | None) -> str:
    """The data member whose ROM span holds `rom`: a symbol inside a datum (a halfword of a table) spells that datum."""
    if not rom:
        return ""
    i = bisect_right(spans, (rom, sys.maxsize, "")) - 1
    return spans[i][2] if i >= 0 and rom < spans[i][1] else ""


def _stems(member: Json, version: str) -> list[str]:
    """The file stems a function's extracted assembly can have: its name, and the neutral name extraction gave it
    (func_<VRAM>_<version>, func_<VRAM>) when it was renamed after the assembly was extracted."""
    vram = next((p["vram"] for p in member["placements"] if p["version"] == version), None)
    neutral = [f"func_{vram:08X}_{version.replace('-', '_')}", f"func_{vram:08X}"] if vram is not None else []
    return [member["name"], *neutral]


def reads(config: Config, doc: Json) -> Graph:
    """Per function with extracted assembly the members it calls and the symbols whose address it loads (a member or
    not: `reads NAME` evidence cites any of them), in order of first use, over the assembly of every version that
    places it: a call only one version makes (a us-only callee) is still a call. Each version's names are its own
    (`func_X_us`); `Map.canon` folds them onto the members. A function without assembly has no entry."""
    root = config.project.root
    by_segment: dict[tuple[str, str], list[str]] = {}
    by_name = {m["name"]: m for m in doc["members"]}
    for m in doc["members"]:
        for version in dict.fromkeys(p["version"] for p in m["placements"]) if m["kind"] == "function" else ():
            by_segment.setdefault((version, m["segment"]), []).append(m["name"])
    graph: Graph = {}
    for (version, _), names in by_segment.items():
        folder = _directory(config, version, names)
        for name in names if folder else ():
            file = next(
                (f for f in (root / folder / f"{n}.s" for n in _stems(by_name[name], version)) if f.is_file()), None
            )
            if file is None:
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            # a callee no member is named as (stale assembly of a renamed function) is resolved by `Map.canon`
            calls = [x for x in dict.fromkeys(_CALL.findall(text)) if not x.startswith(".")]  # a recursive call stays
            loads = [x for x in dict.fromkeys(_LOAD.findall(text)) if x != name]  # data no module holds is still read
            held = graph.get(name, ([], []))
            graph[name] = (list(dict.fromkeys([*held[0], *calls])), list(dict.fromkeys([*held[1], *loads])))
    if not graph and by_segment:
        raise state.stop(
            "recipe.name",
            "no extracted assembly under this tree to read calls from (a fresh git worktree has none)",
            action="run `unbake setup --no-make` in this tree (extracts the base ROMs), then submit again",
        )
    return graph


class Map:
    """The evidence as the name rules read it: members, modules, folders, calls and callers by current name."""

    def __init__(self, config: Config, doc: Json, graph: Graph) -> None:
        self.root, self.doc = config.project.root, doc
        self.members: dict[str, Json] = {m["name"]: m for m in doc["members"]}
        self.modules: dict[str, dict[str, Any]] = {m["name"]: dict(m) for m in doc["modules"]}
        self.config = config
        self.prefixes = prefixes.table(config)
        self.tables = _tables(config, doc["versions"])
        self.catalog = debug.load(config)  # the original debug records, None when the config names no catalog
        self._spelled: dict[str, str] | None = None  # a spelling to its member, built at the first miss (`canon`)
        self._by_vram: dict[tuple[str, int, str], set[str]] | None = None  # (version, vram, kind) to table spellings
        self._vram: dict[tuple[str, int], set[str]] = {}  # (version, VRAM) to the members placed there
        self._externs: set[str] | None = None
        self._extern_members: set[str] | None = None  # the members some extern declaration spells under any alias
        self._strings: slicepack.Strings | None = (
            None  # the strings the data holds (`string` evidence), built at first use
        )
        self._source: slicepack.Source | None = None
        self._at: dict[str, set[tuple[str, int]]] | None = None  # a spelling to its (version, VRAM) in the tables
        self._sdk: dict[tuple[str, int], str] | None = None  # (version, VRAM) to its SDK name (`sdk_name`)
        self.calls = {
            n: list(
                dict.fromkeys(
                    x for x in map(self.canon, c) if (x in self.members or self.symbol_kind(x) == "function") and x != n
                )
            )
            for n, (c, _) in graph.items()
        }
        self.loads = {n: list(dict.fromkeys(map(self.canon, ld))) for n, (_, ld) in graph.items()}
        # a function that calls itself: its own name is evidence (callee/caller) though no edge of the graph
        self.recursive = {n for n, (c, _) in graph.items() if any(self.canon(x) == n for x in c)}
        self.callers: dict[str, list[str]] = {}
        for name in graph:
            for target in (*self.calls[name], *self.loads[name]):
                self.callers.setdefault(target, []).append(name)
        self._gbi: dict[str, bool] = {}
        self.alias: dict[str, str] = {}  # a module renamed by an accepted row: old name to new name
        self.taken: set[str] = {*self.members, *self.modules, *(n for t in self.tables.values() for n in t)}
        self._naming: tree.Naming | None = None
        self._prefix_words: set[str] | None = None  # every prefix this project spells (`reprefix`), built at first use
        for module in self.modules.values():
            self.taken |= {PurePosixPath(module[k]).stem for k in ("source", "header")} | {self.folder(module["name"])}

    def spell(self, folder: str) -> str:
        """A subsystem name as a folder of this project spells it (FRONTEND beside CODE_*.CPP stems)."""
        if self._naming is None:
            self._naming = tree.style(self.root)
        return self._naming.cased(folder)

    def folder_taken(self, word: str) -> bool:
        """Whether a folder already holds the subsystem `word`, in whatever case it spells it (COMBAT holds combat)."""
        return any(self.folder(m).lower() == word.lower() for m in self.modules) or self.spell(word) in self.taken

    def merge_folder(self, target: str, name: str) -> str:
        """The folder, spelled as it is, that already holds the subsystem `name` (in whatever case) when a subsystem row
        for another folder `target` names it, else "". The row then resolves to that subsystem: the folder's modules
        move in beside the ones it holds, and the word is not a collision with itself (Math beside an existing MATH)."""
        if style.folder_unnamed(target):
            held = {self.folder(m) for m in self.modules} - {target, ""}
            return next((f for f in sorted(held) if f.lower() == name.lower() and not style.folder_unnamed(f)), "")
        return ""

    def folder(self, module: str) -> str:
        module = self.alias.get(module, module)
        return PurePosixPath(self.modules[module]["source"]).parent.name if module in self.modules else ""

    def folder_of(self, member: str) -> str:
        return self.folder(self.members[member]["module"]) if member in self.members else ""

    def module_at(self, member: str) -> str:
        """The module (one source file) that holds a member now, after the renames accepted so far."""
        module = self.members[member]["module"] if member in self.members else ""
        return self.alias.get(module, module)

    def reprefix(self, name: str) -> str:
        """The name `name` takes under the module map's prefix for its file, or "" (see `reprefix_of`)."""
        return reprefix_of(self, name)

    def function_word(self, member: str) -> str:
        """The prefix of a function's name: the module map's prefix for the file that holds it (`prefixes`, the one
        authority `recipe style` reads too), else its folder's prefix word."""
        held = self.members.get(self.canon(member), {})
        module = self.modules.get(self.module_at(self.canon(member)), {})
        for path in (held.get("unit"), module.get("source")):
            if path and (word := self.prefixes.get(path)):
                return word
        return self.word(self.folder_of(self.canon(member)))

    def word(self, folder: str) -> str:
        """The prefix word of a folder (a placeholder's is the word before its counter), or an empty string when it
        is neutral or takes no prefix."""
        return style.prefix_word(folder)

    def carried(self, folder: str, name: str) -> dict[str, str]:
        """The members and modules of `folder` that already hold its prefix word, each with the name a subsystem row
        naming the folder `name` gives it (Render_Draw -> Ribbon_Draw, render_ribbon -> ribbon_ribbon)."""
        old = self.word(folder)
        if not old or name == old:
            return {}
        out = {
            n: style.pascal(name) + n[len(old) :]
            for n in self.functions(folder)
            if n.startswith(style.pascal(old) + "_")
        }
        for mod in self.modules:
            if self.folder(mod) == folder and mod.startswith(old + "_"):
                out[mod] = name + mod[len(old) :]
        return out

    def functions(self, folder: str) -> list[str]:
        return [
            m["name"] for m in self.members.values() if m["kind"] == "function" and self.folder_of(m["name"]) == folder
        ]

    def gbi(self, member: str) -> bool:
        unit = self.members.get(member, {}).get("unit")
        if unit and unit not in self._gbi:
            try:
                text = (self.root / unit).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._gbi[unit] = bool(re.search(table("knowledge/style.toml")["gbi"]["use"], text))
        return bool(unit and self._gbi[unit])

    def tag_of(self, callee: str) -> str:
        """The subsystem word an SDK callee name gives: its prefix followed by an upper-case letter or underscore."""
        for prefix, word in table("knowledge/style.toml")["tags"].items():
            if re.match(re.escape(prefix) + r"(?:[A-Z_0-9]|$)", callee):
                return str(word)
        return ""

    def tags(self, names: Iterable[str]) -> Counter[str]:
        found: Counter[str] = Counter()
        for name in names:
            found.update(t for t in (self.tag_of(c) for c in self.calls.get(name, ())) if t)
            found.update([table("knowledge/style.toml")["gbi"]["tag"]] * self.gbi(name))
        return found

    def symbol_kind(self, name: str) -> str:
        """The kind of a name some version's symbol table holds ("function" or "global"), else "" for no such symbol."""
        kinds = {"function" if t[name][3] == "func" else "global" for t in self.tables.values() if name in t}
        return next(iter(kinds)) if len(kinds) == 1 else ""

    def table_name(self, version: str, vram: int, kind: str) -> str:
        """The one spelling the version's symbol table gives a datum at VRAM when no member is placed there (bss and
        other data no module of the map owns, e.g. `D_801539B0 = 0x8014D720` in de), else ''."""
        if self._by_vram is None:
            self._by_vram = {}
            for v, table in self.tables.items():
                for spelled, row in table.items():
                    self._by_vram.setdefault((v, row[0], "function" if row[3] == "func" else "data"), set()).add(
                        spelled
                    )
        held = self._by_vram.get((version, vram, kind), set())
        return next(iter(held)) if len(held) == 1 else ""

    def canon(self, name: str) -> str:
        """The member a spelling names: itself, or the member that sits where a version's symbol table puts that
        spelling (a per-version name, an alias such as D_blob_* or D_auto_*, a function's old name in stale assembly),
        names_from's table first. A spelling no member's stays as it is."""
        if name in self.members:
            return name
        if self._spelled is None:
            at: dict[tuple[str, int, int], str] = {}
            vram: dict[tuple[str, int], set[str]] = {}
            inside: dict[str, list[tuple[int, int, str]]] = {}  # per version a data member's ROM span, by start
            for m in self.members.values():
                for p in m["placements"]:
                    at[(p["version"], p["rom_start"], p["vram"])] = m["name"]
                    vram.setdefault((p["version"], p["vram"]), set()).add(m["name"])
                    if m["kind"] == "data" and p["rom_end"] > p["rom_start"]:
                        inside.setdefault(p["version"], []).append((p["rom_start"], p["rom_end"], m["name"]))
            spans = {v: sorted(s) for v, s in inside.items()}
            # names_from first: a datum split into one member per version group resolves as names_from places it
            versions = sorted(self.doc["versions"], key=lambda v: v != self.doc["names_from"])
            found = {
                spelling: member
                for version, table in reversed([(v, self.tables[v]) for v in versions])
                for spelling, row in table.items()
                if spelling not in self.members
                and (member := at.get((version, row[1] or 0, row[0])) or _within(spans.get(version, ()), row[1]))
            }
            self._vram, self._spelled = vram, found
        if name in self._spelled:
            return self._spelled[name]
        if neutral := re.fullmatch(r"(?:func|D)_([0-9A-F]{8})(?:_(\w+))?", name):  # a neutral name: its version's VRAM
            version = (neutral[2] or "").replace("_", "-")
            version = version if version in self.doc["versions"] else self.doc["names_from"]
            held = self._vram.get((version, int(neutral[1], 16)), set())
            if len(held) == 1:
                return next(iter(held))
        return name

    def spellings(self, target: str) -> list[str]:
        """The target and every other neutral spelling a version's symbol table gives the same address, so a rename
        reaches the table (and the assembly extracted from it) of every version, not only the one the name came from."""
        member = self.members.get(self.canon(target))
        places = {(p["version"], p["vram"]) for p in member["placements"]} if member else set()
        found = [t for v, table in self.tables.items() for t, row in table.items() if (v, row[0]) in places]
        return [target, *dict.fromkeys(t for t in found if t != target and style.neutral(t))]

    def sdk_name(self, member: str) -> str:
        """The SDK name the project's `versions/<v>/libultra_names.tsv` (vram, name, ...) gives a member's address in
        a version that places it, or "" when no table names it."""
        if self._sdk is None:
            self._sdk = structure.sdk_names(self.root, self.doc["versions"])
        return next(
            (
                n
                for p in self.members.get(member, {}).get("placements", ())
                if (n := self._sdk.get((p["version"], p["vram"])))
            ),
            "",
        )

    def module_of(self, target: str) -> str:
        """The module a module row names: its key (the source path), or the one module whose source has that stem or
        that path without `src/` and suffix (CODE_80426EC4, FRONTEND/CODE_80426EC4)."""
        if target in self.modules:
            return target
        path = PurePosixPath
        found = [
            k
            for k, m in self.modules.items()
            if target
            in (m["source"], path(m["source"]).stem, str(path(m["source"]).with_suffix("")).removeprefix("src/"))
        ]
        return found[0] if len(found) == 1 else target

    def users(self, target: str, kind: str) -> list[str]:
        """The members whose evidence speaks for a target: itself, or a folder's or module's members."""
        if kind in ("function", "global", "static"):
            name = self.canon(target)
            return (
                [name]
                if name in self.members or (kind in ("function", "global") and self.symbol_kind(name) == kind)
                else []
            )
        if kind == "subsystem":
            return [m for mod in self.modules if self.folder(mod) == target for m in self.modules[mod]["members"]]
        return list(self.modules.get(target, {}).get("members", ()))

    def verifies(self, item: str, names: Sequence[str]) -> bool:
        if slicepack.extra_form(item):  # `body L<n>: ...` and `string TEXT`: the source and the data bytes show them
            if self._strings is None:
                self._strings, self._source = slicepack.Strings(self.config, self), slicepack.Source(self.root)
            assert self._source is not None
            # a datum has no body: the functions that load it speak for it (the functions `caller` evidence names)
            names = list(dict.fromkeys(x for n in names for x in ([n] if n in self.calls else self.callers.get(n, ()))))
            return slicepack.verifies(self, item, names, self._strings, self._source)
        found = _EVIDENCE.match(item)
        if not found:
            return False
        kind, value = found[1], found[2] if found[1] in ("tag", "debug") else self.canon(found[2])
        if kind == "debug":  # the original record of that name sits where the target is
            return self.catalog is not None and any(
                debug.placed(self.catalog, value, self.members[n]["placements"]) for n in names if n in self.members
            )
        if kind == "tag":
            return self.tags(names)[value] > 0
        if kind in ("caller", "callee") and value in names and value in self.recursive:
            return True  # the function calls itself
        if kind == "caller":
            return any(value in self.callers.get(n, ()) for n in names)
        if kind == "reads":  # a member whose address the code loads: the card's `loads`
            return any(self.same(value, x) for n in names for x in self.loads.get(n, ()))
        return any(self.same(value, x) for n in names for x in self.calls.get(n, ()))

    def externs(self) -> set[str]:
        """The words of every `extern` declaration in the project's sources and headers: a datum declared extern in
        any one of them (another version file, a header) is linked across files whatever one version reads, so its
        name is a global's (`g_`), never a static's (`s_`: style global-name)."""
        if self._externs is None:
            done = host.run(
                ["git", "-c", "core.fsmonitor=false", "grep", "-h", "-E", r"^[ \t]*extern[ \t][^(]*;", "--", *_SOURCES],
                self.root,
                self.config,
            )
            text = done.stdout.decode("utf-8", errors="replace") if done.exit in (0, 1) else ""
            self._externs = set(re.findall(r"[A-Za-z_]\w*", text))
        return self._externs

    def is_extern(self, name: str) -> bool:
        """Whether a datum is declared `extern` anywhere under ANY spelling of it: its own name or a version twin's
        (`D_800C2950_de` in a header beside the member `D_800C2950`), which `externs` alone misses (it holds words)."""
        if self._extern_members is None:
            self._extern_members = {self.canon(w) for w in self.externs()}
        return name in self.externs() or self.canon(name) in self._extern_members

    def same(self, a: str, b: str) -> bool:
        """Whether two spellings are one symbol: equal, or two names some version's symbol table puts at one address
        (D_800D2988 and the literal pool symbol the %lo pair names are both that address, owned by no member)."""
        if a == b:
            return True
        if self._at is None:
            self._at = {}
            for v, t in self.tables.items():
                for spelling, row in t.items():
                    self._at.setdefault(spelling, set()).add((v, row[0]))
        return bool(self._at.get(a, set()) & self._at.get(b, set()))


def _exists(ctx: Map, row: Json) -> str:
    kind, target = row["kind"], row["target"]
    if kind == "subsystem":
        return "" if any(ctx.folder(m) == target for m in ctx.modules) else "no folder has this name"
    if kind == "module":
        return "" if target in ctx.modules else "no module has this name"
    want = "function" if kind == "function" else "data"
    found = ctx.members.get(ctx.canon(target))
    if not found and kind in ("function", "global") and ctx.symbol_kind(target) == kind:
        return ""
    return "" if found and (found["kind"] == "function") == (want == "function") else f"no {want} has this name"


def _as_before(row: Json, renamed: Mapping[str, str]) -> Json:
    """ROW with each evidence item that names a symbol another row of the same submit renames spelled with the name
    the tree still holds (the assembly is read before the renames apply): `caller Menu_Open` for a caller this file
    names Menu_Open reads as `caller func_8040...` (CLEAN-FRONTEND-4)."""
    evidence = row.get("evidence")
    if not renamed or not isinstance(evidence, list):
        return row
    out = []
    for item in evidence:
        found = _EVIDENCE.match(item) if isinstance(item, str) else None
        if found and found[2] in renamed and found[1] != "debug":
            item = item[: found.start(2)] + renamed[found[2]] + item[found.end(2) :]
        out.append(item)
    return {**row, "evidence": out}


def _applied(ctx: Map, row: Json) -> bool:
    """Whether the row is already in the tree: its target is gone and its new name names the same kind of thing (a
    resubmit after an apply, or after a timed-out run that applied before it exited)."""
    target, name = row.get("target"), row.get("name")
    if row.get("kind") not in _ORDER or not isinstance(target, str) or not isinstance(name, str) or target == name:
        return False
    return bool(_exists(ctx, row)) and not _exists(ctx, {**row, "target": name})


def reserved_hint(name: str) -> str:
    """What to write instead of a name that starts with an underscore (reserved for the implementation): the form
    the style asks for (a lane once read `Prefix_VerbNoun` as `__PREFIX___VerbNoun` and wrote 70 refused rows)."""
    if not name.startswith("_"):
        return ""
    form = name.lstrip("_").replace("___", "_")
    return f"; a leading underscore is reserved: write {form} (Module_VerbNoun, one underscore)"


def _form(ctx: Map, row: Json) -> tuple[str, str]:
    """Whether the new name has the shape its kind and subsystem ask for, and holds no banned word."""
    kind, target, name = row["kind"], row["target"], row["name"]
    word = ""
    if kind == "function" and ctx.sdk_name(ctx.canon(target)) == name:
        return "", ""  # the SDK's own name for the function at this address (the project's libultra_names table)
    if kind == "function" and name in table("knowledge/style.toml").get("sdk", {}).get("libc", ()):
        return "", ""  # a standard C library routine keeps its name whatever folder holds it (no subsystem prefix)
    if kind in ("function", "module"):
        folder = ctx.folder_of(ctx.canon(target)) if kind == "function" else ctx.folder(target)
        word = ctx.function_word(target) if kind == "function" else ctx.word(folder)
        external = kind == "function" and ctx.canon(target) not in ctx.members and ctx.symbol_kind(target) == kind
        class_method = kind == "function" and re.fullmatch(
            table("knowledge/style.toml")["names"]["class_function"], name
        )
        if not word and not style.free_folder(folder) and not external and not class_method:
            return "prefix", (
                f"the subsystem folder {folder} has no name yet: name it first, with a subsystem row for {folder} in "
                f"this same file (subsystem rows are judged first) or from `recipe name next --step N2`, which offers "
                "every unnamed folder"
            )
    if not style.legal(kind, name, word) or style.neutral(name):
        free = kind in ("function", "module") and not word  # a folder that takes no prefix (GAME): say the form
        bare = re.sub(r"^[A-Za-z0-9]+_", "", name) if free and "_" in name else ""
        hint = f"; {folder} takes no prefix: the unprefixed form, such as {bare}" if bare else ""
        if kind == "function" and re.match(r"_*[a-z]", name):  # an SDK routine's own spelling, not a project name
            hint += (
                "; if this is the SDK's own name for the routine, add its address and name to "
                "versions/<v>/libultra_names.tsv in every version that has it, then submit again"
            )
        return "regex", f"{name} does not match {style.pattern(kind, word)}{hint}"
    if kind == "module" and style.collision(name):
        return "regex", f"{name} ends in a collision counter"
    if found := style.banned(name, kind):
        return "banned", f"{found} is a banned word"
    return "", ""


def _head(item: str) -> str:
    """`FORM NAME` of an evidence item, its note dropped."""
    found = _EVIDENCE.match(item)
    return f"{found[1]} {found[2]}" if found else item


def reprefix_of(ctx: Map, name: str) -> str:
    """The name a function takes when its file's prefix (the module map's, `function_word`) is another than the one it
    spells: `Scene_Draw` in a file the map gives `Engine` becomes `Engine_Draw`. Only a prefix this project uses (a map
    prefix or a folder's prefix word) is swapped, so a word that is no prefix (`Particle_...` with no such module)
    stays; a class method, an SDK or standard routine and a name the debug catalog spells are original and stay.
    "" when nothing changes."""
    member = ctx.members.get(ctx.canon(name))
    if member is None or member["kind"] != "function" or style.neutral(name):
        return ""
    word = ctx.function_word(name)
    head, sep, rest = name.partition("_")
    if not word or not sep or not rest or head == style.pascal(word):
        return ""
    if re.fullmatch(table("knowledge/style.toml")["names"]["class_function"], name) or ctx.sdk_name(ctx.canon(name)):
        return ""
    if ctx.catalog is not None and ctx.catalog.named(name):
        return ""
    if ctx._prefix_words is None:
        ctx._prefix_words = {style.pascal(w) for w in ctx.prefixes.values()} | {
            style.pascal(w) for f in {ctx.folder(m) for m in ctx.modules} if (w := style.prefix_word(f))
        }
    if head not in ctx._prefix_words:
        return ""
    return style.pascal(word) + "_" + rest


def _original(ctx: Map, row: Json) -> bool:
    """Whether the row gives a function or datum the name its original debug record gives it there (`debug NAME`
    evidence, verified): the original spelling is the style, and it replaces a name given here."""
    evidence = row.get("evidence")
    return (
        row.get("kind") in ("function", "global", "static")
        and isinstance(evidence, list)
        and f"debug {row.get('name')}" in [_head(e) for e in evidence if isinstance(e, str)]
        and ctx.verifies(f"debug {row['name']}", ctx.users(str(row["target"]), str(row["kind"])))
    )


def judge(ctx: Map, row: Json) -> tuple[str, str]:
    """The first rule the row breaks as (rule, reason), or ("", "")."""
    kind, target, name = row.get("kind"), row.get("target"), row.get("name")
    if kind not in _ORDER or not isinstance(target, str) or not isinstance(name, str) or not row.get("doc"):
        missing = [k for k in ("kind", "target", "name", "doc") if not row.get(k)]
        wrong = [k for k in ("target", "name") if row.get(k) and not isinstance(row.get(k), str)]
        if kind not in _ORDER and row.get("kind"):
            return "shape", f"kind {kind!r} is not one of {', '.join(sorted(_ORDER))}"
        return "shape", "a row needs kind, target, name, evidence and doc; " + (
            f"missing: {', '.join(missing)}" if missing else f"not text: {', '.join(wrong)}"
        )
    if reason := _exists(ctx, row):
        return "target", reason
    name_kind = "file" if kind in ("module", "subsystem") else "symbol"
    # the SDK's own reserved names (`__osDisableInt`) pass, as they do in source admission (unbake symbol_names,
    # implementation)
    if why := api.name_issues(name, name_kind, implementation=True):
        return "portable", why[0] + reserved_hint(name)
    if (
        kind in ("function", "global", "static")
        and name in BUILTINS
        and not (kind == "function" and ctx.sdk_name(ctx.canon(target)) == name)
    ):
        return "builtin", f"{name} is a compiler builtin: use a prefixed name, e.g. Math_{name.capitalize()}"
    # Original/debug and SDK evidence never override target portability.
    merge = ctx.merge_folder(target, name) if kind == "subsystem" else ""
    folded = [n for n in ctx.taken if n != target and n != name and n != merge]
    if why := api.name_issues(name, name_kind, taken=folded, implementation=True):  # the same admission as above
        return "duplicate", why[0]
    if kind == "subsystem":
        carried = ctx.carried(target, name)
        for new in carried.values():
            if why := api.name_issues(new):
                return "portable", why[0]
            if why := api.name_issues(new, taken=sorted(ctx.taken - set(carried) - {merge})):
                return "duplicate", why[0]
    original = _original(ctx, row)
    if kind == "module":
        unnamed = style.neutral_module(PurePosixPath(ctx.modules[target]["source"]).stem)
    else:
        unnamed = style.folder_unnamed(target) if kind == "subsystem" else style.neutral(target)
    if not unnamed and not original and not row.get("_fix"):
        if kind == "function" and ctx.sdk_name(ctx.canon(target)) == target:
            return "catalog", f"{target} is an SDK function: its name is the catalog name"
        # An external name can gain a subsystem when its body is first placed.
        # Permit only adding that required prefix; existing names remain stable.
        placed = ctx.canon(target) in ctx.members
        word = ctx.function_word(target) if kind == "function" and placed else ""
        prefix = style.pascal(word) + "_" if word else ""
        repair = prefix and name == prefix + target and _form(ctx, {**row, "name": target})[0] == "regex"
        swap = kind == "function" and placed and ctx.reprefix(target) == name
        if swap:
            repair = True
        if not repair or ctx.verifies(f"debug {target}", ctx.users(target, kind)):
            return "named", f"{target} already has a name"
    if original and ctx.canon(target) == name:
        return "named", f"{target} already has its original name"
    rule, reason = ("", "") if original else _form(ctx, row)
    if rule:
        return rule, reason
    sdk = kind == "function" and ctx.sdk_name(ctx.canon(target)) == name  # another version may spell it already
    if merge:
        pass  # the folder that holds this subsystem takes the target's modules
    elif (name in ctx.taken and not sdk) or (kind == "subsystem" and ctx.folder_taken(name)):
        return "duplicate", f"{name} is already a symbol, folder or file name"
    if kind == "subsystem" and (
        clash := [f"{a} -> {b}" for a, b in ctx.carried(target, name).items() if b in ctx.taken]
    ):
        return "duplicate", f"carrying the prefix to {name} gives a name already taken: {clash[0]}"
    if sdk:
        return "", ""  # the libultra table line for this address is the evidence, with or without a caller
    if kind == "function" and ctx.canon(target) in ctx.members and ctx.reprefix(target) == name:
        return (
            "",
            "",
        )  # the module map's prefix for the file that holds it is the evidence (`recipe name auto --kinds reprefix`)
    evidence = row.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
        return "evidence", "a name needs at least one evidence item"
    if unknown := [e for e in evidence if not _EVIDENCE.match(e) and not slicepack.extra_form(e)]:
        forms = ", ".join(table("knowledge/style.toml")["evidence_forms"])
        note = table("knowledge/style.toml")["evidence_note"]
        return "evidence", f"evidence item {unknown[0]!r} has no form a reader can check; the forms are {forms}; {note}"
    users = ctx.users(target, kind)
    if missing := [e for e in evidence if not ctx.verifies(e, users)]:
        why = f"the assembly does not show: {missing[0]}"
        named = _EVIDENCE.match(missing[0])
        if named and named.group(1) in ("callee", "caller", "reads") and ctx.canon(named.group(2)) not in ctx.taken:
            # R1 (REV1-ONE-72): a pool the change folded into the unit's literals is no symbol any more
            why += (
                f" ({named.group(2)} is no symbol of this tree: a pool or datum the change removed, e.g. folded into"
                " the unit's literals, cannot be evidence; cite a caller, a callee or a global it still reads)"
            )
        if _head(missing[0]).startswith("tag "):
            seen = sorted(w for w, n in ctx.tags(users).items() if n)
            why += f" (the tags its calls carry: {', '.join(seen) or 'none'})"
        return "unverifiable", why
    return "", ""


def _moves(ctx: Map, row: Json) -> list[dict[str, str]]:
    """The moves of an accepted row, with the evidence's paths and names brought forward for the rows after it."""
    kind, target, name = row["kind"], row["target"], row["name"]
    ctx.taken.add(name)
    if kind not in ("subsystem", "module"):
        return [{"op": "symbol", "subject": old, "target": name} for old in ctx.spellings(target)]
    out: list[dict[str, str]] = []
    if kind == "module":
        module = ctx.modules.pop(target)
        ctx.modules[name] = module
        module["name"] = name
        ctx.alias[target] = name
        out.append({"op": "module", "subject": target, "target": name})
        for op in ("source", "header"):
            if not module[op]:  # a module with no header of its own has nothing to rename there
                continue
            path = PurePosixPath(module[op])
            module[op] = str(path.with_name(name + path.suffix))
            out.append({"op": op, "subject": name, "target": module[op]})
        return out
    folder = ctx.merge_folder(target, name) or ctx.spell(
        name
    )  # as the project spells its folders; the row's name stays the word
    ctx.taken.add(folder)
    carried = ctx.carried(target, name)  # a placeholder's prefixed names follow the folder's new word
    ctx.taken |= set(carried.values())
    out += [{"op": "symbol", "subject": old, "target": new} for old, new in carried.items() if old in ctx.members]
    for module in [m for m in ctx.modules.values() if ctx.folder(m["name"]) == target]:
        if renamed := carried.get(module["name"], ""):
            ctx.modules[renamed], ctx.alias[module["name"]] = ctx.modules.pop(module["name"]), renamed
            out.append({"op": "module", "subject": module["name"], "target": renamed})
            module["name"] = renamed
        for op in ("source", "header"):
            if not module[op]:
                continue
            path = PurePosixPath(module[op])
            moved = PurePosixPath(*[folder if p == target else p for p in path.parts])
            moved = moved.with_name(renamed + path.suffix) if renamed else moved
            if moved != path:
                module[op] = str(moved)
                out.append({"op": op, "subject": module["name"], "target": module[op]})
    return out


def _ledger(config: Config, name: str) -> str:
    path = _root(config) / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _patch(config: Config, doc: Json, moves: Sequence[Json]) -> Json:
    """The moves as a saved patch: `{"patch", "files", "moves", "path", "stream"}`, the patch built on the files as they
    are; `path` and `stream` (the patch for stdout) are empty when no move applied."""
    built = {"patch": "", "files": [], "moves": 0}
    if moves:
        captured = api.capture(config) if doc else None
        built = dict(
            structure.build(
                config.project.root,
                moves,
                lambda name: captured.owner(doc["names_from"], name) if captured else None,
                structure.modules_of(doc) if doc else {},
            )
        )
    return {
        **built,
        "path": structure.save(config, built) if built["moves"] else "",
        "stream": structure.stream(built) if built["moves"] else "",
    }


def _make(config: Config, root: Path) -> Callable[[list[str]], tuple[str, str]]:
    """The settle compile: these object targets built by unbake's make (the host jobserver and compiler cache; the
    settle ran its own `nice make -j4` before)."""

    def run(targets: list[str]) -> tuple[str, str]:
        return api.make(config, targets)

    return run


def _land(
    config: Config, params: Json, built: Json, renames: Mapping[str, str] | None = None, ours: Collection[str] = ()
) -> Json:
    """Apply the saved patch to the tree now (`recipe apply`: only the renamed tokens change, then `unbake sync`) unless
    the caller passed `--no-apply`; one `next` link comes back. A refusal keeps the saved patch and says why."""
    if not built["moves"] or params.get("no_apply") or not built["path"]:
        return {}
    root = config.project.root
    try:
        with api.tree_lock(config, exclusive=True, who="name apply"):
            done = structure.apply(config, {"patch": str(root / built["path"]), "host": params.get("host")})
            settled = (
                settle.settle(config, renames, ours, run=_make(config, root))
                if renames and not params.get("no_settle")
                else {}
            )
    except Refusal as refusal:
        why = refusal.findings[0].reason if refusal.findings else "refused"
        return {"applied": False, "apply_refused": why, "next": "recipe apply"}
    return {
        "applied": True,
        "changed": done.get("changed") or built["files"],
        "stream": "",
        "next": "recipe name next",
        **({"settle": settled} if settled else {}),
    }


def _number(config: Config) -> int:
    """The next pack or names number: one past every pack and names file there is."""
    found = [
        int(m[1]) for p in _root(config).glob("*-*") if (m := re.match(r"(?:pack|names|banners|types)-(\d+)\.", p.name))
    ]
    return max(found, default=0) + 1


def _card(ctx: Map, kind: str, target: str, version: str) -> Json:
    names = ctx.users(target, kind)
    members = [ctx.members[n] for n in names if n in ctx.members]
    calls = Counter(c for n in names for c in ctx.calls.get(n, ()) if c not in names)
    card: dict[str, Any] = {
        "target": target,
        "kind": kind,
        "size": sum(_bytes(m, version) for m in members),
        "callees": [c for c, _ in calls.most_common(_CARD_LIMIT)],
        "tags": dict(ctx.tags(names)),
        "loads": sorted({d for n in names for d in ctx.loads.get(n, ())})[:_CARD_LIMIT],
    }
    outside = Counter(c for n in names for c in ctx.callers.get(n, ()) if c not in names)
    card["callers"] = [c for c, _ in outside.most_common(_CARD_LIMIT)]
    card["fan_in"] = sum(outside.values())
    if kind in ("function", "global", "static"):
        card["file"] = ctx.modules.get(ctx.module_at(target), {}).get("source", "")
        card["subsystem"] = ctx.folder_of(target)
    else:
        card["human_names"] = [n for n in names if not style.neutral(n)][:_CARD_LIMIT]
    return card


def _targets(ctx: Map, step: str) -> list[tuple[str, str]]:
    """The (kind, target) pairs an LLM step names, the most useful first."""
    neutral = {n: m for n, m in ctx.members.items() if style.neutral(n)}
    if step == "N2":  # every subsystem folder with no name yet, a neutral sys_ one or a placeholder like FX_2: the
        # functions and modules in it wait on its name (a library's neutral folder is no subsystem)
        every = {ctx.folder(m) for m in ctx.modules}
        folders = {f for f in every if style.folder_unnamed(f) and (f.startswith("sys_") or not style.neutral(f))}
        return [("subsystem", f) for f in sorted(folders)]
    if step == "N1":  # leaf numeric functions many subsystems call
        leaf = [n for n, m in neutral.items() if m["kind"] == "function" and n in ctx.calls and not ctx.calls[n]]
        spread = {n: {ctx.folder_of(c) for c in ctx.callers.get(n, ())} for n in leaf}
        return [("function", n) for n in sorted(leaf) if len(spread[n]) >= 5]
    if step == "N3":  # public API: called from another subsystem, by fan-in
        external = {
            n
            for t in ctx.tables.values()
            for n in t
            if style.neutral(n) and n not in ctx.members and ctx.canon(n) == n and ctx.symbol_kind(n) == "function"
        }
        rows = [
            (len({c for c in ctx.callers.get(n, ()) if ctx.folder_of(c) != ctx.folder_of(n)}), n)
            for n, m in neutral.items()
            if m["kind"] == "function" and ctx.function_word(n)
        ]
        rows += [(len(ctx.callers.get(n, ())), n) for n in external]
        return [("function", n) for fan, n in sorted(rows, key=lambda r: (-r[0], r[1])) if fan]
    if step == "N5":  # internal-only: no caller outside its own folder, so N3 never packs it; the evidence is local
        # (callers and callees inside the folder, the tags its calls carry)
        rows = []
        for n, m in neutral.items():
            if m["kind"] != "function" or not ctx.function_word(n):
                continue
            home = ctx.folder_of(n)
            callers = ctx.callers.get(n, ())
            if any(ctx.folder_of(c) != home for c in callers):
                continue
            if n not in ctx.calls or not (callers or ctx.calls[n] or ctx.tags([n])):
                continue  # nothing a reader could check
            placed = _bytes(m, ctx.doc["names_from"]) > 0  # the version the names are read from first
            rows.append((placed, len(callers) + len(ctx.calls[n]), n))
        return [("function", n) for _, _, n in sorted(rows, key=lambda r: (not r[0], -r[1], r[2]))]
    # N4: a datum is static when one file reads it (C's file scope: the data may sit in a data file of its own, the
    # code file it came from is the one that reads it), global when two files do; a folder is no scope of C
    data = {n: ctx.callers.get(n, []) for n, m in neutral.items() if m["kind"] != "function"}
    external = {
        n for t in ctx.tables.values() for n in t if style.neutral(n) and ctx.canon(n) == n and n not in ctx.members
    }
    data.update({n: ctx.callers.get(n, []) for n in external if ctx.symbol_kind(n) == "global"})
    held = [
        (
            n,
            "global"
            if n not in ctx.members or len({ctx.module_at(c) for c in by}) >= 2 or ctx.is_extern(n)
            else "static",
        )
        for n, by in data.items()
        if by
    ]
    return [(k, n) for n, k in sorted(held, key=lambda r: (-len(data[r[0]]), r[0]))]


def _auto(ctx: Map) -> list[Json]:
    """N2 rows for the folders whose external calls are at least half one tag; a tag word names one folder only."""
    out: list[Json] = []
    claimed: set[str] = set()
    for _, folder in _targets(ctx, "N2"):
        names = [n for n in ctx.users(folder, "subsystem") if n in ctx.members]
        calls = [c for n in names for c in ctx.calls.get(n, ()) if ctx.folder_of(c) != folder]
        tags, external = ctx.tags(names), len(calls) + sum(ctx.gbi(n) for n in names)
        if not tags or not external:
            continue
        word, held = tags.most_common(1)[0]
        if held / external >= 0.5 and word not in ctx.taken | claimed:
            claimed.add(word)
            seen = next((c for c in calls if ctx.tag_of(c) == word), "")
            evidence = [f"tag {word}", *([f"callee {seen}"] if seen else [])]
            out.append(
                {"target": folder, "kind": "subsystem", "name": word, "doc": f"the {word} code", "evidence": evidence}
            )
    return out


def _sdk(ctx: Map) -> list[Json]:
    """Rows for the functions the project's libultra table names at their address: the SDK's own name, in any step."""
    return [
        {"target": n, "kind": "function", "name": sdk, "doc": f"The SDK's {sdk}.", "evidence": []}
        for n, m in ctx.members.items()
        if m["kind"] == "function" and style.neutral(n) and (sdk := ctx.sdk_name(n))
    ]


def _identifiers(root: Path) -> set[str]:
    """Every word the project's sources and headers spell."""
    files = structure.Files(root)
    return {w for p in files.everything() for w in re.findall(r"[A-Za-z_]\w*", files.get(p) or "")}


def _scope(config: Config, doc: Json, target: str) -> set[str]:
    """The words one function spells: its unit source."""
    ctx = Map(config, doc, {})
    name = _chosen(ctx, "T", target)[1]  # an unknown name is refused
    if name not in ctx.members and ctx.canon(name) not in ctx.members:  # a symbol no module owns: the files spelling it
        files = structure.Files(config.project.root)
        text = "\n".join(
            files.get(p) or "" for p in files.everything() if re.search(rf"\b{re.escape(name)}\b", files.get(p) or "")
        )
        return {name, *re.findall(r"[A-Za-z_]\w*", text)}
    module = ctx.members.get(name, {}).get("module", "")
    path = config.project.root / ctx.modules.get(module, {}).get("source", "-")
    return {name, *re.findall(r"[A-Za-z_]\w*", path.read_text(encoding="utf-8") if path.is_file() else "")}


def _types(config: Config, doc: Json, target: str | None) -> Json:
    """`recipe name next --step T`: the project records the debug catalog proves invented-but-original take their
    original type and member names as one patch; every unique match, moved or not, is written to types-K.json.
    `--target F` keeps the records and names F's own text spells, so the patch is F's alone."""
    catalog = debug.load(config)
    if catalog is None:
        raise state.stop(
            "recipe.debug", "no debug catalog is configured", action='add debug_types = "PATH" under [inputs]'
        )
    root = config.project.root
    layouts = cparse.load(api.snapshot(config))
    scope = _scope(config, doc, target) if target else None
    library = [str(x) for x in getattr(config.project, "libraries", ())]
    found = debug.plan(layouts, catalog, _identifiers(root), lambda p: structure.Files(root).get(p), scope, library)
    ctx, names, refused = Map(config, doc, {}), list[Json](), list[Json]()
    rows = [r for r in debug.rows(catalog, doc["members"]) if scope is None or r["target"] in scope]
    for row in sorted(rows, key=lambda r: _ORDER.get(str(r["kind"]), 9)):
        rule, reason = judge(ctx, row)
        if rule:
            refused.append({"row": row, "rule": rule, "reason": reason})
        else:
            names.extend(_moves(ctx, row))
    moves = [*found.moves, *names]
    k = _number(config)
    report = _root(config) / f"types-{k}.json"
    built = _patch(config, doc, moves)
    body = {"catalog": catalog.path, "proposals": list(found.proposals), "names": names, "refused": refused}
    structure.write(report, json.dumps({**body, "skipped": built.get("skipped", [])}, indent=1))
    reasons = Counter(re.sub(r"\d+", "N", p["why"]) or "moved" for p in found.proposals)
    return {
        "action": "next",
        "step": "T",
        "read": len(layouts),
        "pack": str(report.relative_to(root)),
        "targets": len(found.proposals),
        "proposed": sum(1 for p in found.proposals if p["moved"]),
        "reasons": dict(reasons),
        "names": len(names),
        **built,
        "next": _SHIP.format(built["path"]) if built["moves"] else "recipe name next",
        "after": "",
    }


def _leaf(ctx: Map, target: str) -> str:
    """A folder spelled as a path (src/game/FX, game/FX/) is the folder FX; a name that is no path is unchanged."""
    if "/" not in target or target in ctx.members:
        return target
    leaf = PurePosixPath(target.rstrip("/")).name
    return leaf if leaf in {ctx.folder(m) for m in ctx.modules} else target


def _preview(config: Config, params: Json, built: Json) -> Json:
    """A patch that renames across the tree (T) is a preview until `--apply`: the files it touches are listed, the ones
    outside the target's own folders as `outside`, and `--target` plus outside files refuses to apply without
    `--allow-outside` (a record's members are used by every folder that reads it)."""
    root, target = config.project.root, params.get("target")
    own = {f.parent.relative_to(root).as_posix() for f in locals_.folder_files(root, target)} if target else set()
    outside = [f for f in built.get("files", []) if own and not any(f.startswith(d + "/") for d in own)]
    info: Json = {"outside": outside, "applied": False}
    if not built.get("moves"):
        return {}
    if not params.get("apply"):
        return {
            **info,
            "next": f"recipe name next --step {params['step']} --apply" + (f" --target {target}" if target else ""),
        }
    if outside and not params.get("allow_outside"):
        why = f"{len(outside)} files outside {', '.join(sorted(own))} would change; add --allow-outside to apply"
        return {**info, "apply_refused": why}
    landed = _land(config, {**params, "no_apply": False}, built)
    if landed.get("applied") and outside:  # edits beyond the target are judged by the compiler, not by the evidence
        check = settle.compile_check(config, outside, run=_make(config, config.project.root))
        landed["compile"] = check
        if check["errors"]:
            landed["settle"] = {"settled": False, "compiled": check["compiled"], "fixed": 0, "unfixed": check["errors"]}
            landed["next"] = "fix the compile errors in `compile`, then rebuild the stamp"
    return landed


def _unread(ctx: Map, folder: str | None) -> Json:
    """N4 packs only data some extracted assembly reads. The rest has no evidence the pack can cite: counted by class so
    lanes stop chasing it (`no_evidence`), per kind of datum in `skipped`."""
    skipped: Counter[str] = Counter()
    for name, member in ctx.members.items():
        if member["kind"] == "function" or not style.neutral(name) or ctx.callers.get(name):
            continue
        if (
            folder
            and ctx.folder_of(name) != folder
            and folder not in ctx.modules.get(member["module"], {}).get("source", "")
        ):
            continue
        skipped["blob member" if re.match(r"D_(?:blob|auto)", name) else "no reader in the assembly"] += 1
    return {"no_evidence": sum(skipped.values()), "skipped": dict(skipped)}


def _members(config: Config, params: Json) -> Json:
    """`recipe name next --step M --target STRUCT|HEADER`: the unkNN members of a record (every record a header
    defines) with the evidence every typed access in the project gives each."""
    target, root = params.get("target"), config.project.root
    if not target:
        raise state.stop(
            "recipe.name", "step M needs --target STRUCT or HEADER", action="recipe name next --step M --target STRUCT"
        )
    texts = members_.texts_of(root)
    types = access.Types(texts)
    types.learn(texts)
    if target in texts or f"include/{target}" in texts:
        path = target if target in texts else f"include/{target}"
        keys = sorted({types.key[x] for _, _, t, a in structure.bodies(texts[path]) for x in (a, t) if x in types.key})
    else:
        keys = [members_.record(texts, types, target)[0]]
    index, cards, bare = locals_.Prototypes(root), [], 0
    for key in keys:
        unknown = [m for m in types.member.get(key, {}) if members_.UNKNOWN.fullmatch(m)]
        found = members_.gather(texts, types, key, unknown, index) if unknown else {}
        slots = [{"name": m, "type": types.member[key][m], "evidence": found[m]} for m in unknown if found.get(m)]
        bare += len(unknown) - len(slots)
        if slots:
            _, where, tag, alias = members_.record(texts, types, key)
            cards.append({"struct": alias or tag, "path": where, "slots": slots})
    k = _number(config)
    path_, answer = _root(config) / f"pack-{k}.json", f".unbake/recipe/name/names-{k}.jsonl"
    row = {
        "kind": "field",
        "struct": "STRUCT",
        "target": "unk1C",
        "name": "NEW",
        "doc": "what it holds",
        "evidence": ["param CALLEE NAME", "stored NAME", "loaded NAME"],
    }
    pack = {
        "step": "M",
        "k": k,
        "targets": cards,
        "answer": answer,
        "row": row,
        "rule": "one row per member; every evidence item must be one listed for it; the name is a lower-case word"
        " that is spelled nowhere else in the tree (renameproof refuses a name already in use)",
    }
    if cards:
        structure.write(path_, json.dumps(pack))
    return {
        "action": "next",
        "step": "M",
        "pack": str(path_.relative_to(root)) if cards else None,
        "targets": len(cards),
        "slots": sum(len(c["slots"]) for c in cards),
        "no_evidence": bare,
        "functions_left": 0,
        "next": f"recipe name submit {answer}" if cards else "recipe name next",
    }


def _locals(config: Config, params: Json) -> Json:
    """`recipe name next --step L --target FOLDER|FN`: per function the decompiler-named locals and parameters with the
    evidence the source gives each (callee parameter name, record member, loop/index/return role)."""
    target, root = params.get("target"), config.project.root
    if not target:
        raise state.stop(
            "recipe.name", "step L needs --target FOLDER or FN", action="recipe name next --step L --target FOLDER"
        )
    files = locals_.folder_files(root, target)
    if not files:
        raise state.stop(
            "recipe.name", f"no source folder or function is named {target}", action="recipe name next --step L"
        )
    cards, bare = _local_cards(root, target, files, locals_.Prototypes(root), debug.variables(config))
    cards.sort(key=lambda c: (-len(c["slots"]), c["path"], c["line"]))
    return _local_pack(config, params, cards, bare)


def _local_cards(
    root: Path, target: str, files: Sequence[Path], index: Any, original: Mapping[str, Sequence[str]] | None = None
) -> tuple[list[Json], int]:
    """(cards, slots without evidence) of step L for TARGET (a folder or one function) over FILES; ORIGINAL maps a
    function to the variable names the debug catalog gives it (each is `debug NAME` evidence for its slots)."""
    original = original or {}
    single = not locals_.is_folder(root, target)
    cards: list[Json] = []
    bare = 0
    for file in files:
        text = file.read_text(encoding="utf-8", errors="replace")
        masked = access.mask(text)
        rel = file.relative_to(root).as_posix()
        for fn in locals_.functions(text):
            if single and fn.name != target:
                continue
            body = masked[fn.start : fn.end]
            rows = []
            for slot in locals_.slots(text, fn):
                found = locals_.suggestions(body, slot.name, index.get(locals_.calls(body)), original.get(fn.name, ()))
                if found:
                    rows.append(
                        {"name": slot.name, "kind": slot.kind, "type": slot.type, "line": slot.line, "evidence": found}
                    )
                else:
                    bare += 1
            if rows:
                cards.append({"function": fn.name, "path": rel, "line": fn.line, "slots": rows})
    return cards, bare


def _local_pack(config: Config, params: Json, cards: list[Json], bare: int) -> Json:
    root = config.project.root
    pick = cards[: int(params["count"])]
    k, rules = _number(config), table("knowledge/style.toml")
    path, answer = _root(config) / f"pack-{k}.json", f".unbake/recipe/name/names-{k}.jsonl"
    row = {
        "kind": "local",
        "target": "OLD",
        "name": "NEW",
        "path": "PATH",
        "line": 0,
        "function": "FN",
        "doc": "what it holds",
        "evidence": ["param CALLEE NAME", "member NAME", "role loop", "debug NAME"],
    }
    pack = {
        "step": "L",
        "k": k,
        "targets": pick,
        "answer": answer,
        "row": row,
        "rule": "one row per local or parameter; every evidence item must be one listed for it; the new name is a "
        "lower-case word (camelCase or snake_case as the folder spells) that is not a decompiler slot name",
    }
    pack |= {x: rules[x] for x in ("names", "banned")}
    if pick:
        structure.write(path, json.dumps(pack))
    return {
        "action": "next",
        "step": "L",
        "pack": str(path.relative_to(root)) if pick else None,
        "targets": len(pick),
        "slots": sum(len(c["slots"]) for c in pick),
        "no_evidence": bare,
        "functions_left": len(cards) - len(pick),
        "next": f"recipe name submit {answer}" if pick else "recipe name next",
    }


def _chosen(ctx: Map, step: str, target: str) -> tuple[str, str]:
    """(kind, target) for `--target`: a folder is a subsystem, a function a function, a datum static or global as N4
    reads it (the step's own kind when it lists the target); a name the evidence lacks is refused."""
    target = _leaf(ctx, target)
    found = next((t for t in _targets(ctx, step) if t[1] == target), None) if step in ("N2", "N4") else None
    if found:
        return found
    member = ctx.members.get(target) or ctx.members.get(ctx.canon(target))
    if member and member["kind"] == "function":
        return ("function", member["name"])
    if member:
        readers = {ctx.module_at(c) for c in ctx.callers.get(member["name"], ())}
        return ("global" if len(readers) >= 2 or ctx.is_extern(member["name"]) else "static", member["name"])
    if target in {ctx.folder(m) for m in ctx.modules}:
        return ("subsystem", target)
    if kind := ctx.symbol_kind(target):  # a symbol some version's table knows, owned by no module (a bss datum)
        return (kind, target)
    raise state.stop("recipe.name", f"no member or folder is named {target}", action="recipe name next --step N3")


def next_(config: Config, params: Json) -> Json:
    """`recipe name next --step`: tag-named N2 becomes a batch of moves at once, every step an evidence pack; T
    renames invented records to the debug catalog's original types."""
    if params["step"] == "L":  # source evidence only: no ROM map, no census
        return _locals(config, params)
    if params["step"] == "M":
        return _members(config, params)
    if params["step"] == "S":  # a slice's functions with their slots, read-only (T4)
        doc = structure.evidence(config, params["host"])
        return slicepack.build(config, params, Map(config, doc, reads(config, doc)), doc)
    step, doc = params["step"] or "N3", structure.evidence(config, params["host"])
    debt = api.naming_debt(config.project.root)
    debt_path = _root(config) / "symbol-name-debt.json"
    structure.write(debt_path, json.dumps(debt, indent=2) + "\n")
    if step == "T":
        typed = {
            **_types(config, doc, params.get("target")),
            "naming_debt": str(debt_path.relative_to(config.project.root)),
        }
        return {**typed, **_preview(config, params, typed)}
    graph = reads(config, doc)
    ctx, moves, done = Map(config, doc, graph), list[Json](), set[str]()
    for row in [*(_auto(ctx) if step == "N2" else []), *_sdk(ctx)]:
        if not judge(ctx, row)[0]:
            moves, done = [*moves, *_moves(ctx, row)], done | {row["target"]}
    wanted = params.get("target")
    if wanted:  # the one thing the caller already chose: its card alone, never the step's best other target
        pick = [_chosen(ctx, step, wanted)]
        if pick[0][0] == "subsystem" and step != "N2":  # a named folder: the step's best targets inside it
            folder = pick[0][1]
            pick = [t for t in _targets(ctx, step) if ctx.folder_of(t[1]) == folder and t[1] not in done]
            if step == "N4":  # data is named where it is read: a blob member sits in a data folder, an extern has none
                used = {
                    t: sum(ctx.folder_of(c) == folder for c in ctx.callers.get(t[1], ())) for t in _targets(ctx, step)
                }
                pick = sorted((t for t, u in used.items() if u and t[1] not in done), key=lambda t: (-used[t], t[1]))
            pick = pick[: int(params["count"])]
        elif pick[0][1] not in ctx.members and pick[0][0] not in ("function", "global"):
            raise state.stop(
                "recipe.name",
                f"{wanted} is a symbol no module owns; only step T takes it",
                action="recipe name next --step T",
            )
    else:
        pick = [t for t in _targets(Map(config, doc, graph), step) if t[1] not in done][: int(params["count"])]
    path = None
    answer = ""
    if pick:
        k, rules = _number(config), table("knowledge/style.toml")
        path, answer = _root(config) / f"pack-{k}.json", f".unbake/recipe/name/names-{k}.jsonl"
        cards = [_card(ctx, kind, t, doc["names_from"]) for kind, t in pick]
        pack = {"step": step, "k": k, "targets": cards, "answer": answer, "naming_debt": debt}
        pack |= {x: rules[x] for x in ("names", "banned", "evidence_forms", "evidence_note")}
        structure.write(path, json.dumps(pack))
    built = _patch(config, doc, moves)
    return {
        **(_unread(ctx, wanted) if step == "N4" else {}),
        "action": "next",
        "step": step,
        "read": len(graph),
        "naming_debt": str(debt_path.relative_to(config.project.root)),
        "pack": str(path.relative_to(config.project.root)) if path else None,
        "targets": len(pick),
        **built,
        "next": _SHIP.format(built["path"])
        if built["moves"]
        else f"recipe name submit {answer}"
        if pick
        else "recipe name next",
        "after": f"recipe name submit {answer}" if pick else "",
        **_land(config, params, built),
    }


_FIELD = {
    "target": "target",
    "named": "target",
    "catalog": "target",
    "evidence": "evidence",
    "unverifiable": "evidence",
    "shape": "row",
    "portable": "name",
    "prefix": "name",
    "regex": "name",
    "banned": "name",
    "duplicate": "name",
    "builtin": "name",
}


LISTED = 200  # rows listed by name in a submit result


def _row_line(row: Json) -> str:
    where = f" {row['struct']}" if row.get("kind") == "field" and row.get("struct") else ""
    where += f" in {row['function']}" if row.get("kind") == "local" and row.get("function") else ""
    return f"{row.get('kind', '?')}{where} {row.get('target', '?')} -> {row.get('name', '?')}"


def refusal_lines(refused: Sequence[Json], limit: int = 20) -> list[str]:
    """One line per refused row naming the rule and the field it judged: `refused RULE FIELD TARGET -> NAME: reason`."""
    out = []
    for r in refused[:limit]:
        row = r["row"]
        field = _FIELD.get(r["rule"], "row")
        where = row.get("key") or row.get("target", "?")
        out.append(f"refused {r['rule']} {field} {where} -> {row.get('name', '?')}: {r['reason']}")
    return out + (
        [f"refused ... {len(refused) - limit} more in .unbake/recipe/name/refused.jsonl"]
        if len(refused) > limit
        else []
    )


@dataclass
class Judged:
    """The rows of one file after the rules: accepted with their moves, refused with the rule, or held already."""

    accepted: list[tuple[Json, Sequence[Json]]] = field(default_factory=list)
    refused: list[Json] = field(default_factory=list)
    already: list[Json] = field(default_factory=list)
    doc: Json = field(default_factory=dict)
    rows: list[Json] = field(default_factory=list)


def read_rows(path: Path) -> list[Json]:
    """The rows of a names file; a line that is no JSON object comes back as {"bad": line}."""
    rows: list[Json] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            row = {"bad": line}
        rows += [row if isinstance(row, dict) else {"bad": line}] if row is not None else []
    return rows


def judge_rows(
    config: Config,
    params: Json,
    rows: list[Json],
    queued: Sequence[Json] = (),
    gate: Any = None,
    landed: Mapping[str, str] | None = None,
) -> Judged:
    """Every row of a names file against the rules and the evidence. `queued` = rows already accepted but not yet in the
    tree (the ledger's), whose names and slots count as taken; `gate(row)` returns (rule, reason) to refuse a row before
    any other rule (the ledger's ownership check). `landed` = key -> the name this lane's landed row gave it: a row that
    renames a function or datum from exactly that name is the lane's correction under the same key (fixes forward)."""
    out = Judged(rows=rows)
    ctx: Map | None = None
    repairs = repair.Repairs(config.project.root, lambda: debug.variables(config))
    fields = members_.Fields(config.project.root, loose=not params.get("no_apply") and not params.get("no_settle"))
    # new name -> the name the tree holds, for the symbol rows of this file (evidence may name either spelling)
    renamed = {
        str(r["name"]): str(r["target"])
        for r in [*queued, *rows]
        if r.get("kind") in ("function", "global", "static")
        and isinstance(r.get("name"), str)
        and isinstance(r.get("target"), str)
    }
    for q in queued:  # a name or a slot the queue holds is taken for this file
        if q.get("kind") in ("local", "param") and isinstance(q.get("function"), str):
            repairs.scoped.setdefault(q["function"], set()).add(str(q.get("name")))
        elif q.get("kind") == "field":
            fields.queued.append((str(q.get("struct")), str(q.get("name"))))
        elif isinstance(q.get("name"), str):
            repairs.accepted.add(q["name"])
    keyed = any("key" in r for r in rows) or gate is not None
    if keyed:
        doc = structure.evidence(config, params["host"])
        ctx = Map(config, doc, reads(config, doc))
        out.doc = doc
        ctx.taken |= {str(q["name"]) for q in queued if isinstance(q.get("name"), str)}
    for row in sorted(rows, key=lambda r: _ORDER.get(str(r.get("kind")), 9)):
        if ctx is not None:
            row, rule, reason = namebook.resolve(ctx, fields, repairs, row)
            if rule == "applied":
                out.already.append(row)
                continue
            if rule:
                out.refused.append({"row": row, "rule": rule, "reason": reason})
                continue
            if landed and landed.get(str(row.get("_key"))) == row.get("target") != row.get("name"):
                row = {**row, "_fix": True}
        if gate is not None and (why := gate(row)):
            if why[0] == "applied":
                out.already.append(row)
            else:
                out.refused.append({"row": row, "rule": why[0], "reason": why[1]})
            continue
        if row.get("suggest"):  # a suggestion is checked for shape and key only (its owner judges the name)
            if (
                isinstance(row.get("name"), str)
                and row["name"]
                and isinstance(row.get("doc"), str)
                and row["doc"].strip()
            ):
                out.accepted.append((row, []))
            else:
                out.refused.append({"row": row, "rule": "shape", "reason": "a suggestion needs a key, name and doc"})
            continue
        if row.get("kind") == "param":  # a parameter is a local of its function (CLEAN-MENU2-3 wrote "param")
            row = {**row, "kind": "local"}
        if row.get("kind") in ("local", "repair"):
            rule, reason, direct = repairs.judge(row)
            if rule == "applied":
                out.already.append(row)
            elif rule:
                out.refused.append({"row": row, "rule": rule, "reason": reason})
            else:
                out.accepted.append((row, direct))
            continue
        if row.get("kind") in ("field", "type"):
            rule, reason, direct = fields.judge(row) if row["kind"] == "field" else fields.judge_type(row)
            if rule == "applied":
                out.already.append(row)
            elif rule:
                out.refused.append({"row": row, "rule": rule, "reason": reason})
            else:
                out.accepted.append((row, direct))
            continue
        if row.get("kind") not in _ORDER:
            kinds = ", ".join([*sorted(_ORDER), "field", "type", "local", "param", "repair"])
            why = f"unknown naming row kind {row.get('kind')!r}; the kinds are {kinds}"
            out.refused.append({"row": row, "rule": "shape", "reason": why})
            continue
        if ctx is None:
            out.doc = structure.evidence(config, params["host"])
            ctx = Map(config, out.doc, reads(config, out.doc))
            ctx.taken |= {str(q["name"]) for q in queued if isinstance(q.get("name"), str)}
        if row.get("kind") == "module" and isinstance(row.get("target"), str):
            row = {**row, "target": ctx.module_of(row["target"])}
        if "bad" not in row and _applied(ctx, row):
            out.already.append(row)
            continue
        rule, reason = judge(ctx, _as_before(row, renamed)) if "bad" not in row else ("shape", "not a JSON object")
        if rule:
            out.refused.append({"row": row, "rule": rule, "reason": reason})
            continue
        out.accepted.append((row, _moves(ctx, row)))
    return out


def submit(config: Config, params: Json) -> Json:
    """`recipe name submit FILE`: validate every row, append the accepted ones as moves and write their banners."""
    path = Path(params["file"])
    if not path.is_file() or path.suffix != ".jsonl":  # a pack or types report is read, never submitted
        why = "does not exist" if not path.is_file() else "is no names file (one JSON row per line, .jsonl)"
        raise state.stop("recipe.name", f"{path} {why}", action="recipe name next")
    rows = read_rows(path)
    if params.get("ledger"):
        return namebook.submit(config, params, rows)
    k = m[1] if (m := re.search(r"names-(\d+)", path.name)) else str(_number(config))
    judged = judge_rows(config, params, rows)
    accepted, refused, already, doc = judged.accepted, judged.refused, judged.already, judged.doc
    moves = [m for _, group in accepted for m in group]
    built = _patch(config, doc, moves)
    banners = [{"name": r["name"], "kind": r["kind"], "doc": " ".join(r["doc"].split())} for r, _ in accepted]
    structure.write(_root(config) / f"banners-{k}.json", json.dumps(banners))
    old = _ledger(config, "refused.jsonl")
    structure.write(
        _root(config) / "refused.jsonl", old + "".join(json.dumps(r, sort_keys=True) + "\n" for r in refused)
    )
    renames = {m["subject"]: m["target"] for _, group in accepted for m in group if m["op"] == "field"}
    ours = {
        key
        for _, group in accepted
        for m in group
        if m["op"] == "field"
        for key in (
            m.get("alias"),
            f"struct {m['tag']}" if m.get("tag") else "",
            f"union {m['tag']}" if m.get("tag") else "",
        )
        if key
    }
    landed = _land(config, params, built, renames, ours)
    return {
        "action": "submit",
        "rows": len(rows),
        "accepted": len(accepted),
        "refused": len(refused),
        "already": len(already),
        # which rows this run applied (or, with --no-apply, would apply), and which the tree already held
        "accepted_rows": [_row_line(r) for r, _ in accepted][:LISTED],
        "already_rows": [_row_line(r) for r in already][:LISTED],
        "by_rule": dict(Counter(r["rule"] for r in refused)),
        "refusals": refusal_lines(refused),
        "findings": [  # the decomp wrapper prints these when the exit code is 1
            {"key": "recipe.name", "blocking": True, "message": line}
            for line in [*refusal_lines(refused), *settle.lines(landed.get("settle"))]
        ],
        "exit": 1
        if refused or (landed.get("settle") or {}).get("unfixed")
        else 0,  # a refused row is a failure the caller's `&&` must see; accepted rows are still patched
        **built,
        "banners": f".unbake/recipe/name/banners-{k}.json",
        "next": _SHIP.format(built["path"]) if built["moves"] else "recipe name next",
        **landed,
    }


def _banner(name: str, doc: str, vram: str, version: str) -> str:
    where = [f"{version} {vram}"] if vram else []
    return "\n".join(["/*", "=" * 16, name, "", doc, *where, "=" * 16, "*/", ""])


def banners(config: Config, params: Json) -> Json:
    """`recipe name banners`: candidate sources with the id banner above each renamed function, for `submit --bulk`."""
    doc = structure.evidence(config, params["host"])
    ctx, version = Map(config, doc, {}), doc["names_from"]
    wanted = [
        b for p in sorted(_root(config).glob("banners-*.json")) for b in json.loads(p.read_text(encoding="utf-8"))
    ]
    by_unit: dict[str, list[Json]] = {}
    for item in wanted:
        member = ctx.members.get(item["name"])
        if item["kind"] == "function" and member and member["unit"]:
            by_unit.setdefault(member["unit"], []).append(item)
    out, skipped = [], 0
    for unit, items in sorted(by_unit.items()):
        text = (ctx.root / unit).read_text(encoding="utf-8")
        for item in items:
            name = item["name"]
            start = re.search(
                rf"^[ \t]*(?:static[ \t]+)?[\w \t*]*?\b{re.escape(name)}[ \t]*\([^;{{]*\)\s*\{{", text, re.M
            )
            if not start or re.search(rf"={{16}}\n{re.escape(name)}\n", text):
                skipped += 1
                continue
            vram = next((p["vram"] for p in ctx.members[name]["placements"] if p["version"] == version), None)
            line = _banner(name, item["doc"], f"{vram:08X}" if vram is not None else "", version)
            text = text[: start.start()] + line + text[start.start() :]
        target = _root(config) / "banners" / (unit.replace("/", "__"))
        structure.write(target, text)
        out.append(str(target.relative_to(config.project.root)))
    return {
        "action": "banners",
        "files": out,
        "skipped": skipped,
        "next": "unbake submit --bulk " + " ".join(out) if out else "recipe name next",
    }


COUNTED = ("L", "N1", "N3", "N4", "N5")  # the steps `count` reads: a pack of targets with evidence per folder


def count(config: Config, params: Json) -> Json:
    """`recipe name count --steps L,N4,N5 --targets FOLDER...`: per folder and step, how many slots or targets a pack
    would carry with evidence (`namable`) and how many have none (`no_evidence`), read-only (no pack, no patch). One
    evidence build serves every folder and step: `decomp cleanup count --evidence` asked `next --no-apply` once per
    folder and step (45 runs of the same whole-project build, 231 s, WATCH-0823)."""
    steps = [x for x in str(params.get("steps") or "L,N4,N5").split(",") if x]
    if bad := [x for x in steps if x not in COUNTED]:
        raise state.stop("recipe.name", f"count takes steps {', '.join(COUNTED)}, not {', '.join(bad)}")
    targets = list(params.get("targets") or [])
    if not targets:
        raise state.stop("recipe.name", "count needs --targets FOLDER...", action="recipe name count --targets src/X")
    root = config.project.root
    out: dict[str, dict[str, Json]] = {t: {} for t in targets}
    if "L" in steps:
        index, original = locals_.Prototypes(root), debug.variables(config)
        for t in targets:
            files = locals_.folder_files(root, t)
            cards, bare = _local_cards(root, t, files, index, original) if files else ([], 0)
            out[t]["L"] = {"namable": sum(len(c["slots"]) for c in cards), "no_evidence": bare}
    named = [x for x in steps if x != "L"]
    if named:
        doc = structure.evidence(config, params["host"])
        ctx = Map(config, doc, reads(config, doc))
        done = {row["target"] for row in _sdk(ctx) if not judge(ctx, row)[0]}
        for step in named:
            every = [t for t in _targets(ctx, step) if t[1] not in done]
            for t in targets:
                try:
                    kind, folder = _chosen(ctx, step, t)
                except Refusal as refusal:
                    out[t][step] = {"error": refusal.findings[0].reason if refusal.findings else "refused"}
                    continue
                if kind != "subsystem":
                    out[t][step] = {"error": f"{t} is a {kind}, not a folder"}
                    continue
                if step == "N4":
                    used = {u: sum(ctx.folder_of(c) == folder for c in ctx.callers.get(u[1], ())) for u in every}
                    pick = [u for u, n in used.items() if n]
                    out[t][step] = {"namable": len(pick), "no_evidence": _unread(ctx, folder)["no_evidence"]}
                else:
                    pick = [u for u in every if ctx.folder_of(u[1]) == folder]
                    out[t][step] = {"namable": len(pick), "no_evidence": 0}
    return {"action": "count", "steps": steps, "folders": out, "next": "recipe name next --step STEP --target FOLDER"}


def slices_evidence(config: Config, host: Path | None) -> Json:
    """The structure evidence for a slice run, read while no writer holds the tree (a shared tree lock: a land or an
    applier that mutates the tree finishes first). A tree that still changes between `unbake map` and its capture is
    refused with the way out: slices run on a leased tree of their own, never on the tree an applier or a round
    mutates."""
    with api.tree_lock(config, who="recipe name slices"):
        try:
            return structure.evidence(config, host)
        except Refusal as refusal:
            if "tree changed between" not in str(refusal):
                raise
            raise state.stop(
                "recipe.slices",
                f"{config.project.root} changed while the slices were cut (an applier or a round is writing it)",
                action="run slices with --project on a leased tree, not the tree an applier or the train mutates",
            ) from None


def cut(config: Config, params: Json) -> Json:
    """`recipe name slices --lanes N [--map MODULES.tsv] [--assign LANE=ID,ID] [--out DIR]`: the slice document (T3)."""
    where = Path(params["out"]) if params.get("out") else None
    held = slices.load(where)
    lanes = int(params.get("lanes") or (held or {}).get("lanes") or 12)
    if params.get("lanes") or held is None:
        mapping = slices.read_map(slices.map_path(config, params.get("map")))
        doc = slices_evidence(config, params["host"])
        ctx = Map(config, doc, reads(config, doc))
        functions, data, records = slices.collect(ctx, config.project.root)
        result = dict(slices.compute(functions, data, records, lanes, held, mapping, doc["names_from"]))
    else:
        result = dict(held)
    for pair in params.get("assign") or []:
        lane, _, ids = pair.partition("=")
        if not lane or not ids:
            raise state.stop("recipe.name", f"--assign takes LANE=ID[,ID], not {pair!r}", action="recipe name slices")
        slices.assign(result, lane, ids.split(","))
    saved = slices.save(result, where)
    lane_files = slices.write_lane_files(result, where)
    return {
        "action": "slices",
        "path": str(saved),
        "lane_files": [str(p) for p in lane_files],
        "summary": slices.summary(result),
        "slices": len(result["slices"]),
        "next": "recipe name next --step S --target SLICE --count 20 --out DIR",
    }


def auto(config: Config, params: Json) -> Json:
    """`recipe name auto --kinds sdk,catalog,strings --out FILE`: the mechanical names (T6) as ledger rows."""
    kinds = [k for k in str(params.get("kinds") or ",".join(autoname.DEFAULT)).split(",") if k]
    if bad := [k for k in kinds if k not in autoname.KINDS]:
        raise state.stop("recipe.name", f"auto takes kinds {', '.join(autoname.KINDS)}, not {', '.join(bad)}")
    doc = structure.evidence(config, params["host"])
    ctx = Map(config, doc, reads(config, doc))
    where = Path(params["out"]) if params.get("out") else _root(config) / "auto-rows.jsonl"
    return autoname.run(config, ctx, kinds, where, judge, _moves)


def run(config: Config, params: Json) -> Json:
    return {"next": next_, "submit": submit, "banners": banners, "count": count, "slices": cut, "auto": auto}[
        params["action"]
    ](config, params)
