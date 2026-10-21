"""What the project's headers and sources declare: the header list, the first header declaration of each name, the
externs a source makes, the units a header reaches, and the names declared more than one way."""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator
from functools import partial
from typing import Any

from unbake import api
from unbake.contracts import Finding, Snapshot, digest

from recipemaker import world

_SOURCE = (".c", ".cpp")
_HEADER = (".h", ".hpp")


def _blank(text: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)


def is_source(path: str) -> bool:
    """A C source by suffix, case aside (the suffixes of unbake's unit contract; a test keeps them equal)."""
    return path.lower().endswith(_SOURCE)


def is_header(path: str) -> bool:
    """A header by suffix, case aside."""
    return path.lower().endswith(_HEADER)


def _kind_of(suffix: str) -> Callable[[str], bool]:
    """ ".c" any source (.c, .cpp, any case), ".h" any header, else that exact suffix."""
    if suffix in (".c", ".h"):
        endings = _SOURCE if suffix == ".c" else _HEADER
        return lambda path: path.lower().endswith(endings)
    return lambda path: path.endswith(suffix)


def sources(snapshot: Snapshot, place: str = "include", suffix: str = ".h") -> list[str]:
    """The project files of one kind under a folder, with the snapshot's overlays laid over the disk."""
    root = snapshot.config.project.root
    kind = _kind_of(suffix)

    def listed() -> list[str]:
        return sorted(r for p in (root / place).rglob("*") if p.is_file() and kind(r := p.relative_to(root).as_posix()))

    paths = set(api.memo(("recipe.sources", str(root), place, suffix, snapshot.digest), listed))
    paths.update(p for p in snapshot.overlays if p.startswith(place + "/") and kind(p))
    return sorted(p for p in paths if p not in snapshot.overlays or snapshot.overlays[p] is not None)


_QUOTED_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)


def included_by(snapshot: Snapshot, changed: Collection[str] = ()) -> dict[str, set[str]]:
    """Per file the files that include it directly; a quoted name is found beside the includer, under include/ or
    under src/."""

    def graph() -> dict[str, set[str]]:
        known = {*sources(snapshot), *world.of(snapshot).groups, *changed}
        found: dict[str, set[str]] = defaultdict(set)
        seen: dict[str, bytes | None] = {}

        def read(path: str) -> bytes | None:
            if path not in seen:
                seen[path] = snapshot.peek(path)
            return seen[path]

        pending = sorted(known)
        while pending:
            path = pending.pop()
            for name in _QUOTED_INCLUDE.findall((read(path) or b"").decode(errors="replace")):
                for base in (posixpath.dirname(path), "include", "src"):
                    target = posixpath.normpath(posixpath.join(base, name))
                    if target in known or read(target) is not None:
                        found[target].add(path)
                        if target not in known:
                            known.add(target)
                            pending.append(target)
        return found

    return api.memo(("recipe.included-by", snapshot.digest, tuple(sorted(changed))), graph)


def consumers(snapshot: Snapshot, changed: Collection[str]) -> tuple[str, ...]:
    """Every module that includes a changed header directly or through others."""
    if not changed:
        return ()
    direct = included_by(snapshot, changed)
    reached: set[str] = set()
    todo = list(changed)
    while todo:
        new = direct.get(todo.pop(), set()) - reached
        reached |= new
        todo.extend(new)
    modules = world.of(snapshot).groups
    return tuple(sorted(p for p in reached if p in modules))


_DIRECTIVE = r"^[ \t]*#(?:[^\n]*\\\n)*[^\n]*"  # a preprocessor line with its backslash continuations


def _first_branches(blanked: str, clean: str) -> str:
    """The clean text with the brackets of every #elif and #else branch blanked: each branch of a conditional opens
    and closes the same brackets, so only the first one is counted."""
    out: list[str] = []
    at, stack = 0, list[bool]()
    for m in re.finditer(_DIRECTIVE, blanked, re.M):
        piece = clean[at : m.start()]
        out.append(re.sub(r"[{}()]", " ", piece) if any(stack) else piece)
        word = m[0].strip()
        if re.match(r"#\s*if", word):
            stack.append(False)
        elif re.match(r"#\s*(?:elif|else)\b", word) and stack:
            stack[-1] = True
        elif re.match(r"#\s*endif\b", word) and stack:
            stack.pop()
        out.append(clean[m.start() : m.end()])
        at = m.end()
    out.append(re.sub(r"[{}()]", " ", clean[at:]) if any(stack) else clean[at:])
    return "".join(out)


def statements(text: str) -> Iterator[tuple[int, int, str]]:
    """(start, end, text) of each file-scope statement; a function definition is none."""
    blanked = _blank(text)
    clean = re.sub(_DIRECTIVE, lambda m: re.sub(r"[^\n]", " ", m[0]), blanked, flags=re.M)
    clean = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', lambda m: re.sub(r"[^\n]", " ", m[0]), clean)
    counted = _first_branches(blanked, clean) if "#" in blanked else clean
    start, braces, parens = None, 0, 0
    for i, char in enumerate(counted):
        if start is None and not clean[i].isspace():
            start = i
        parens += (char == "(") - (char == ")")
        braces += (char == "{") - (char == "}")
        if char == "}" and braces == 0 and start is not None:
            prefix = clean[start : clean.find("{", start, i)]
            if prefix.rstrip().endswith(")") and "=" not in prefix:  # a definition, never a record
                start = None
        if char == ";" and not braces and not parens and start is not None:
            yield start, i + 1, clean[start : i + 1]
            start = None


def _parts(text: str) -> Iterator[str]:
    start = depth = 0
    for i, char in enumerate(text):
        depth += (char in "([{") - (char in ")]}")
        if char == "," and not depth:
            yield text[start:i]
            start = i + 1
    yield text[start:]


# a pointer declarator `(*name)`, else a plain name, its arrays and parameters
_DECLARATOR = re.compile(r"\(\s*\*\s*(\w+)\s*\)|\b(\w+)\s*(?:\[[^]]*\]\s*)*(?:\((?!\s*\*)[^{};]*\))?\s*$")
_DEFINITION = re.compile(
    r"^([ \t]*)(?!extern\b|typedef\b|return\b)((?:[\w]+[ \t*]+)+)(\w+)[ \t]*((?:\[[^\]]*\][ \t]*)*)[=;]", re.M
)
_EXTERN = re.compile(r"^[ \t]*extern\b[^;{}]*;", re.M)
_FUNCTION = re.compile(r"^[A-Za-z_][\w \t*]*?\b(\w+)\s*\(([^;{}]*)\)\s*\{", re.M)
_SYNONYMS = {
    "f32": "float",
    "f64": "double",
    "s8": "signed char",
    "u8": "unsigned char",
    "s16": "short",
    "u16": "unsigned short",
    "s32": "int",
    "u32": "unsigned int",
    "s64": "long long",
    "u64": "unsigned long long",
    "const": "",
    "volatile": "",
    "extern": "",
}


def _declared(text: str) -> list[tuple[str, int, str]]:
    """(name, line, statement) for every declaration one text makes."""
    rows: list[Any] = []
    for start, end, declaration in statements(text):
        names = [f"{kind} {tag}" for kind, tag in re.findall(r"\b(struct|union|enum)\s+(\w+)\s*\{", declaration)]
        if re.search(r"\b(?:extern|typedef)\b", declaration):
            for part in _parts(declaration.rstrip(";")):
                match = _DECLARATOR.search(part)
                if match:
                    names.append(match[1] or match[2])
        elif "{" not in declaration:
            match = re.search(r"\b(\w+)\s*\([^;]*\)\s*;", declaration)
            if match:
                names.append(match[1])
        rows.extend((name, text.count("\n", 0, start) + 1, " ".join(_blank(text[start:end]).split())) for name in names)
    return rows


def _scan(text: str, source: bool) -> dict[str, Any]:
    """What one file declares and defines; a pure function of its text."""
    clean = _blank(text)
    found = list(_DEFINITION.finditer(clean)) if source else []

    def line(at: int) -> int:
        return clean.count("\n", 0, at) + 1

    externs = [(f, r) for f in _EXTERN.finditer(clean) for r in _declared(f[0])]
    return {
        "declared": [(r[0], line(f.start()), r[2]) for f, r in externs] if source else _declared(text),
        "defined": [f[3] for f in found],
        "defs": [(f[3], line(f.start()), f"{f[2]} @{f[4]}") for f in found if not f[1]],
        "functions": (
            [(f[1], line(f.start()), " ".join(f[0].split()).rstrip("{ ")) for f in _FUNCTION.finditer(clean)]
            if source
            else []
        ),
        "words": frozenset(re.findall(r"\w+", text)),
    }


def _scanned(snapshot: Snapshot, paths: list[str]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        data = snapshot.read(path)
        source = _kind_of(".c")(path)
        out.append(api.memo(("recipe.scan", digest(data), source), partial(_scan, data.decode(), source)))
    return out


def _key(snapshot: Snapshot, paths: list[str]) -> str:
    return digest([(p, digest(snapshot.read(p))) for p in paths])


def catalog(snapshot: Snapshot) -> dict[str, tuple[str, int, str]]:
    """Per name the first header declaration: (path, line, statement)."""
    paths = sources(snapshot)

    def produce() -> dict[str, tuple[str, int, str]]:
        result: dict[str, tuple[str, int, str]] = {}
        for path, scanned in zip(paths, _scanned(snapshot, paths), strict=True):
            for name, line, text in scanned["declared"]:
                result.setdefault(name, (path, line, text))
        return result

    return api.memo(("recipe.catalog", _key(snapshot, paths)), produce)


def externs(snapshot: Snapshot) -> dict[str, list[tuple[str, int, str]]]:
    """Per name the (path, line, type) of every extern statement a source makes of it."""
    paths = sources(snapshot, "src", ".c")

    def produce() -> dict[str, list[tuple[str, int, str]]]:
        found: defaultdict[str, list[tuple[str, int, str]]] = defaultdict(list)
        for path, scanned in zip(paths, _scanned(snapshot, paths), strict=True):
            for name, line, text in scanned["declared"]:
                found[name].append((path, line, _type_of(text, name).replace("@", "").strip()))
        return dict(found)

    return api.memo(("recipe.externs", _key(snapshot, paths)), produce)


def _type_of(statement: str, name: str) -> str:
    """The type one declaration gives a name: its specifiers, the stars before it, and what follows it."""
    base: str | None = None
    for part in _parts(statement.rstrip(";")):
        match = _DECLARATOR.search(part)
        if match and base is None:
            base = part[: match.start()].rstrip("* ").strip()
        if match and name in (match[1], match[2]):
            tail = re.search(r"[*\s]*$", part[: match.start()])
            stars = tail[0] if tail else ""
            declarator = match[0].replace(name, "@", 1) + (part[match.end() :] if match[1] else "")
            return " ".join(f"{(base or '').removeprefix('extern')} {stars} {declarator}".split())
    return ""


_TYPE_WORDS = {"const", "volatile", "unsigned", "signed", "struct", "union", "enum", "int", "char", "short", "long"}
_TYPE_WORDS |= {"void", "float", "double"}


def _shape(spelled: str) -> str:
    return re.sub(r"\[[^\]]*\]", "[]", _normal(spelled)).replace("static ", "").strip()


def _unnamed(spelled: str) -> str:
    """A function type without its parameter names: `@(s32 a, s32 *b)` and `@(s32, s32*)` are one type."""
    opener = "@(" if "@(" in spelled else "@)(" if "@)(" in spelled else ""
    if not opener:
        return spelled
    head, _, rest = spelled.partition(opener)
    kept = []
    for param in (p.strip() for p in rest.rpartition(")")[0].split(",")):
        named = re.search(r"(\w+)\s*$", param)
        kept.append(param[: named.start()] if named and named.start() and named[1] not in _TYPE_WORDS else param)
    return re.sub(r"\s+|\bvoid\b(?=\))|\b(?:struct|union|enum)\b", "", head + opener + ",".join(kept) + ")")


def _normal(spelled: str) -> str:
    return " ".join(re.sub(r"\b\w+\b", lambda w: _SYNONYMS.get(w[0], w[0]), spelled).split())


def disagreements(snapshot: Snapshot) -> tuple[dict[str, str], list[Finding]]:
    """The one type each declared name has where every declaration agrees (and no source defines it), and a
    `types.conflict` finding per name whose declarations disagree; a definition is canonical, and its finding's
    symptoms hold {definition: {path, line, signature}, consumers}."""
    paths = [*sources(snapshot), *sources(snapshot, "src", ".c")]
    return api.memo(("recipe.disagreements", _key(snapshot, paths)), lambda: _rows(paths, _scanned(snapshot, paths)))


def _rows(paths: list[str], scans: list[dict[str, Any]]) -> tuple[dict[str, str], list[Finding]]:
    found: dict[str, list[tuple[str, int, str]]] = {}
    owned: set[str] = set()
    definitions: dict[str, tuple[str, int, str]] = {}
    variables: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for path, scanned in zip(paths, scans, strict=True):
        owned.update(scanned["defined"])
        definitions.update({name: (path, line, text) for name, line, text in scanned["functions"]})
        for name, line, text in scanned["defs"]:
            variables[name].append((path, line, text))
        for name, line, text in scanned["declared"]:
            if not text.startswith("typedef") and not re.match(r"(?:struct|union|enum)\b[^;(]*\{", text):
                found.setdefault(name, []).append((path, line, text))
    agreed: dict[str, str] = {}
    findings: list[Finding] = []
    for name, rows in sorted(variables.items()):
        spelled = {_shape(t) for _, _, t in rows} | {_shape(_type_of(t, name)) for _, _, t in found.get(name, ())}
        if len(spelled) > 1:
            shown = (
                *(f"{name}: {p}:{n} {' '.join(t.replace('@', name).split())}" for p, n, t in rows),
                *(f"{name}: {p}:{n} {t}" for p, n, t in found.get(name, ())),
            )
            findings.append(
                Finding(
                    "types.conflict",
                    f"{name} is defined or declared {len(spelled)} ways",
                    path=rows[0][0],
                    line=rows[0][1],
                    unit=name,
                    missing=shown,
                    blocking=False,
                    action="declare and define it with one type",
                )
            )
    words = [s["words"] for s in scans]
    for name, rows in sorted(found.items()):
        if name in definitions:
            path, line, text = definitions[name]
            canonical = _unnamed(_normal(_type_of(text, name)))
            if canonical and any(_unnamed(_normal(_type_of(t, name))) != canonical for _, _, t in rows):
                excluded = {p for p, _, _ in rows} | {path}
                findings.append(
                    Finding(
                        "types.conflict",
                        f"{name} is defined as {text} but declared another way",
                        path=path,
                        line=line,
                        unit=name,
                        missing=tuple(f"{name}: {p}:{n} {t}" for p, n, t in [definitions[name], *rows]),
                        blocking=False,
                        action=f"declare it as its definition in {path} does",
                        symptoms={
                            "definition": {"path": path, "line": line, "signature": text},
                            "consumers": sum(
                                name in w and p not in excluded for p, w in zip(paths, words, strict=True)
                            ),
                        },
                    )
                )
            continue
        if name in owned:
            continue
        spellings = [_type_of(text, name) for _, _, text in rows]
        if len({_normal(s) for s in spellings}) == 1 and "" not in spellings:
            agreed[name] = spellings[0]
        else:
            findings.append(
                Finding(
                    "types.conflict",
                    f"{name} is declared {len({_normal(s) for s in spellings})} ways and defined nowhere",
                    unit=name,
                    missing=tuple(f"{name}: {path}:{line} {text}" for path, line, text in rows),
                    blocking=False,
                    action="declare it once, with one type, in a shared header",
                )
            )
    return agreed, findings


def normal(text: str) -> str:
    """C text pycparser reads: attributes blanked, line-marker flags dropped, GNU spellings made plain."""
    while match := re.search(r"\b__attribute__\s*\(", text):
        end, depth = match.end(), 1
        while end < len(text) and depth:
            depth += (text[end] == "(") - (text[end] == ")")
            end += 1
        text = text[: match.start()] + re.sub(r"[^\n]", " ", text[match.start() : end]) + text[end:]
    text = re.sub(r'^(# \d+ "[^"\n]*")(?: \d+)+[ \t]*$', r"\1", text, flags=re.M)
    text = re.sub(r"\b(?:__extension__|__restrict)\b", "", text)
    return re.sub(r"\b(?:__inline__|__inline)\b", "inline", text)
