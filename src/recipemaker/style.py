"""`recipe style`: the clean-code rules as findings per file and a census per rule with a ratchet. Facts are read once
per file content (a parse cache keyed by the text digest), and the rules run on the facts, so a rule change reparses
nothing."""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Json, sha256

from recipemaker import cparse, debug, host, table

FACTS = 3  # the version of what _facts reads: bump it and every file is read again
SEVERITY = {"m2c-local": "warning"}
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
    regular = str(names[key]).replace("{Prefix}", pascal(word)).replace("{dir}", word)
    if (
        kind == "function" and word in _rules()["unprefixed"]
    ):  # also the prefixed form: a builtin's wrapper is Math_Sqrtf
        prefixed = str(names["function"]).replace("{Prefix}", pascal(word)).replace("{dir}", word)
        regular = f"(?:{regular}|{prefixed})"
    return f"(?:{regular}|{names['class_function']})" if kind == "function" else regular


def legal(kind: str, name: str, word: str = "") -> bool:
    return not api.name_issues(name) and (
        neutral(name) or name in _rules()["allow"] or bool(re.fullmatch(pattern(kind, word), name))
    )


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
        "disorder": (order := [rank for _, rank in sorted(ranks)]) != sorted(order),
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
        self._original: set[str] | None = None

    def known(self) -> set[str]:
        return self.versions

    def original(self, name: str) -> bool:
        """Whether the debug catalog (the original developers' records) spells this type, function or datum so: the
        original spelling is the style whatever the pattern says (CGameAnimateState_t)."""
        if self._original is None:
            catalog = debug.load(self.config)
            self._original = (
                set()
                if catalog is None
                else {
                    *(n for r in catalog.records for n in (r.name, debug.tag_of(r))),
                    *(f.name for f in (*catalog.functions, *catalog.globals)),
                }
            )
        return name in self._original

    def placed(self, name: str) -> dict[str, str] | None:
        """Per version the VRAM of a symbol as hex, or None when no version has the symbol."""
        if self._tables is None:
            self._tables = {v: api.symbols(self.config, v) for v in sorted(self.versions)}
        found = {v: f"{syms[name][0]:08X}" for v, syms in self._tables.items() if name in syms}
        return found or None


_SUFFIXES = {".c": "c", ".cpp": "c", ".h": "h"}


def kind(path: str | PurePosixPath) -> str:
    """The one discovery rule: "c" for a source, "h" for a header, "" for anything the audit does not read. The
    suffix is read in any letter case (RW spells .CPP and .H)."""
    return _SUFFIXES.get(PurePosixPath(path).suffix.lower(), "")


def _words(path: str) -> list[str]:
    """The subsystem words a file can take its prefix from: its folder, and for a header its own stem."""
    parts = PurePosixPath(path)
    found = [parts.parent.name, *([parts.stem] if kind(parts) == "h" else [])]
    return list(dict.fromkeys(word for folder in found if (word := prefix_word(folder))))


def _why(name: str, words: Sequence[str]) -> str:
    """Why a function name is illegal in a file whose folder (or header) allows the prefixes `words`."""
    if issues := api.name_issues(name):
        return "; ".join(issues)
    return "must be " + " or ".join(f"{pascal(w)}_CamelCase" for w in words) + " (a class method CName_Method also passes)"


def _names(facts: Json, words: Sequence[str], context: Context, source: bool = False) -> Iterator[tuple[str, str]]:
    for name, static, defined in facts["fns"]:
        # an extern in a source names a function another subsystem's file defines (and is judged there)
        if (
            words
            and (defined or static or not source)
            and not any(legal("function", name, w) for w in words)
            and not context.original(name)
        ):
            yield "function-name", f"{name} ({_why(name, words)})"
        if (defined or static) and (word := banned(name)) and not neutral(name) and not context.original(name):
            yield "banned-word", f"{name} ({word})"
    for name, static, _ in facts["vars"]:
        kind = "static" if static else "global"
        if not legal(kind, name) and not context.original(name):
            yield _KINDS[kind], name
    yield from (("type-name", n) for n in facts["types"] if not legal("type", n) and not context.original(n))
    yield from (("macro-name", n) for n in facts["macros"] if not legal("macro", n))


def check(path: str, facts: Json, context: Context) -> list[tuple[str, str, str]]:
    """The findings (rule, path, detail) of one file from its facts."""
    rules, out = _rules(), []
    where = PurePosixPath(path)
    if not kind(where) or any(path.startswith(x) for x in rules["paths"]["exempt"]):
        return []
    words = _words(path)
    out += [(rule, path, detail) for rule, detail in _names(facts, words, context, kind(where) == "c")]
    if kind(where) == "c" and words and not neutral(where.stem) and not legal("module", where.stem, words[0]):
        out.append(("file-name", path, where.stem))
    folder = where.parent.name
    if (
        kind(where) == "c"
        and folder.lower() not in [*rules["paths"]["no_prefix"], ""]
        and not words
        and not neutral(folder)
    ):
        out.append(("dir-name", str(where.parent), folder))
    out += [("collision-suffix", path, where.stem)] * collision(where.stem)
    out += [("guard-tool", path, facts["guard"])] * (rules["comments"]["guard_tool"] in facts["guard"])
    out += [("header-order", path, "")] * (kind(where) == "h" and facts["disorder"])
    if kind(where) == "c":
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
    return _read(config, {f: (root / f).read_text(encoding="utf-8", errors="replace") for f in files if kind(f)})


def _read(config: Config, texts: dict[str, str]) -> tuple[dict[str, Json], int]:
    """Facts per path from its text through the digest cache, and the number of files parsed afresh."""
    root = config.project.root
    keys = {f: sha256(f"{FACTS}\n{t}".encode()) for f, t in texts.items()}
    path, cache = _cache(root)
    todo = sorted({keys[f]: texts[f] for f in texts if keys[f] not in cache}.items())
    if todo:
        done = api.pool_map(config, "recipe.style", _facts, [text for _, text in todo])
        cache.update({key: d for (key, _), d in zip(todo, done, strict=True)})
        host.write(path, json.dumps(cache, sort_keys=True).encode())
    return {f: cache[k] for f, k in keys.items()}, len(todo)


Found = list[tuple[str, str, str]]


def _across(listing: Sequence[str]) -> Found:
    """The findings that hold of a set of paths as a whole: crowded folders, tool-named paths, repeated basenames."""
    rules, out = _rules(), []
    folders: Counter[str] = Counter(str(PurePosixPath(f).parent) for f in listing)
    out += [("files-per-dir", d, str(n)) for d, n in sorted(folders.items()) if n > rules["dir_max"]]
    out += [
        ("tool-named-path", f, "")
        for f in listing
        if re.search(rules["paths"]["tool_named"], f) and not f.startswith("src/fuzzy/")
    ]
    stems: defaultdict[str, list[str]] = defaultdict(list)
    for f in listing:
        if kind(f) == "c" and not any(f.startswith(x) for x in rules["paths"]["exempt"]) and _words(f):
            stems[PurePosixPath(f).name].append(f)
    out += [("file-name", f, "basename is not unique") for same in stems.values() if len(same) > 1 for f in same]
    return out


def _listed(config: Config, *args: str) -> set[str]:
    """The NUL-separated paths a git command prints."""
    return set(host.git(config, *args).stdout.decode(errors="replace").split("\0"))


def findings(config: Config, paths: Sequence[str] = (), base: str = "") -> tuple[Found, Found, int, int, int]:
    """(new, debt, files read, files parsed afresh, files audited) over the repo or the given paths. Without a base
    every finding is new. With one, the findings the base commit's files already had are debt and only what the change
    introduces is new: an untouched file has the findings it had, a touched one is read at the base."""
    listing = _listing(config, paths)
    facts, parsed = gather(config, listing)
    context = Context(config)
    mine = {path: check(path, found, context) for path, found in sorted(facts.items())}
    now = [f for found in mine.values() for f in found] + _across(listing)
    if not base:
        return now, [], len(listing), parsed, len(facts)
    touched = _listed(config, "diff", "--name-only", "-z", base) | _listed(
        config, "ls-files", "-z", "--others", "--exclude-standard"
    )
    old = _listed(config, "ls-tree", "-r", "--name-only", "-z", base)
    texts = {}
    for path in sorted(touched & facts.keys() & old):
        shown = host.run(["git", "show", f"{base}:{path}"], config.project.root, config)
        texts[path] = bytes(shown.stdout).decode(errors="replace") if shown.exit == 0 else ""
    held, _ = _read(config, texts)
    before = Counter(f for path, found in mine.items() if path not in touched for f in found)
    before.update(f for path, found in held.items() for f in check(path, found, context))
    before.update(_across([f for f in listing if f in old]))
    new: Found = []
    debt: Found = []
    for f in now:
        (debt if before[f] > 0 else new).append(f)
        before[f] -= 1
    return new, debt, len(listing), parsed, len(facts)


def run(config: Config, params: Json) -> Json:
    """`recipe style`: the findings for the paths; exit 1 when one is an error."""
    found, debt, files, parsed, audited = findings(
        config, params["paths"], "" if params.get("full") else params.get("base", "")
    )
    if not audited:  # an audit that read no source proves nothing: it fails
        found.append(
            ("nothing-audited", ",".join(params["paths"]) or ".", f"{files} files listed, no C source or header")
        )
    bad = any(SEVERITY.get(f[0], "error") == "error" for f in found)
    return {
        "ok": not bad,
        "exit": int(bad),
        "files": files,
        "parsed": parsed,
        "audited": audited,
        "findings": [{"rule": r, "path": p, "detail": d} for r, p, d in found],
        "debt": [{"rule": r, "path": p, "detail": d} for r, p, d in debt],
        "next": "recipe name next",
    }
