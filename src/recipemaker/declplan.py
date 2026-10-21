"""The text side of a declaration change set: one prototype in the definition's family header, every other copy of the
declaration deleted, the include added where a unit loses sight of it, and explicit scalar casts only at call sites
whose old declaration took another type. Pure functions of text; nothing here reads the project."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from recipemaker import headers

_SCALAR = re.compile(r"(?:[suf]\d+|vu\d+|int|char|short|long|float|double|unsigned|signed|size_t)")
_WORDS = {"const", "volatile", "unsigned", "signed", "struct", "union", "enum", "int", "char", "short", "long"}
_WORDS |= {"void", "float", "double"}
_SIMPLE = re.compile(r"[\w.\[\]>-]+")
_MARKER = re.compile(r"^[ \t]*/\*\s*extern\s*\*/[ \t]*$")


@dataclass(frozen=True)
class Copy:
    """One declaration of a name that a text makes: where it stands and the prototype it spells."""

    start: int
    end: int
    statement: str
    prototype: str  # `ret name(params)` or `type name[...]`, without extern


def copies(text: str, name: str) -> list[Copy]:
    """Every file-scope declaration statement in `text` that declares `name` (a definition, a typedef, a record and a
    file-local `static` are none)."""
    found = []
    for start, end, statement in headers.statements(text):
        plain = headers.normal(statement)  # an attribute after the declarator is blanked, so the declarator is read
        if not re.search(rf"\b{re.escape(name)}\b", plain) or re.search(r"\btypedef\b|\bstatic\b|\{", plain):
            continue
        if any(n == name for n, _, _ in headers._declared(plain)):
            spelled = headers._type_of(" ".join(plain.split()), name)
            found.append(Copy(start, end, statement, spelled.replace("@", name) if spelled else ""))
    return found


def _line_span(text: str, start: int, end: int) -> tuple[int, int, bool]:
    """The span widened to whole lines when nothing else stands on them, and whether it was."""
    begin = text.rfind("\n", 0, start) + 1
    tail = text.find("\n", end)
    tail = len(text) if tail < 0 else tail
    if not text[begin:start].strip() and not text[end:tail].strip():
        return begin, min(tail + 1, len(text)), True
    return start, end, False


def _drop_marker(text: str, begin: int) -> int:
    """`begin` moved up over a lone ` /* extern */` line that sat above the deleted declaration."""
    before = text.rfind("\n", 0, max(begin - 1, 0)) + 1
    return before if begin and _MARKER.match(text[before : begin - 1]) else begin


def _declares(declarator: str, name: str) -> bool:
    found = headers._DECLARATOR.search(declarator)
    return found is not None and name in (found[1], found[2])


def remove(text: str, one: Copy, name: str) -> str:
    """`text` without the declaration of `name` that `one` is; a statement that declares other names keeps them."""
    parts = list(headers._parts(text[one.start : one.end].rstrip(";")))
    first = headers._DECLARATOR.search(parts[0])
    if len(parts) > 1 and first:
        decls = [parts[0][first.start() :], *parts[1:]]
        keep = [d.strip() for d in decls if not _declares(d, name)]
        if keep and len(keep) < len(decls):
            return text[: one.start] + parts[0][: first.start()] + ", ".join(keep) + ";" + text[one.end :]
    begin, end, whole = _line_span(text, one.start, one.end)
    if not whole:  # something else shares the line: only the statement goes
        return text[:begin] + text[end:]
    begin = _drop_marker(text, begin)
    out = text[:begin] + text[end:]
    if (
        begin
        and begin < len(out)
        and out[begin - 1] == "\n"
        and out[begin] == "\n"
        and text[begin - 2 : begin] == "\n\n"
    ):
        out = out[:begin] + out[begin + 1 :]  # no second blank line where one deletion met a blank line
    return out


def _style(text: str) -> bool:
    """Whether the declarations already in a header are written with `extern`."""
    rows = [s for _, _, s in headers.statements(text) if "(" in s and "typedef" not in s and "{" not in s]
    return sum(s.lstrip().startswith("extern") for s in rows) * 2 > len(rows) if rows else True


def put(text: str, name: str, prototype: str) -> str:
    """`text` with the one declaration of `name`: the first existing copy replaced in place, the rest deleted, else a
    new line after the last declaration of the header (before the closing #endif when it has none)."""
    line = ("extern " if _style(text) else "") + prototype.rstrip(";").strip() + ";"
    found = copies(text, name)
    if found:
        first = found[0]
        out = text
        for one in reversed(found[1:]):
            out = remove(out, one, name)
        return out[: first.start] + line + out[first.end :]
    last = [s for s in headers.statements(text) if "typedef" not in s[2] and "{" not in s[2] and "(" in s[2]]
    if last:
        at = text.find("\n", last[-1][1])
        at = len(text) if at < 0 else at + 1
        return text[:at] + line + "\n" + text[at:] if text[:at].endswith("\n") else text[:at] + "\n" + line + "\n"
    ends = list(re.finditer(r"^[ \t]*#[ \t]*endif\b[^\n]*\n?\s*\Z", text, re.M))
    if ends:
        return text[: ends[-1].start()] + line + "\n" + text[ends[-1].start() :]
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def include_line(header: str) -> str:
    return f'#include "{header.removeprefix("include/")}"'


def add_include(text: str, header: str) -> str:
    """`text` with the quoted include of `header` after the last include of its first include block; a file with none
    gets it after the guard's #define, else at the top."""
    line = include_line(header) + "\n"
    found = list(re.finditer(r"^[ \t]*#[ \t]*include\b[^\n]*\n?", text, re.M))
    if not found:
        guard = re.match(r"\s*#[ \t]*ifndef\b[^\n]*\n[ \t]*#[ \t]*define\b[^\n]*\n", text)
        return text[: guard.end()] + line + text[guard.end() :] if guard else line + text
    last = found[0]
    for one in found[1:]:
        if text[last.end() : one.start()].strip():
            break  # code between two includes: the block ended
        last = one
    at = last.end()
    return text[:at] + ("\n" if not text[:at].endswith("\n") else "") + line + text[at:]


_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]+)"', re.M)


def resolve(path: str, spelled: str, known: Callable[[str], bool]) -> str | None:
    """The project file a quoted include names: beside the includer, under include/ or under src/."""
    for base in (posixpath.dirname(path), "include", "src"):
        target = posixpath.normpath(posixpath.join(base, spelled))
        if known(target):
            return target
    return None


def reaches(path: str, target: str, read: Callable[[str], str | None], seen: set[str] | None = None) -> bool:
    """Whether `path` includes `target`, directly or through other project files."""
    seen = set() if seen is None else seen
    if path == target:
        return True
    if path in seen:
        return False
    seen.add(path)
    for spelled in _INCLUDE.findall(read(path) or ""):
        found = resolve(path, spelled, lambda p: read(p) is not None)
        if found and reaches(found, target, read, seen):
            return True
    return False


def split_signature(prototype: str, name: str) -> tuple[str, list[str]] | None:
    """(return type, parameter types) of a function prototype, names dropped; None when it is no function."""
    found = re.search(rf"\b{re.escape(name)}\s*\(", prototype)
    if not found or ")" not in prototype:
        return None
    ret = prototype[: found.start()].replace("extern", "").strip()
    inner = prototype[found.end() : prototype.rfind(")")]
    params = [] if inner.strip() in ("", "void") else [typed(p) for p in headers._parts(inner)]
    return ret, params


def typed(param: str) -> str:
    """A parameter's type: its trailing name dropped (`CVector3 *out` -> `CVector3 *`, `unsigned int` stays)."""
    text = " ".join(param.split())
    tokens = text.split()
    named = re.search(r"(\w+)\s*(?:\[[^\]]*\])?$", text)
    if named and len(tokens) >= 2 and named[1] not in _WORDS and tokens[-2] not in ("struct", "union", "enum"):
        text = text[: named.start()].strip() or text
    return text


def scalar(spelled: str) -> bool:
    words = re.findall(r"\w+|\*", spelled)
    return (
        bool(words) and "*" not in words and all(_SCALAR.fullmatch(w) for w in words if w not in ("const", "volatile"))
    )


def same(a: str, b: str) -> bool:
    return headers._unnamed(headers._normal(a).replace(" ", "")) == headers._unnamed(
        headers._normal(b).replace(" ", "")
    )


def _arguments(text: str, opened: int) -> tuple[list[tuple[int, int]], int] | None:
    """The (start, end) of each argument of the call whose `(` stands at `opened`, and the index after its `)`."""
    depth, start, out = 0, opened + 1, []
    for i in range(opened, len(text)):
        char = text[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                out.append((start, i))
                return out, i + 1
        elif char == "," and depth == 1:
            out.append((start, i))
            start = i + 1
        elif char == ";" or char == '"':
            return None
    return None


@dataclass
class Cast:
    text: str
    count: int = 0
    blocked: list[str] = field(default_factory=list)


def cast_calls(text: str, name: str, old: tuple[str, list[str]], new: tuple[str, list[str]]) -> Cast:
    """`text` with every call of `name` cast back to the types `old` took where `new` differs: a scalar argument
    wrapped in a cast to its old type, a scalar result cast to its old type. A pointer difference is not cast (a
    pointer cast to another view is what the original developers did not write): it is returned in `blocked`."""
    result = Cast(text)
    out, at = [], 0
    for call in re.finditer(rf"(?<![\w.>&*])\b{re.escape(name)}\s*\(", text):
        if call.start() < at:
            continue
        opened = call.end() - 1
        parsed = _arguments(text, opened)
        if parsed is None:
            continue
        spans, after = parsed
        if len(spans) == 1 and not text[spans[0][0] : spans[0][1]].strip():
            spans = []
        if len(old[1]) != len(new[1]):
            result.blocked.append(f"{name} took {len(old[1])} parameters here, the definition takes {len(new[1])}")
            return result
        if len(spans) != len(new[1]):
            result.blocked.append(f"a call of {name} passes {len(spans)} arguments, the definition takes {len(new[1])}")
            continue
        pieces, changed = [], False
        for (a, b), was, now in zip(spans, old[1], new[1], strict=True):
            argument = text[a:b]
            if same(was, now):
                pieces.append(argument)
            elif scalar(was) and scalar(now):
                body = argument.strip()
                wrapped = body if _SIMPLE.fullmatch(body) else f"({body})"
                pieces.append(f"{argument[: len(argument) - len(argument.lstrip())]}({was}){wrapped}")
                changed = True
            else:
                result.blocked.append(f"{name} took `{was}` where the definition takes `{now}`")
                pieces.append(argument)
        call_text = text[call.start() : opened + 1] + ",".join(pieces) + ")"
        before = text[: call.start()].rstrip()[-1:]
        unread = before in ("", ";", "{", "}", ":") and text[after:].lstrip().startswith(";")  # nobody reads the result
        if not unread and not same(old[0], new[0]):
            if scalar(old[0]) and scalar(new[0]) and old[0].strip() != "void":
                call_text, changed = f"({old[0]}){call_text}", True
            else:
                result.blocked.append(f"{name} returned `{old[0]}` where the definition returns `{new[0]}`")
        out += [text[at : call.start()], call_text if changed else text[call.start() : after]]
        result.count += changed
        at = after
    result.text = "".join(out) + text[at:]
    return result


def overlay(edits: Mapping[str, str], read: Callable[[str], bytes | None]) -> Callable[[str], str | None]:
    """A reader of project text with `edits` laid over the tree."""

    def text(path: str) -> str | None:
        if path in edits:
            return edits[path]
        data = read(path)
        return None if data is None else data.decode(errors="replace")

    return text
