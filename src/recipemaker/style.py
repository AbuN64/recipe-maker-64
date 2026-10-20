"""`recipe style`: the clean-code rules as findings per file and a census per rule with a ratchet. Facts are read once
per file content (a parse cache keyed by the text digest), and the rules run on the facts, so a rule change reparses
nothing."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Json, sha256

from recipemaker import cparse, host, table

FACTS = 3  # the version of what _facts reads: bump it and every file is read again
SEVERITY = {"m2c-local": "warning"}
RULES = (
    *("function-name", "global-name", "static-name", "type-name", "macro-name", "banned-word", "file-name"),
    *("dir-name", "collision-suffix", "files-per-dir", "tool-named-path", "header-order", "guard-tool"),
    *("include-order", "banner-name", "banner-address", "version-word", "m2c-local"),
)
_KINDS = {"function": "function-name", "global": "global-name", "static": "static-name", "type": "type-name"}
_MARKUP = re.compile(r"/\*.*?\*/|//[^\n]*|^[ \t]*#(?:\\\n|[^\n])*", re.S | re.M)
_PIECE = re.compile(r"[{};]")
_BANNER = re.compile(r"/\*\s*\n=+\n(\w+)\n(.*?)\n=+\n\*/", re.S)
_AFTER = re.compile(r"\s*(?:[\w*]+\s+)*?\**(\w+)\s*\(")
_ADDRESS = re.compile(r"^(\S+) ([0-9A-F]{8})$")
_GUARD = re.compile(r"^[ \t]*#[ \t]*ifndef[ \t]+(\w+)[ \t]*\n[ \t]*#[ \t]*define[ \t]+\1\b", re.M)
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


def _rules() -> Json:
    return table("knowledge/style.toml")


def neutral(name: str) -> bool:
    return any(re.fullmatch(p, name) for p in _rules()["names"]["neutral"])


def neutral_module(stem: str) -> bool:
    """Whether a module's file stem is a structure name, not a human one: a neutral form in any case (CODE_80426EC4
    as code_80426EC4) or a bare address with an optional counter (80423280_4)."""
    folded = re.sub(r"^(CODE|DATA)_", lambda m: m[1].lower() + "_", stem)
    return neutral(stem) or neutral(folded) or bool(re.fullmatch(r"[0-9A-F]{8}(?:_\d+)?", stem))


def folder_unnamed(folder: str) -> bool:
    """Whether a folder has no name yet: a neutral address form or a word with a collision counter (FX_2).
    The one fact both the subsystem row (it may be named) and the function and module rows (they must wait) read."""
    return neutral(folder) or bool(re.fullmatch(_rules()["names"]["placeholder_folder"], folder))


def subsystem_word(folder: str) -> str:
    """The subsystem word of a folder: its name folded to the convention's case, or empty for a neutral folder,
    a folder that is no subsystem word, or a folder the name rules leave alone. SCHED and sched give `sched`."""
    rules = _rules()
    word = folder.lower()
    ok = re.fullmatch(rules["names"]["subsystem"], word) and word not in rules["paths"]["no_prefix"]
    return word if ok and not folder_unnamed(folder) and not neutral(word) else ""


def prefix_word(folder: str) -> str:
    """The word a folder's functions and modules take their prefix from: its subsystem word, and for a placeholder
    (RENDER_3) the word before its collision counter. Structure gave the placeholder that word from the same evidence
    as RENDER, so Render_ is true of its code; naming the folder later carries the prefix to the new word."""
    if word := subsystem_word(folder):
        return word
    counted = re.fullmatch(r"([A-Za-z]+)_\d{1,3}", folder)
    return subsystem_word(counted[1]) if counted and not neutral(folder) else ""


def free_folder(folder: str) -> bool:
    """Whether a folder's functions and modules take no prefix: a folder that is no subsystem word (GAME), or a
    placeholder of one (GAME_2). The audit reads no prefix there either (`_words`)."""
    placeholder = not neutral(folder) and re.fullmatch(_rules()["names"]["placeholder_folder"], folder)
    base = re.sub(r"_\d+$", "", folder) if placeholder else folder
    return base.lower() in _rules()["paths"]["no_prefix"]


def pascal(word: str) -> str:
    return word[:1].upper() + word[1:]


def pattern(kind: str, word: str = "") -> str:
    """The regex of a name kind, for a subsystem word (`{dir}` and `{Prefix}`); a function or module of no word (a
    folder that takes no prefix) has the unprefixed form."""
    names = _rules()["names"]
    bare = kind in ("function", "module") and not word
    key = f"unprefixed_{kind}" if bare or (kind == "function" and word in _rules()["unprefixed"]) else kind
    return str(names[key]).replace("{Prefix}", pascal(word)).replace("{dir}", word)


def legal(kind: str, name: str, word: str = "") -> bool:
    return neutral(name) or name in _rules()["allow"] or bool(re.fullmatch(pattern(kind, word), name))


def collision(stem: str) -> bool:
    """Whether a file stem ends in a collision counter; the VRAM of a neutral name is no counter."""
    return bool(re.search(_rules()["names"]["forbidden_suffix"], re.sub(r"^(?:code|data)_[0-9A-F]{8}", "", stem)))


def banned(name: str, kind: str = "function") -> str:
    """The banned word or bare verb a name holds, else an empty string."""
    rules = _rules()
    tail = name.split("_", 1)[-1] if kind == "function" else ""
    if tail in rules["bare_verbs"]:
        return tail
    words = [w for part in name.split("_") for w in _WORDS.findall(part)]
    return next((w for w in words if w.capitalize() in rules["banned"]), "")


def _blank(text: str) -> str:
    """The text with comments and preprocessor lines blanked to spaces, so offsets still match the original."""
    return _MARKUP.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), text)


def _chunks(clean: str) -> Iterator[tuple[int, str, str]]:
    """(offset, head, body) per top-level statement; the head has every brace body replaced by `{}`."""
    depth, start, head, body, first = 0, 0, "", "", 0
    for found in _PIECE.finditer(clean):
        char, at = found[0], found.start()
        if depth == 0 and char != "}":
            head += clean[start:at]
            first = first or at
        if char == "{":
            depth += 1
            if depth == 1:
                start, body = at + 1, ""
        elif char == "}":
            depth = max(depth - 1, 0)
            if depth == 0:
                body, head = body + clean[start:at], head + "{}"
                start = at + 1
                if re.search(r"\)\s*\{\}$", head):  # a function definition ends at its brace
                    yield first, " ".join(head.split()), body
                    head, body, first = "", "", 0
        elif depth == 0:
            yield first, " ".join(head.split()), body
            start, head, body, first = at + 1, "", "", 0


_RANK = {"include": 1, "macro": 2, "type": 3, "extern": 4, "prototype": 5}
_STATEMENT = ("if", "while", "for", "switch", "return")


def _disordered(order: Sequence[int]) -> bool:
    return any(rank < max(order[:i], default=0) for i, rank in enumerate(order))


def _statement(head: str, body: str, facts: dict[str, Any], ranks: list[tuple[int, int]], at: int) -> None:
    """Classify one top-level statement as a type, a function (defined or declared) or a variable."""
    static = bool(re.search(r"\bstatic\b", head))
    if re.fullmatch(r"(?:struct|union|enum) ?(?:\{\})?", head):  # an anonymous body: enumerators, no name
        return
    tag = re.match(r"(?:struct|union|enum) (\w+) ?(?:\{\})?$", head)
    if head.startswith("typedef") or tag:
        alias = re.search(r"\(\s*\*\s*(\w+)\s*\)", head) or re.search(r"(\w+)\s*(?:\[[^\]]*\])?$", head)
        found = (alias if head.startswith("typedef") else tag) or alias
        facts["types"] += [found[1]] if found else []
        ranks.append((at, _RANK["type"]))
        return
    paren, equals = head.find("("), head.find("=")
    if paren > 0 and (equals < 0 or paren < equals) and not re.match(r"\(\s*\*", head[paren:]):
        name = re.search(r"(\w+)\s*$", head[:paren])
        if name and not head.startswith(_STATEMENT) and not re.fullmatch(r"_?[A-Z][A-Z0-9_]+", name[1]):
            facts["fns"].append([name[1], static, head.endswith("{}")])
            facts["locals"] |= set(re.findall(_rules()["comments"]["m2c_local"], body))
            ranks.append((at, _RANK["prototype"]))
        return
    shape = head.split("=")[0]
    name = re.search(r"\(\s*\*\s*(\w+)\s*\)", shape) or re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*(?:\{\})?$", shape)
    if name and not head.startswith("}"):
        facts["vars"].append([name[1], static, head.startswith("extern")])
        ranks.append((at, _RANK["extern"]))


def _facts(text: str) -> Json:
    """Everything the rules need from one file; it depends on the text alone."""
    clean, notes = _blank(text), _rules()["comments"]["version_note"]
    facts: dict[str, Any] = {"fns": [], "vars": [], "types": [], "locals": set()}
    ranks: list[tuple[int, int]] = []
    for at, head, body in _chunks(clean):
        _statement(head, body, facts, ranks, at)
    structs, plain = cparse.scan(text)
    named = (alias or tag for alias, tag, *_ in structs)
    types = {*facts["types"], *(n for n in named if n and n.isidentifier()), *(n for n, _ in plain)}
    guard = _GUARD.search(text)
    defines = [m for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w+)", text, re.M) if not guard or m[1] != guard[1]]
    ranks += [(m.start(), _RANK["include"]) for m in re.finditer(r"^[ \t]*#[ \t]*include\b", text, re.M)]
    ranks += [(m.start(), _RANK["macro"]) for m in defines]
    banners = []
    for found in _BANNER.finditer(text):
        after, address = _AFTER.match(clean, found.end()), _ADDRESS.match((found[2].strip().splitlines() or [""])[-1])
        banners.append(
            [found[1], address[1] if address else "", address[2] if address else "", after[1] if after else ""]
        )
    return {
        "fns": facts["fns"],
        "vars": facts["vars"],
        "types": sorted(types),
        "macros": [m[1] for m in defines],
        "guard": guard[1] if guard else "",
        "includes": re.findall(r'^[ \t]*#[ \t]*include[ \t]+[<"]([^>"]+)[>"]', text, re.M),
        "disorder": _disordered([rank for _, rank in sorted(ranks)]),
        "banners": banners,
        "notes": [m[1] for c in re.finditer(r"/\*.*?\*/", text, re.S) if (m := re.match(notes, c[0]))],
        "m2c": len(facts["locals"]),
    }


class Context:
    """What the project's versions say about banners: their names and, per symbol, the VRAM in each. The symbol tables
    (`api.symbols`) are read once, and only when a file has a banner address."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.versions: set[str] = set(config.project.versions)
        self._tables: dict[str, Any] | None = None

    def known(self) -> set[str]:
        return self.versions

    def placed(self, name: str) -> dict[str, str] | None:
        """Per version the VRAM of a symbol as hex, or None when no version has the symbol."""
        if self._tables is None:
            self._tables = {v: api.symbols(self.config, v) for v in sorted(self.versions)}
        found = {v: f"{syms[name][0]:08X}" for v, syms in self._tables.items() if name in syms}
        return found or None


def _words(path: str) -> list[str]:
    """The subsystem words a file can take its prefix from: its folder, and for a header its own stem."""
    rules = _rules()
    parts = PurePosixPath(path)
    found = [parts.parent.name, *([parts.stem] if parts.suffix == ".h" else [])]
    return [w for w in found if w not in rules["paths"]["no_prefix"] and re.fullmatch(rules["names"]["subsystem"], w)]


def _names(facts: Json, words: Sequence[str]) -> Iterator[tuple[str, str]]:
    for name, static, defined in facts["fns"]:
        if words and not any(legal("function", name, w) for w in words):
            yield "function-name", name
        if (defined or static) and (word := banned(name)) and not neutral(name):
            yield "banned-word", f"{name} ({word})"
    for name, static, _ in facts["vars"]:
        kind = "static" if static else "global"
        if not legal(kind, name):
            yield _KINDS[kind], name
    yield from (("type-name", n) for n in facts["types"] if not legal("type", n))
    yield from (("macro-name", n) for n in facts["macros"] if not legal("macro", n))


def check(path: str, facts: Json, context: Context) -> list[tuple[str, str, str]]:
    """The findings (rule, path, detail) of one file from its facts."""
    rules, out = _rules(), []
    where = PurePosixPath(path)
    if where.suffix not in (".c", ".h") or any(path.startswith(x) for x in rules["paths"]["exempt"]):
        return []
    words = _words(path)
    out += [(rule, path, detail) for rule, detail in _names(facts, words)]
    if where.suffix == ".c" and words and not neutral(where.stem) and not legal("module", where.stem, words[0]):
        out.append(("file-name", path, where.stem))
    folder = where.parent.name
    if where.suffix == ".c" and folder not in [*rules["paths"]["no_prefix"], ""] and not words and not neutral(folder):
        out.append(("dir-name", str(where.parent), folder))
    out += [("collision-suffix", path, where.stem)] * collision(where.stem)
    out += [("guard-tool", path, facts["guard"])] * (rules["comments"]["guard_tool"] in facts["guard"])
    out += [("header-order", path, "")] * (where.suffix == ".h" and facts["disorder"])
    if where.suffix == ".c":
        own = next((i for i in facts["includes"] if PurePosixPath(i).stem in (where.parent.name, where.stem)), None)
        rest = sorted({i for i in facts["includes"] if i != own})
        if facts["includes"] != [*([own] if own else []), *rest]:
            out.append(("include-order", path, ""))
    for name, version, vram, defined in facts["banners"]:
        out += [("banner-name", path, f"{name} above {defined}")] * bool(defined and defined != name)
        placed = context.placed(defined) if vram and defined else None
        if placed is not None and placed.get(version) != vram:
            out.append(("banner-address", path, f"{defined} {version} {vram}"))
    for word in facts["notes"]:
        if word not in rules["comments"]["not_versions"] and (known := context.known()) and word not in known:
            out.append(("version-word", path, word))
    out += [("m2c-local", path, str(facts["m2c"]))] * bool(facts["m2c"])
    return out


def _listing(config: Config, paths: Sequence[str]) -> list[str]:
    """Tracked files, or the files under the given paths, as project-relative POSIX paths."""
    root = config.project.root
    if not paths:
        return sorted(host.git(config, "ls-files").stdout.decode(errors="replace").splitlines())
    found: set[str] = set()
    for given in paths:
        base = Path(given) if Path(given).is_absolute() else root / given
        files = [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else [base]
        found |= {p.relative_to(root).as_posix() for p in files if ".git" not in p.parts and ".unbake" not in p.parts}
    return sorted(found)


def _cache(root: Path) -> tuple[Path, dict[str, Any]]:
    path = root / ".unbake/recipe/style-cache.json"
    try:
        return path, dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return path, {}


def gather(config: Config, files: Sequence[str]) -> tuple[dict[str, Json], int]:
    """Facts per C file through the digest cache; only the unseen contents go to the pool. Returns the facts and the
    number of files parsed."""
    root = config.project.root
    texts = {f: (root / f).read_text(encoding="utf-8", errors="replace") for f in files if f.endswith((".c", ".h"))}
    keys = {f: sha256(f"{FACTS}\n{t}".encode()) for f, t in texts.items()}
    path, cache = _cache(root)
    todo = sorted({keys[f]: texts[f] for f in texts if keys[f] not in cache}.items())
    if todo:
        done = api.pool_map(config, "recipe.style", _facts, [text for _, text in todo])
        cache.update({key: d for (key, _), d in zip(todo, done, strict=True)})
        host.write(path, json.dumps(cache, sort_keys=True).encode())
    return {f: cache[k] for f, k in keys.items()}, len(todo)


def findings(config: Config, paths: Sequence[str] = ()) -> tuple[list[tuple[str, str, str]], int, int]:
    """All findings over the repo or the given paths, the number of files read and the number parsed."""
    rules, listing = _rules(), _listing(config, paths)
    facts, parsed = gather(config, listing)
    context, out = Context(config), []
    for path, found in sorted(facts.items()):
        out += check(path, found, context)
    folders: Counter[str] = Counter(str(PurePosixPath(f).parent) for f in listing)
    out += [("files-per-dir", d, str(n)) for d, n in sorted(folders.items()) if n > rules["dir_max"]]
    out += [
        ("tool-named-path", f, "")
        for f in listing
        if re.search(rules["paths"]["tool_named"], f) and not f.startswith("src/fuzzy/")
    ]
    stems: defaultdict[str, list[str]] = defaultdict(list)
    for f in listing:
        if f.endswith(".c") and not any(f.startswith(x) for x in rules["paths"]["exempt"]) and _words(f):
            stems[PurePosixPath(f).name].append(f)
    out += [("file-name", f, "basename is not unique") for same in stems.values() if len(same) > 1 for f in same]
    return out, len(listing), parsed


def run(config: Config, params: Json) -> Json:
    """`recipe style`: findings for the paths, or with --census the count per rule against the saved baseline."""
    found, files, parsed = findings(config, params["paths"])
    census = {rule: sum(1 for f in found if f[0] == rule) for rule in RULES}
    base = config.project.root / ".unbake/recipe/style-baseline.json"
    saved: Json = json.loads(base.read_text(encoding="utf-8")) if base.is_file() else {}
    rose = {r: [saved.get(r, 0), n] for r, n in census.items() if r in saved and n > saved[r]}
    wrote = bool(params["baseline"])
    if wrote:
        host.write(base, json.dumps(census, sort_keys=True).encode())
    mode = "census" if params["census"] or wrote else "findings"
    errors = [f for f in found if SEVERITY.get(f[0], "error") == "error"]
    bad = bool(rose) if mode == "census" else bool(errors)
    return {
        "ok": not bad,
        "exit": int(bad),
        "mode": mode,
        "files": files,
        "parsed": parsed,
        "census": census,
        "rose": rose,
        "baseline": wrote,
        "findings": [] if mode == "census" else [{"rule": r, "path": p, "detail": d} for r, p, d in found],
        "next": "recipe style --baseline" if mode == "census" and not saved else "recipe status",
    }
