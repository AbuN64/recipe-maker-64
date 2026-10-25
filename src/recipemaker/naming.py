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
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Json, Refusal

from recipemaker import cparse, debug, repair, state, structure, style, table, tree

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
# style.toml evidence_forms; whatever follows the name (a parenthesized note, nested parentheses, `*`, `=`, a `: reason`)
# is the reader's, the name alone is checked
_EVIDENCE = re.compile(r"^(callee|caller|reads|tag|debug) ([^\s():;,]+)(?:[:;,]?(?:\s+.*)?)$")
# `jal NAME` and a tail `j NAME`; a local label starts with a dot, a register with a dollar sign
_CALL = re.compile(r"\*/\s+j(?:al)?\s+([A-Za-z_]\w*)")
_LOAD = re.compile(r"%(?:hi|lo)\(([A-Za-z_]\w*)")
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
            calls = [x for x in dict.fromkeys(_CALL.findall(text)) if x != name and not x.startswith(".")]
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
        self.tables = _tables(config, doc["versions"])
        self.catalog = debug.load(config)  # the original debug records, None when the config names no catalog
        self._spelled: dict[str, str] | None = None  # a spelling to its member, built at the first miss (`canon`)
        self._vram: dict[tuple[str, int], set[str]] = {}  # (version, VRAM) to the members placed there
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
        self.callers: dict[str, list[str]] = {}
        for name in graph:
            for target in (*self.calls[name], *self.loads[name]):
                self.callers.setdefault(target, []).append(name)
        self._gbi: dict[str, bool] = {}
        self.alias: dict[str, str] = {}  # a module renamed by an accepted row: old name to new name
        self.taken: set[str] = {*self.members, *self.modules, *(n for t in self.tables.values() for n in t)}
        self._naming: tree.Naming | None = None
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
        if kind == "caller":
            return any(value in self.callers.get(n, ()) for n in names)
        if kind == "reads":  # a member whose address the code loads: the card's `loads`
            return any(value in self.loads.get(n, ()) for n in names)
        return any(value in self.calls.get(n, ()) for n in names)


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
        word = ctx.word(folder)
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
        return "shape", "a row needs kind, target, name, evidence and doc"
    if reason := _exists(ctx, row):
        return "target", reason
    name_kind = "file" if kind in ("module", "subsystem") else "symbol"
    # the SDK's own reserved names (`__osDisableInt`) pass, as they do in source admission (unbake symbol_names, implementation)
    if why := api.name_issues(name, name_kind, implementation=True):
        return "portable", why[0]
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
    if not unnamed and not original:
        if kind == "function" and ctx.sdk_name(ctx.canon(target)) == target:
            return "catalog", f"{target} is an SDK function: its name is the catalog name"
        # An external name can gain a subsystem when its body is first placed.
        # Permit only adding that required prefix; existing names remain stable.
        placed = ctx.canon(target) in ctx.members
        word = ctx.word(ctx.folder_of(ctx.canon(target))) if kind == "function" and placed else ""
        prefix = word.title() + "_" if word else ""
        repair = prefix and name == prefix + target and _form(ctx, {**row, "name": target})[0] == "regex"
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
    evidence = row.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
        return "evidence", "a name needs at least one evidence item"
    if unknown := [e for e in evidence if not _EVIDENCE.match(e)]:
        forms = ", ".join(table("knowledge/style.toml")["evidence_forms"])
        note = table("knowledge/style.toml")["evidence_note"]
        return "evidence", f"evidence item {unknown[0]!r} has no form a reader can check; the forms are {forms}; {note}"
    users = ctx.users(target, kind)
    if missing := [e for e in evidence if not ctx.verifies(e, users)]:
        why = f"the assembly does not show: {missing[0]}"
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


def _land(config: Config, params: Json, built: Json) -> Json:
    """Apply the saved patch to the tree now (`recipe apply`: only the renamed tokens change, then `unbake sync`) unless
    the caller passed `--no-apply`; one `next` link comes back. A refusal keeps the saved patch and says why."""
    if not built["moves"] or params.get("no_apply") or not built["path"]:
        return {}
    root = config.project.root
    try:
        with api.tree_lock(config, exclusive=True, who="name apply"):
            done = structure.apply(config, {"patch": str(root / built["path"]), "host": params.get("host")})
    except Refusal as refusal:
        why = refusal.findings[0].reason if refusal.findings else "refused"
        return {"applied": False, "apply_refused": why, "next": "recipe apply"}
    return {"applied": True, "changed": done.get("changed") or built["files"], "stream": "", "next": "recipe name next"}


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
            if m["kind"] == "function" and ctx.word(ctx.folder_of(n))
        ]
        rows += [(len(ctx.callers.get(n, ())), n) for n in external]
        return [("function", n) for fan, n in sorted(rows, key=lambda r: (-r[0], r[1])) if fan]
    # N4: a datum is static when one file reads it (C's file scope: the data may sit in a data file of its own, the
    # code file it came from is the one that reads it), global when two files do; a folder is no scope of C
    data = {n: ctx.callers.get(n, []) for n, m in neutral.items() if m["kind"] != "function"}
    external = {
        n for t in ctx.tables.values() for n in t if style.neutral(n) and ctx.canon(n) == n and n not in ctx.members
    }
    data.update({n: ctx.callers.get(n, []) for n in external if ctx.symbol_kind(n) == "global"})
    held = [
        (n, "global" if n not in ctx.members or len({ctx.module_at(c) for c in by}) >= 2 else "static")
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
    found = debug.plan(layouts, catalog, _identifiers(root), lambda p: structure.Files(root).get(p), scope)
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


def _chosen(ctx: Map, step: str, target: str) -> tuple[str, str]:
    """(kind, target) for `--target`: a folder is a subsystem, a function a function, a datum static or global as N4
    reads it (the step's own kind when it lists the target); a name the evidence lacks is refused."""
    found = next((t for t in _targets(ctx, step) if t[1] == target), None) if step in ("N2", "N4") else None
    if found:
        return found
    member = ctx.members.get(target) or ctx.members.get(ctx.canon(target))
    if member and member["kind"] == "function":
        return ("function", member["name"])
    if member:
        readers = {ctx.module_at(c) for c in ctx.callers.get(member["name"], ())}
        return ("global" if len(readers) >= 2 else "static", member["name"])
    if target in {ctx.folder(m) for m in ctx.modules}:
        return ("subsystem", target)
    if kind := ctx.symbol_kind(target):  # a symbol some version's table knows, owned by no module (a bss datum)
        return (kind, target)
    raise state.stop("recipe.name", f"no member or folder is named {target}", action="recipe name next --step N3")


def next_(config: Config, params: Json) -> Json:
    """`recipe name next --step`: tag-named N2 becomes a batch of moves at once, every step an evidence pack; T
    renames invented records to the debug catalog's original types."""
    step, doc = params["step"] or "N3", structure.evidence(config, params["host"])
    debt = api.naming_debt(config.project.root)
    debt_path = _root(config) / "symbol-name-debt.json"
    structure.write(debt_path, json.dumps(debt, indent=2) + "\n")
    if step == "T":
        typed = {
            **_types(config, doc, params.get("target")),
            "naming_debt": str(debt_path.relative_to(config.project.root)),
        }
        return {**typed, **_land(config, params, typed)}
    graph = reads(config, doc)
    ctx, moves, done = Map(config, doc, graph), list[Json](), set[str]()
    for row in [*(_auto(ctx) if step == "N2" else []), *_sdk(ctx)]:
        if not judge(ctx, row)[0]:
            moves, done = [*moves, *_moves(ctx, row)], done | {row["target"]}
    wanted = params.get("target")
    if wanted:  # the one thing the caller already chose: its card alone, never the step's best other target
        pick = [_chosen(ctx, step, wanted)]
        if pick[0][1] not in ctx.members and pick[0][0] not in ("function", "global"):
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


def submit(config: Config, params: Json) -> Json:
    """`recipe name submit FILE`: validate every row, append the accepted ones as moves and write their banners."""
    path = Path(params["file"])
    if not path.is_file() or path.suffix != ".jsonl":  # a pack or types report is read, never submitted
        why = "does not exist" if not path.is_file() else "is no names file (one JSON row per line, .jsonl)"
        raise state.stop("recipe.name", f"{path} {why}", action="recipe name next")
    doc: Json = {}
    ctx: Map | None = None
    repairs = repair.Repairs(config.project.root)
    rows: list[Json] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            row = {"bad": line}
        rows += [row if isinstance(row, dict) else {"bad": line}] if row is not None else []
    k = m[1] if (m := re.search(r"names-(\d+)", path.name)) else str(_number(config))
    accepted: list[tuple[Json, Sequence[Json]]] = []
    refused: list[Json] = []
    for row in sorted(rows, key=lambda r: _ORDER.get(str(r.get("kind")), 9)):
        if row.get("kind") in ("local", "repair"):
            rule, reason, direct = repairs.judge(row)
            if rule:
                refused.append({"row": row, "rule": rule, "reason": reason})
            else:
                accepted.append((row, direct))
            continue
        if row.get("kind") not in _ORDER:
            refused.append({"row": row, "rule": "shape", "reason": "unknown naming row kind"})
            continue
        if ctx is None:
            doc = structure.evidence(config, params["host"])
            ctx = Map(config, doc, reads(config, doc))
        if row.get("kind") == "module" and isinstance(row.get("target"), str):
            row = {**row, "target": ctx.module_of(row["target"])}
        rule, reason = judge(ctx, row) if "bad" not in row else ("shape", "not a JSON object")
        if rule:
            refused.append({"row": row, "rule": rule, "reason": reason})
            continue
        accepted.append((row, _moves(ctx, row)))
    moves = [m for _, group in accepted for m in group]
    built = _patch(config, doc, moves)
    banners = [{"name": r["name"], "kind": r["kind"], "doc": " ".join(r["doc"].split())} for r, _ in accepted]
    structure.write(_root(config) / f"banners-{k}.json", json.dumps(banners))
    old = _ledger(config, "refused.jsonl")
    structure.write(
        _root(config) / "refused.jsonl", old + "".join(json.dumps(r, sort_keys=True) + "\n" for r in refused)
    )
    return {
        "action": "submit",
        "rows": len(rows),
        "accepted": len(accepted),
        "refused": len(refused),
        "by_rule": dict(Counter(r["rule"] for r in refused)),
        **built,
        "banners": f".unbake/recipe/name/banners-{k}.json",
        "next": _SHIP.format(built["path"]) if built["moves"] else "recipe name next",
        **_land(config, params, built),
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


def run(config: Config, params: Json) -> Json:
    return {"next": next_, "submit": submit, "banners": banners}[params["action"]](config, params)
