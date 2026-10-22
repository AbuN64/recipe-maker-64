"""Repairs of m2c drafts for errors the compiler would name: stack names m2c left undeclared, small-number symbols
used as field offsets, and scalars that are dereferenced. Each is a text rewrite of the draft, never of a header."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from recipemaker import records

_STACK = re.compile(r"\bsp(?=[0-9A-F]*[0-9])[0-9A-F]+\b")
_OFFSET = re.compile(
    r"\*\((\w+) \+ &(D_[0-9A-Fa-f]{1,7})\)"
)  # a symbol of at most 7 digits is a number, not an address
_SCALAR = r"(?:M2C_UNK\d*|s8|s16|s32|u8|u16|u32|int|short|char|unsigned int)"
_DEREF = re.compile(r"invalid type argument of `unary \*'")
_ERROR = re.compile(r"\.c:(\d+): invalid type argument of `unary \*'")
_FIELD_TYPES = ("f32", "f64", "s32", "u32", "s16", "u16", "s8", "u8")


def stack(text: str) -> str:
    """The draft with a local for each stack name it uses and never declares."""
    names = sorted(set(_STACK.findall(re.sub(r"/\*.*?\*/", "", text, flags=re.S))))
    missing = [n for n in names if not re.search(rf"^[ \t]+[A-Za-z_][\w ]*?[ *]{n}(?:\[\w*\])?;", text, re.M)]
    opened = re.search(r"^[^\n#]*\) \{\n", text, re.M)
    if not missing or opened is None:
        return text
    return text[: opened.end()] + "".join(f"    M2C_UNK {n};\n" for n in missing) + text[opened.end() :]


def offsets(text: str) -> str:
    """`*(base + &D_26DB0)` as a field access at that offset, typed like the variable it is read into."""
    types = {m[2]: m[1] for m in re.finditer(rf"^\s*({'|'.join(_FIELD_TYPES)})\s+(\w+);", text, re.M)}

    def field(found: re.Match[str]) -> str:
        start = text.rfind("\n", 0, found.start()) + 1
        read = re.fullmatch(r"\s*(\w+) = ", text[start : found.start()])
        return f"M2C_FIELD({found[1]}, {types.get(read[1], 's32') if read else 's32'} *, 0x{found[2][2:]})"

    out = _OFFSET.sub(field, text)
    for name in {m[2] for m in _OFFSET.finditer(text)}:
        if out.count(name) == 1:
            out = re.sub(rf"^extern \w+ {name};\n", "", out, flags=re.M)
    return out


_BITWISE = re.compile(r"M2C_BITWISE\(\s*(\w+)\s*,\s*(\w+)\s*\)")
_FIRST = re.compile(r"^\s*((?:\w+\s+)*?\w+)\s+(\w+)\s*(?:\[[^\]]*\])?\s*[,;]", re.M)


def members(text: str, structs: Mapping[str, str]) -> str:
    """`M2C_BITWISE(T, local)` of a struct local whose first field is a T, as a read of that field: m2c reads offset 0
    of a struct value as a reinterpreted word, which a C compiler refuses as an aggregate used where a T is expected."""
    kinds = {m[2]: m[1] for m in re.finditer(r"^[ \t]+(?:struct\s+)?(\w+)[ \t]+(\w+);", text, re.M)}

    def field(found: re.Match[str]) -> str:
        body = structs.get(kinds.get(found[2], ""), "")
        first = _FIRST.search(body)
        return f"{found[2]}.{first[2]}" if first and first[1].split()[-1] == found[1] else found[0]

    return _BITWISE.sub(field, text)


_ARG_CAST = re.compile(r"(?:(?<=\()|(?<=, ))\(\s*(?:const\s+)?(?:struct\s+)?\w+\s*\*+\s*\)\s*(?=&?[\w.>\[\]-]+\s*[,)])")


def uncast(text: str) -> str:
    """The draft without the pointer casts m2c puts on whole call arguments: a pointer converts to the parameter type
    with the same code, so the cast only hides that two project types disagree (the compiler warns instead)."""
    return _ARG_CAST.sub("", text)


_ARG_NOTE = re.compile(r"/\* (extra\?|(\w+)\+0x([0-9A-Fa-f]+)) \*/ ")


def _calls(text: str) -> list[tuple[int, int]]:
    """(start, end) of the argument list inside each call's parentheses that holds one of m2c's argument notes."""
    found = []
    for note in _ARG_NOTE.finditer(text):
        depth, at = 0, note.start()
        while at > 0 and depth >= 0:
            at -= 1
            depth += (text[at] in ")]") - (text[at] in "([")
        if at > 0 and depth < 0 and text[at] == "(" and re.search(r"\w\s*$", text[:at]):
            found.append(at + 1)
    out = []
    for start in sorted(set(found)):
        depth, at = 1, start
        while at < len(text) and depth:
            depth += (text[at] in "([") - (text[at] in ")]")
            at += 1
        out.append((start, at - 1))
    return out


def _arguments(args: list[str]) -> list[str]:
    """The arguments without m2c's guesses past the prototype (`/* extra? */`), and each struct passed by value that m2c
    spelled as its words (`/* Vec3+0x0 */ p.x, /* Vec3+0x4 */ p.y, ..`) as the one expression they are members of;
    words that name no common record stay as they are."""
    out: list[str] = []
    at = 0
    while at < len(args):
        note = _ARG_NOTE.match(args[at].strip())
        if note is None:
            out.append(args[at])
            at += 1
            continue
        if note[1] == "extra?":
            at += 1
            continue
        run = [args[at].strip()]
        while at + len(run) < len(args):
            nxt = _ARG_NOTE.match(args[at + len(run)].strip())
            if nxt is None or nxt[2] != note[2] or int(nxt[3], 16) == 0:
                break
            run.append(args[at + len(run)].strip())
        bases = {re.sub(r"(?:\.|->)\w+$", "", _ARG_NOTE.sub("", word, count=1)) for word in run}
        ways = {re.search(r"(\.|->)\w+$", _ARG_NOTE.sub("", word, count=1)) is not None for word in run}
        steps = {m[1] for word in run if (m := re.search(r"(\.|->)\w+$", _ARG_NOTE.sub("", word, count=1)))}
        if int(note[3], 16) or len(bases) != 1 or ways != {True} or len(steps) != 1:
            out += args[at : at + len(run)]
            at += len(run)
            continue
        base = bases.pop()
        out.append(" " * (at > 0) + (base if steps.pop() == "." else f"*{base}"))
        at += len(run)
    return out


def arguments(text: str) -> str:
    """The draft with the call arguments m2c annotated (see `_arguments`) rewritten where it can: m2c guesses extra
    arguments from registers the callee's prototype does not take, and splits a struct passed by value into words,
    which a call against the project prototype cannot compile."""
    for start, end in reversed(_calls(text)):
        text = text[:start] + ",".join(_arguments(records.split(text[start:end], ","))).strip() + text[end:]
    return text


def repair(text: str) -> str:
    return arguments(offsets(stack(text)))


def _operands(line: str) -> set[str]:
    """The first name of each operand of a unary `*` on the line."""
    found = set()
    for at, char in enumerate(line):
        before = line[:at].rstrip()
        if char == "*" and (not before or before[-1] in "(,=!&|+-*<>?:;{" or before.endswith("return")):
            named = re.match(r"[\s*(&]*([A-Za-z_]\w*)", line[at + 1 :])
            found |= {named[1]} if named else set()
    return found


def deref(text: str, reason: str, skew: int) -> str:
    """The draft with one more pointer level on each scalar the compiler says is dereferenced; `skew` is the number of
    lines the measured file has before the draft."""
    lines = text.splitlines(keepends=True)
    names: set[str] = set()
    for number in {int(m[1]) for m in _ERROR.finditer(reason)}:
        if 0 <= number - 1 - skew < len(lines):
            names |= _operands(lines[number - 1 - skew])
    for name in sorted(names):
        text = re.sub(rf"\b({_SCALAR})(\s+\**){name}\b", rf"\1\2*{name}", text, count=1)
    return text


_UNKNOWN = {"M2C_UNK": "s32", "M2C_UNK8": "s8", "M2C_UNK16": "s16", "M2C_UNK32": "s32", "M2C_UNK64": "s64"}


def unknown(text: str) -> str:
    """The draft with m2c's unknown-type names spelled as the project scalars they are typedefs of (m2c_macros.h): the
    same code, and no M2C_ text a landing refuses."""
    return re.sub(r"\bM2C_UNK(?:8|16|32|64)?\b", lambda m: _UNKNOWN[m[0]], text)


_FAKE = re.compile(r"(?:\bstruct\s+)?\b(\w*func_[0-9A-Fa-f]+\w*)(\s*\*)")  # a record type named after one function
_PROTOTYPE = re.compile(r"^(?:extern\s+)?[A-Za-z_][^\n;{}#=]*\b(\w+)\s*\([^\n;{}]*\)\s*;$", re.M)
_LOCAL = re.compile(r"^([ \t]+)(?:struct\s+)?(\w*func_[0-9A-Fa-f]+\w*)\s*\*\s*(\w+);$", re.M)


def _dereferenced(body: str, name: str) -> bool:
    """Whether the draft reads through `name`: a member, an index or a unary `*`."""
    if re.search(rf"\b{name}\s*(?:->|\[|\.)", body):
        return True
    return any(name in _operands(line) for line in body.splitlines())


def unfake(text: str, member: str) -> str:
    """The draft without per-function record types (a name carrying a function symbol) that callee signatures and the
    locals m2c typed from them bring in: a prototype's pointer to one becomes `void *`, and so does a local nothing
    reads through. A pointer converts to and from `void *` with the same code, so only the type debt goes; a local that
    is read through keeps its type, and the landing refuses it as before."""

    def prototype(found: re.Match[str]) -> str:
        if found[1] == member:
            return found[0]
        return _FAKE.sub(lambda m: f"void{m[2]}" if m[1] != found[1] else m[0], found[0])

    out = _PROTOTYPE.sub(prototype, text)
    opened = re.search(rf"^[^\n;{{}}#]*\b{re.escape(member)}\s*\([^;{{]*\)\s*\{{$", out, re.M)
    if opened is None:
        return out
    body = out[opened.end() :]

    def local(found: re.Match[str]) -> str:
        return found[0] if _dereferenced(body, found[3]) else f"{found[1]}void *{found[3]};"

    return out[: opened.end()] + _LOCAL.sub(local, body)


def refused(reason: str) -> bool:
    return bool(_DEREF.search(reason))


_WORD = {"s8", "u8", "s16", "u16", "s32", "u32", "f32", "int", "float", "char", "short", "long"}
_PAIR = re.compile(r"^\s*(?:const\s+)?(?:unsigned\s+)?(\w+)\s+(\w+)\s*;\s*$")


def _split(text: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for at, char in enumerate(text):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and not depth:
            parts.append(text[start:at].strip())
            start = at + 1
    return [*parts, text[start:].strip()] if text.strip() else []


def _fields(body: str) -> list[str] | None:
    """The member names of a struct whose members are all one-word scalars, else None."""
    names = []
    for line in body.strip().splitlines():
        if not line.strip():
            continue
        pair = _PAIR.match(line)
        if pair is None or pair[1] not in _WORD:
            return None
        names.append(pair[2])
    return names or None


def by_value(text: str, member: str, declaration: str, structs: Callable[[], Mapping[str, str]]) -> str:
    """m2c flattens a by-value struct parameter into one scalar per word (`CVector3` -> arg5, arg6, arg7), so the
    draft's signature conflicts with the declared prototype. When the prototype's word count agrees with m2c's, the
    draft takes the declared parameter types and each flattened word becomes its struct field (`arg5.x`)."""
    mark = re.search(rf"^([^\n(=;]*\b{re.escape(member)}\()(.*)\) \{{$", text, re.M)
    start = declaration.find(f"{member}(")
    if mark is None or start < 0 or not declaration.rstrip().endswith(");"):
        return text
    declared = _split(declaration.rstrip()[start + len(member) + 1 : -2])
    flat = _split(mark[2])
    if not declared or declared == ["void"] or any("(" in d or "..." in d for d in declared):
        return text
    kinds = [re.sub(r"\s+", " ", d).replace("struct ", "").strip() for d in declared]
    plain = lambda k: k.endswith("*") or k.split()[-1] in _WORD or k.split()[-1] in ("void",)  # noqa: E731
    if all(plain(k) for k in kinds):
        return text
    known = structs()
    words: list[list[str] | None] = []
    for kind in kinds:
        if plain(kind):
            words.append(None)
            continue
        names = _fields(known.get(kind.split()[-1], ""))
        if names is None:
            return text
        words.append(names)
    if sum(1 if w is None else len(w) for w in words) != len(flat):
        return text
    given = [re.findall(r"(\w+)\s*$", f)[0] if re.search(r"\w\s*$", f) else "" for f in flat]
    if not all(given):
        return text
    params, rename, at = [], {}, 0
    for declared_type, w in zip(declared, words, strict=True):
        if w is None:
            params.append(f"{declared_type}{'' if declared_type.endswith('*') else ' '}{given[at]}")
            at += 1
            continue
        params.append(f"{declared_type}{'' if declared_type.endswith('*') else ' '}{given[at]}")
        for offset, field in enumerate(w):
            rename[given[at + offset]] = f"{given[at]}.{field}"
        at += len(w)
    head, body = text[: mark.start(2)], text[mark.end(2) :]
    if rename:
        pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, rename)) + r")\b(?!\.)")
        body = pattern.sub(lambda m: rename[m[1]], body)
    return head + ", ".join(params) + body
