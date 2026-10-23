"""Type definitions per source unit: which units cannot share one file because a type is defined differently in them.

A unit's definitions are the struct/union/enum bodies and the typedefs in its own text plus every header it includes.
Prototype and extern disagreements are not read here: unbake compose localizes them."""

import re
from collections.abc import Callable
from pathlib import Path

from recipemaker import records, world

Types = dict[str, str]
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_BODY = re.compile(r"\b(struct|union|enum)\s+(\w+)\s*\{")
_WORD = re.compile(r"[A-Za-z_]\w*")


def _typedefs(text: str) -> Types:
    out: Types = {}
    for found in re.finditer(r"\btypedef\b", text):
        at, depth = found.end(), 0
        while at < len(text) and not (text[at] == ";" and depth == 0):
            depth += (text[at] in "{(") - (text[at] in "})")
            at += 1
        statement = " ".join(text[found.start() : at].split())
        head = re.sub(r"\{.*\}", " ", statement)  # the name sits outside the body
        pointer = re.search(r"\(\s*\*\s*(\w+)\s*\)", head)
        names = _WORD.findall(re.sub(r"\[[^\]]*\]|\(.*\)", " ", head)) if not pointer else [pointer[1]]
        if names:
            out.setdefault(f"typedef {names[-1]}", statement)
    return out


def definitions(text: str) -> Types:
    """The tagged bodies and typedefs one text defines, whitespace and comments normalized."""
    text = _COMMENT.sub(" ", text)
    out: Types = {}
    for found in _BODY.finditer(text):
        out.setdefault(
            f"{found[1]} {found[2]}", " ".join(text[found.start() : records.after(text, found.end())].split())
        )
    for key, statement in _typedefs(text).items():
        out.setdefault(key, statement)
    return out


def reader(root: Path) -> Callable[[str], Types]:
    """`types(unit_path)` for the project at `root`, each header read once however many units include it. A file is
    parsed once and an include is resolved once per including folder, so a header reached again through another
    path costs a lookup (the suffix test is case-blind: a project may spell its headers `.H`)."""
    seen: dict[str, Types] = {}
    parsed: dict[str, tuple[list[str], Types] | None] = {}
    found: dict[tuple[str, str], str] = {}

    def find(name: str, parent: str) -> str:
        key = (name, str(Path(parent).parent))
        if key not in found:
            found[key] = ""
            for base in (Path(parent).parent, Path("include"), Path()):
                if (root / base / name).is_file():
                    found[key] = str(base / name)
                    break
        return found[key]

    def read(path: str) -> tuple[list[str], Types] | None:
        if path not in parsed:
            try:
                text = (root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                parsed[path] = None
            else:
                parsed[path] = (world.INCLUDE.findall(_COMMENT.sub(" ", text)), definitions(text))
        return parsed[path]

    def of(path: str, trail: tuple[str, ...] = ()) -> Types:
        if path in seen:
            return seen[path]
        own = read(path)
        if own is None:
            return {}
        out: Types = {}
        for name in own[0]:
            child = find(name, path)
            if child and child not in trail and child != path:
                for key, value in of(child, (*trail, path)).items():
                    out.setdefault(key, value)
        for key, value in own[1].items():
            out.setdefault(key, value)
        if not trail or path.lower().endswith(".h"):
            seen[path] = out
        return out

    return of


def conflicts(left: Types, right: Types) -> list[str]:
    """The type names the two definition sets spell differently."""
    return sorted(k for k in left.keys() & right.keys() if left[k] != right[k])
