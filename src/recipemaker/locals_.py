"""Evidence for the locals and parameters a decompiler named by register or stack slot (`sp1C`, `arg2`, `var_s0`,
`temp_v1`): what each is used for, read from the function text and the project's prototypes alone (no ROM map).

Three forms of evidence, each re-derived from the source when a rename is submitted, so a reader can check them:
  param CALLEE NAME   the value is passed as an argument whose parameter the prototype of CALLEE calls NAME
  member NAME         the value is loaded from, or stored into, the record member NAME
  role loop|index|return   the value is a loop counter, indexes an array, or is the function's result"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from recipemaker import access, cparse, table

WORD = re.compile(r"[A-Za-z_]\w*")
_PROTO = re.compile(r"(?m)^[ \t]*[\w \t*]+?\b([A-Za-z_]\w*)[ \t]*\(([^;{}()]*(?:\([^()]*\)[^;{}()]*)*)\)[ \t\n]*[;{]")
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_MEMBER = re.compile(r"(?:->|\.)\s*([A-Za-z_]\w*)")
_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "else", "do", "case"}


@lru_cache(maxsize=1)
def _placeholder() -> re.Pattern[str]:
    return re.compile(table("knowledge/style.toml")["comments"]["m2c_local"])


def placeholder(name: str) -> bool:
    """Whether NAME is one a decompiler invented for a register or stack slot."""
    return bool(_placeholder().fullmatch(name))


def named(member: str) -> bool:
    """A member or parameter name that says something: not unkNN, padNN or a decompiler slot."""
    return bool(member) and not placeholder(member) and not re.fullmatch(r"(?:unk|pad)_?[0-9A-Fa-f]*", member)


class Prototypes:
    """The parameter names the project's C files give every function, read in one pass over the tree the first time a
    name is asked (a scan per batch of new names re-ran one big regex over every file that spelled one: 156 scans,
    21 s for three folders of `recipe name count`)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.found: dict[str, list[list[str]]] | None = None

    def get(self, names: Iterable[str]) -> dict[str, list[list[str]]]:
        if self.found is None:
            paths = [p for d in ("src", "include") for p in (self.root / d).rglob("*") if p.suffix.lower() in _EXT]
            texts = [p.read_text(encoding="utf-8", errors="replace") for p in paths if p.is_file()]
            self.found = prototypes(texts, None)
        return self.found


_EXT = (".c", ".cpp", ".h")


def calls(body: str) -> set[str]:
    """The names a function body calls."""
    return {m[1] for m in _CALL.finditer(body)} - _KEYWORDS


def prototypes(texts: Iterable[str], wanted: Iterable[str] | None) -> dict[str, list[list[str]]]:
    """Per WANTED function name (None: every name) the parameter-name lists the TEXTS declare or define for it (an
    unnamed parameter or a decompiler slot is an empty string)."""
    if wanted is None:
        head = r"[A-Za-z_]\w*"
    else:
        names = sorted(set(wanted), key=len, reverse=True)
        if not names:
            return {}
        head = "|".join(map(re.escape, names))
    pattern = re.compile(
        r"(?m)^[ \t]*[\w \t*]+?\b(" + head + r")[ \t]*\(([^;{}()]*(?:\([^()]*\)[^;{}()]*)*)\)[ \t\n]*[;{]"
    )
    out: dict[str, list[list[str]]] = {}
    for text in texts:
        for found in pattern.finditer(access.mask(text)):
            if found[1] in _KEYWORDS:
                continue
            params = []
            for part in split(found[2]):
                words = WORD.findall(part)
                last = words[-1] if len(words) >= 2 and not part.rstrip().endswith("*") else ""
                params.append(last if named(last) else "")
            out.setdefault(found[1], []).append(params)
    return out


def split(text: str) -> list[str]:
    """TEXT cut at its top-level commas."""
    parts, depth, at = [], 0, 0
    for i, c in enumerate(text):
        depth += (c in "([{") - (c in ")]}")
        if c == "," and not depth:
            parts.append(text[at:i])
            at = i + 1
    return [*parts, text[at:]] if text.strip() else parts


@dataclass(frozen=True)
class Function:
    name: str
    start: int  # offsets of the whole definition in the file text
    end: int
    line: int  # the 1-based line of its head


def functions(text: str) -> list[Function]:
    """The function definitions of a file (a head with a braced body), in order."""
    masked = access.mask(text)
    out = []
    for a, b, directive in cparse.spans(masked):
        body = masked[a:b]
        if directive or "{" not in body or not body.rstrip().endswith("}"):
            continue
        head = body[: body.index("{")]
        found = re.search(r"\b([A-Za-z_]\w*)\s*\(", head)
        if found and found[1] not in _KEYWORDS and head.rstrip().endswith(")"):
            out.append(Function(found[1], a, b, masked.count("\n", 0, a) + 1))
    return out


@dataclass(frozen=True)
class Slot:
    name: str
    kind: str  # param | local
    type: str
    line: int


def slots(text: str, fn: Function) -> list[Slot]:
    """The parameters and locals of FN that carry a decompiler name, once each, with their declared type."""
    masked = access.mask(text)
    body = masked[fn.start : fn.end]
    open_ = body.index("{")
    decl = re.compile(
        r"\b([A-Za-z_][\w \t]*?)(?:[ \t]+|[ \t]*\*+[ \t]*)(" + _placeholder().pattern + r")\s*(?=[=;,)\[])"
    )
    found: dict[str, Slot] = {}
    for m in decl.finditer(body):
        kind_word = m[1].split()[-1] if m[1].split() else ""
        if kind_word in ("return", "goto", "case", "else") or m[2] in found:
            continue
        line = masked.count("\n", 0, fn.start + m.start(2)) + 1
        found[m[2]] = Slot(m[2], "param" if m.start() < open_ else "local", " ".join(m[1].split()), line)
    return list(found.values())


def suggestions(body: str, name: str, protos: Mapping[str, list[list[str]]], debug: Sequence[str] = ()) -> list[str]:
    """The evidence strings the text of one function (comments masked) gives for NAME, best first; DEBUG are the
    original variable names the debug catalog records for the function (`debug NAME`, one of them is NAME's)."""
    out: list[str] = [f"debug {n}" for n in debug]
    for call in _CALL.finditer(body):
        callee = call[1]
        if callee in _KEYWORDS or callee not in protos:
            continue
        depth, i = 1, call.end()
        while i < len(body) and depth:
            depth += (body[i] == "(") - (body[i] == ")")
            i += 1
        for at, arg in enumerate(split(body[call.end() : i - 1])):
            if arg.strip().lstrip("&") == name:
                for params in protos[callee]:
                    if at < len(params) and params[at]:
                        out.append(f"param {callee} {params[at]}")
    for m in re.finditer(rf"\b{re.escape(name)}\s*=[^=;][^;]*?(?:->|\.)\s*([A-Za-z_]\w*)\s*;", body):
        out += [f"member {m[1]}"] * named(m[1])
    for m in re.finditer(rf"(?:->|\.)\s*([A-Za-z_]\w*)\s*=\s*{re.escape(name)}\s*[;,)]", body):
        out += [f"member {m[1]}"] * named(m[1])
    if re.search(rf"\bfor\s*\(\s*{re.escape(name)}\s*=", body):
        out.append("role loop")
    if re.search(rf"\[\s*{re.escape(name)}\s*\]", body):
        out.append("role index")
    if re.search(rf"\breturn\s+{re.escape(name)}\s*;", body):
        out.append("role return")
    return list(dict.fromkeys(out))


def verify(
    body: str, name: str, evidence: object, protos: Mapping[str, list[list[str]]], debug: Sequence[str] = ()
) -> str:
    """ "" when every evidence item of a rename of NAME is one the source gives, else the reason it is refused."""
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
        return f"{name} needs evidence: param CALLEE NAME, member NAME, role loop|index|return or debug NAME"
    have = suggestions(body, name, protos, debug)
    for item in evidence:
        form = re.sub(r"\s+\(.*$", "", item.strip())
        if form not in have:
            return f"`{item}` is not in the function for {name}; it has: {', '.join(have) or 'no evidence'}"
    return ""


def is_folder(root: Path, target: str) -> bool:
    """Whether TARGET names a source folder (`AI/NAV`, `src/AI/NAV` or a leaf `NAV`) rather than a function."""
    return (
        (root / target).is_dir()
        or (root / "src" / target).is_dir()
        or any(d.is_dir() for d in (root / "src").rglob(target))
    )


def folder_files(root: Path, target: str) -> list[Path]:
    """The sources a target names: a folder's files, else the files defining the function."""
    for base in (root / target, root / "src" / target):
        if base.is_dir():
            return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in (".c", ".cpp"))
    leaf = [d for d in (root / "src").rglob(target) if d.is_dir()]
    if leaf:
        return folder_files(root, leaf[0].relative_to(root).as_posix())
    pat = re.compile(rf"\b{re.escape(target)}\s*\(")
    return sorted(
        p
        for p in (root / "src").rglob("*")
        if p.is_file()
        and p.suffix.lower() in (".c", ".cpp")
        and pat.search(p.read_text(encoding="utf-8", errors="replace"))
    )
