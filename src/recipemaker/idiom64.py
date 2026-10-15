"""Collapse the 64-bit idioms m2c prints as helper calls and hi/lo word pairs into u64 expressions.

Every rewrite is checked numerically (random 64-bit values, equal-high-word and off-by-one cases): the printed words
must be the halves of one 64-bit value, or the expression must equal the comparison or difference it becomes. A name
the rewrites turned into a whole 64-bit value is bracketed until the end, and a pair that something still reads as
two words is left alone, together with every rewrite that needed it."""

from __future__ import annotations

import operator
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

HELPERS = {
    "__udivdi3": "/",
    "__divdi3": "/",
    "__umoddi3": "%",
    "__moddi3": "%",
    "__cmpdi2": "cmp",
    "__floatdisf": "f32",
}
_BITS = {
    "s8": (8, 1),
    "u8": (8, 0),
    "s16": (16, 1),
    "u16": (16, 0),
    "s32": (32, 1),
    "u32": (32, 0),
    "s64": (64, 1),
    "u64": (64, 0),
}
_ASSIGN = re.compile(r"^(\s*)(\w+) = (.*);$")
_CALL = re.compile(r"(\w+)\((.*)\)")
_DECL = r"^[ \t]*(?:s32|u32|u64|s64|s16|u16|s8|u8|void \*) (temp_\w+);\n"
_CAST = re.compile(r"\((s8|u8|s16|u16|s32|u32|s64|u64)\) ")
_GLOBAL = re.compile(r"D_(?:auto_)?([0-9A-Fa-f]{8})")
_MARK = re.compile("[\x01\x02]")  # brackets around a name the rewrites made a whole 64-bit value
_LOW = "_lo_"  # stands for `(u32) name` while an expression is read
_M64 = (1 << 64) - 1
_COMPARE: dict[str, Callable[[int, int], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
_FLOAT = re.compile(
    r"^(?P<i>[ \t]*)if \((?P<cmp>\w+)\((?P<h>\w+), (?P<l>\w+), 0, 0\) > 0\) \{\n"
    r"[ \t]*(?P<v>\w+) = (?P<cvt>\w+)\((?P=h), (?P=l)\);\n"
    r"[ \t]*\} else \{\n"
    r"(?:[ \t]*(?P<a>\w+) = (?P=h) << 0x1F;\n)?"
    r"[ \t]*(?P=v) = 2\.0f \* (?P=cvt)\((?P<p>[^\n]*)\);\n[ \t]*\}",
    re.M,
)


def _mark(name: str) -> str:
    return f"\x01{name}\x02"


def _plain(text: str) -> str:
    return _MARK.sub("", text)


def aliases(table: Mapping[str, Mapping[str, object]], version: str) -> dict[str, str]:
    """Each name a helper goes by in the assembly (its symbol and its func_ label) -> its operator."""
    found: dict[str, str] = {}
    for name, op in HELPERS.items():
        if isinstance(address := table.get(name, {}).get(version), int):
            found |= {name: op, f"func_{address:08X}": op}
    return found


def _wrap(value: int, bits: int, signed: int) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if signed and value >> (bits - 1) else value


def _casts(expr: str) -> str:
    """The C casts of an expression as calls of the type functions (`(u32) x` -> `u32(x)`)."""
    while match := list(_CAST.finditer(expr)):
        m = match[-1]
        end = m.end()
        if expr[end] == "(":
            depth = 0
            while True:
                depth += (expr[end] == "(") - (expr[end] == ")")
                end += 1
                if not depth:
                    break
        else:
            end += len(re.match(r"\w+", expr[end:])[0])  # type: ignore[index]
        expr = f"{expr[: m.start()]}{m[1]}({expr[m.end() : end]}){expr[end:]}"
    return expr


def _python(expr: str, leaf: str) -> str:
    """The C word expression as Python: casts become calls, the leaf's bare name its high word, its cast its low."""
    expr = re.sub(rf"\((?:u32|s32)\) (?:\(u64\) )?{leaf}\b", "LO", _plain(expr))
    return _casts(re.sub(rf"\b{leaf}\b", "HI", re.sub(r"\b(0x[0-9A-Fa-f]+|\d+)[uUlL]+\b", r"\1", expr)))


class _Names(dict[str, int]):
    """Temp values by name, each computed from its definition on first use and wrapped to its declared type."""

    def __init__(self, defs: dict[str, str], types: dict[str, tuple[int, int]], leaf: str, full: int) -> None:
        super().__init__(HI=_wrap(full >> 32, 32, 1), LO=full & 0xFFFFFFFF, **{k: _fn(*v) for k, v in _BITS.items()})
        self.defs, self.types, self.leaf = defs, types, leaf

    def __missing__(self, name: str) -> int:
        value = _wrap(
            eval(_python(self.defs[name], self.leaf), {"__builtins__": {}}, self), *self.types.get(name, (32, 1))
        )
        self[name] = value
        return value


def _fn(bits: int, signed: int):  # type: ignore[no-untyped-def]
    return lambda v: _wrap(v, bits, signed)


def _factor(hi: str, lo: str, defs: dict[str, str], types: dict[str, tuple[int, int]], leaf: str) -> int | None:
    """K when the words are the halves of leaf * K (1 for the bare value), else None."""
    results = []
    rng = random.Random(64)
    try:
        fulls = [1, *(rng.getrandbits(64) for _ in range(6))]
        for full in fulls:
            names = _Names(defs, types, leaf, full)
            h, lw = (eval(_python(e, leaf), {"__builtins__": {}}, names) for e in (hi, lo))
            results.append(((h & 0xFFFFFFFF) << 32) | (lw & 0xFFFFFFFF))
    except (KeyError, ValueError, ZeroDivisionError, RecursionError, SyntaxError, TypeError):
        return None
    k = results[0]
    return k if k and all(r == full * k % 2**64 for r, full in zip(results, fulls, strict=True)) else None


def _adjacent(hi: str, lo: str) -> bool:
    m = [_GLOBAL.fullmatch(n) for n in (hi, lo)]
    return bool(m[0] and m[1] and int(m[1][1], 16) == int(m[0][1], 16) + 4)


def _sext(value: int) -> int:
    return value - (1 << 32) if value >> 31 & 1 else value


def _split(args: str) -> list[str]:
    out: list[str] = []
    depth, current = 0, ""
    for char in args:
        depth += (char == "(") - (char == ")")
        if char == "," and not depth:
            out, current = [*out, current.strip()], ""
        else:
            current += char
    return [*out, current.strip()]


def _result(lines: list[str], i: int, r: str) -> tuple[str, int, str, str] | None:
    """(addend text, lines consumed, hi store, lo store) of the statements that store the division result; the
    stores are adjacent globals, or temps that become one u64 temp and its low word."""
    rest = lines[i + 1 : i + 6]
    pre = re.fullmatch(r"\s*(temp_\w+) = (\w+) \+ (0x[0-9A-Fa-f]+);", rest[0]) if rest else None
    k = 1 if pre else 0
    add = re.fullmatch(rf"\s*(temp_\w+) = \(u32\) (?:\(u64\) )?{r} \+ (\w+);", rest[k]) if len(rest) > k else None
    if add:
        x = add[2]
        sign = rf"\((?:\(s32\) )?{x} >> 0x1F\) \+ "
        hi = (
            re.fullmatch(rf"\s*(\w+) = {r} \+ (?:{sign})?\({add[1]} < (?:\(u32\) )?{x}U?\);", rest[k + 1])
            if len(rest) > k + 1
            else None
        )
        lo = re.fullmatch(rf"\s*(\w+) = (?:\(s32\) )?{add[1]};", rest[k + 2]) if len(rest) > k + 2 else None
        if hi and not (pre and x != pre[1]):
            word = pre[2] if pre else x
            off = _sext(int(pre[3], 16)) if pre else 0
            tail = f" + ({word} {'-' if off < 0 else '+'} {abs(off)})" if off else f" + {word}"
            if not pre and re.fullmatch(r"0x[0-9A-Fa-f]+", x):
                tail = f" + {x}ULL"
            if lo and _adjacent(hi[1], lo[1]):
                return tail, k + 3, hi[1], lo[1]
            if hi[1].startswith("temp_"):
                return tail, k + 2, hi[1], add[1]
    hi = re.fullmatch(rf"\s*(\w+) = {r};", rest[0]) if rest else None
    lo = re.fullmatch(rf"\s*(\w+) = (?:\(s32\) )?(?:\(u32\) )?(?:\(u64\) )?{r};", rest[1]) if len(rest) > 1 else None
    return ("", 2, hi[1], lo[1]) if hi and lo and _adjacent(hi[1], lo[1]) else None


def _pure_temp(line: str) -> bool:
    return bool((m := _ASSIGN.match(line)) and m[2].startswith("temp_") and not re.search(r"\w\(", line))


def _drop(s: str, reads: dict[str, int]) -> tuple[str, str]:
    """`s` without the temp assignments read nowhere else, and without the declarations of unread temps."""
    pure = r"^[ \t]*(temp_\w+) = (?:(?!\b\w+\()[^\n])*;\n"
    t = re.sub(pure, lambda x: "" if reads.get(x[1]) == 1 else x[0], s, flags=re.M)
    return t, re.sub(_DECL, lambda x: "" if x[1] not in reads else x[0], t, flags=re.M)


def _unread(s: str) -> str:
    """The text without the temps nothing reads any more."""
    while True:
        body = re.sub(_DECL, "", s, flags=re.M)
        t, trimmed = _drop(s, {n: len(re.findall(rf"\b{n}\b", body)) for n in re.findall(r"\btemp_\w+", body)})
        if t == s:
            return trimmed
        s = t


@dataclass
class _Run:
    """One attempt: what the rewrites touched, and the 64-bit values `banned` from an earlier attempt."""

    helpers: Mapping[str, str]
    banned: set[str]
    pairs: dict[str, set[str]] = field(default_factory=dict)  # whole value -> its low words, for every rewritten pair
    locals: set[str] = field(default_factory=set)  # temps that became u64
    derived: set[str] = field(default_factory=set)  # temps a difference made, which the numerators may read
    used: set[str] = field(default_factory=set)  # helper names no call is left of
    callee: set[str] = field(default_factory=set)
    full: dict[str, set[str]] = field(default_factory=dict)  # call results and differences -> their low-word temps

    def touch(self, hi: str, *lows: str) -> None:
        self.pairs.setdefault(hi, set()).update(lows)


@dataclass
class _Scan:
    lines: list[str]
    types: dict[str, tuple[int, int]]
    counts: dict[str, int]
    defs: dict[str, str]
    calls: dict[str, tuple[int, str]]


def _scan(text: str, run: _Run) -> _Scan:
    lines = text.split("\n")
    types = {m[2]: _BITS[m[1]] for m in re.finditer(r"^\s*(s32|u32|u64|s64|s16|u16|s8|u8) (temp_\w+);$", text, re.M)}
    counts: dict[str, int] = {}
    defs: dict[str, str] = {}
    calls: dict[str, tuple[int, str]] = {}
    for i, line in enumerate(lines):
        if m := _ASSIGN.match(line):
            counts[m[2]] = counts.get(m[2], 0) + 1
            if _CALL.fullmatch(m[3]):
                calls[m[2]] = (i, m[3])
            else:
                defs[m[2]] = m[3]
        elif (d := re.fullmatch(r"\s*\x01(temp_\w+)\x02 = (.*);", line)) and d[1] in run.derived:
            calls[d[1]] = (i, f"({_plain(d[2])})")
    defs = {k: v for k, v in defs.items() if counts[k] == 1 and k.startswith("temp_")}
    return _Scan(lines, types, counts, defs, calls)


def _registry(scan: _Scan, run: _Run) -> None:
    """Every whole 64-bit value (call result, difference, division result) with the temps holding its low word."""
    run.full = {}
    wide = {n for n in scan.calls if scan.types.get(n, (0, 0))[0] == 64 and scan.counts.get(n, 1) == 1}
    for name in (wide | run.derived | run.locals) - run.banned:
        low = rf"\((?:u32|s32)\) (?:\(u64\) )?(?:{_mark(name)}|{name})"
        run.full[name] = {n for n, rhs in scan.defs.items() if re.fullmatch(low, rhs)}


def _alias(text: str, run: _Run) -> str:
    """`A = R;` with both 64-bit temps names R twice: A is replaced by R everywhere."""
    while True:
        scan = _scan(text, run)
        for i, line in enumerate(scan.lines):
            m = re.fullmatch(r"\s*(temp_\w+) = (temp_\w+);", line)
            if (
                m
                and m[2] in scan.calls
                and m[2] not in run.banned
                and scan.counts[m[1]] == 1
                and scan.types.get(m[1], (0, 0))[0] == 64 == scan.types.get(m[2], (0, 0))[0]
            ):
                rest = [
                    re.sub(rf"\b{m[1]}\b", m[2], x)
                    for j, x in enumerate(scan.lines)
                    if j != i and not re.fullmatch(rf"\s*\w+ {m[1]};", x)
                ]
                text = "\n".join(rest)
                break
        else:
            return text


def _names(expr: str) -> set[str]:
    bare = re.sub(r"\b(?:0x[0-9A-Fa-f]+|\d+)[uUlL]*\b", " ", _plain(expr))
    return {n for n in re.findall(r"[A-Za-z_]\w*", bare) if n not in _BITS}


def _low_forms(expr: str, run: _Run) -> str:
    """`(u32) R` read as the placeholder of R's low word, for every whole value R."""
    for hi in run.full:
        expr = re.sub(rf"\((?:u32|s32)\) (?:\(u64\) )?(?:{_mark(hi)}|{hi})", f"{hi}{_LOW}", expr)
    return expr


@dataclass(frozen=True)
class _Operand:
    hi: str
    lows: tuple[str, ...]
    glob: bool

    def text(self) -> str:
        return _mark(self.hi)


def _operands(names: set[str], run: _Run) -> list[_Operand] | None:
    """The 64-bit values `names` read as a high word and a low word, None when some name is neither."""
    found: list[_Operand] = []
    for hi, lows in run.full.items():
        if hi in names:
            have = tuple(sorted(n for n in names if n in lows or n == hi + _LOW))
            found.append(_Operand(hi, have, False))
    for n in sorted(names):
        sibling = next((s for s in sorted(names) if _adjacent(n, s)), "")
        if sibling and n not in run.banned:
            found.append(_Operand(n, (sibling,), True))
    seen = {x for o in found for x in (o.hi, *o.lows)}
    return found if found and not names - seen else None


_ROWS = None


def _rows() -> list[tuple[int, int]]:
    """Pairs of 64-bit values that tell comparisons and sums apart: equal words, neighbours, flipped high bits."""
    global _ROWS
    if _ROWS is None:
        rng = random.Random(64)
        base = [0, 1, 2**32 - 1, 2**32, 2**32 + 1, 2**63, _M64, *(rng.getrandbits(64) for _ in range(24))]
        _ROWS = [
            (a, b)
            for a in base
            for b in (
                a,
                (a + 1) & _M64,
                (a - 1) & _M64,
                a ^ 0xFFFFFFFF,
                a & ~0xFFFFFFFF | rng.getrandbits(32),
                rng.getrandbits(64),
            )
        ]
    return _ROWS


def _bind(env: dict[str, int], operand: _Operand, value: int) -> None:
    env[operand.hi] = value >> 32
    for low in operand.lows:
        env[low] = value & 0xFFFFFFFF


def _value(expr: str, env: dict[str, int]) -> int | None:
    py = _casts(
        re.sub(r"\b(0x[0-9A-Fa-f]+|\d+)[uUlL]+\b", r"\1", _plain(expr)).replace("||", " or ").replace("&&", " and ")
    )
    try:
        return int(eval(py, {"__builtins__": {}}, {**{k: _fn(*v) for k, v in _BITS.items()}, **env}))
    except (SyntaxError, NameError, TypeError, ValueError, ZeroDivisionError, KeyError):
        return None


def _literals(expr: str) -> list[int]:
    return sorted({int(m[1], 0) for m in re.finditer(r"\b(0x[0-9A-Fa-f]+|\d+)[uUlL]*\b", _plain(expr))})


def _relation(cond: str, run: _Run) -> str | None:
    """The comparison of two 64-bit values (or of one and a constant) that the word expression `cond` is, as text."""
    expr = _low_forms(cond, run)
    found = _operands(_names(expr), run)
    if not found or len(found) > 2:
        return None
    left, right = sorted(found, key=lambda o: _plain(expr).index(o.hi)) if len(found) == 2 else (found[0], None)
    for const in [0] if right else _consts(expr):
        rows = _rows() if right else [(a, const) for a, _ in _rows()] + [(const + d, const) for d in (-1, 0, 1)]
        for op, test in _COMPARE.items():
            if all(_decides(expr, left, right, a, b) == test(a, b) for a, b in rows if 0 <= a <= _M64):
                for o in found:
                    run.touch(o.hi, *o.lows)
                return f"{left.text()} {op} {right.text() if right else f'{const}ULL'}"
    return None


def _consts(expr: str) -> list[int]:
    """The 64-bit constants the literals of `expr` can make: a low word alone, or high and low together."""
    lits = _literals(expr)
    return sorted({(h << 32) | lo for h in [0, *lits] for lo in [0, *lits] if lo >> 32 == 0 and h >> 32 == 0})


def _decides(expr: str, left: _Operand, right: _Operand | None, a: int, b: int) -> bool | None:
    env: dict[str, int] = {}
    _bind(env, left, a)
    if right:
        _bind(env, right, b)
    got = _value(expr, env)
    return None if got is None else bool(got)


def _condition(text: str, run: _Run) -> str:
    """Each `if (...)` whose condition is a comparison of two 64-bit values, rewritten as that comparison."""

    def rewrite(cond: str) -> str:
        whole = _relation(cond, run)
        if whole:
            return whole
        parts = _top(cond)
        if len(parts) < 2:
            return cond
        pieces = [
            (op, _relation(inner, run) or inner)
            if (inner := part.strip().removeprefix("(").removesuffix(")"))
            else (op, part)
            for op, part in parts
        ]
        return (
            cond
            if all(a == b for (_, a), (_, b) in zip(pieces, parts, strict=True))
            else "".join(f"{op}({new})" if op else f"({new})" for op, new in pieces)
        )

    out = []
    for line in text.split("\n"):
        m = re.fullmatch(r"(\s*(?:\} else )?if \()(.*)(\) \{)", line)
        out.append(f"{m[1]}{rewrite(m[2])}{m[3]}" if m else line)
    return "\n".join(out)


def _nested(text: str, run: _Run) -> str:
    """`if (A) { if (B) { ... } }` whose two conditions together are a comparison: one `if` of that comparison."""
    lines = text.split("\n")
    i = 0
    while i < len(lines) - 1:
        i += 1
        a, b = (re.fullmatch(r"(\s*)if \((.*)\) \{", x) for x in lines[i - 1 : i + 1])
        if not (a and b and len(b[1]) == len(a[1]) + 4):
            continue
        end = next((j for j in range(i + 1, len(lines)) if lines[j] == f"{b[1]}}}"), 0)
        inner = lines[i + 1 : end]
        if not end or lines[end + 1 : end + 2] != [f"{a[1]}}}"] or any(x.startswith(f"{b[1]}}} else") for x in inner):
            continue
        if rel := _relation(f"({a[2]}) && ({b[2]})", run):
            lines[i - 1 : end + 2] = [f"{a[1]}if ({rel}) {{", *(x.removeprefix("    ") for x in inner), f"{a[1]}}}"]
            i -= 1
    return "\n".join(lines)


def _top(cond: str) -> list[tuple[str, str]]:
    """The operands of the top-level `&&` and `||` of `cond`, each with the operator in front of it."""
    parts, depth, current, op, i = [], 0, "", "", 0
    while i < len(cond):
        pair = cond[i : i + 2]
        if not depth and pair in ("&&", "||"):
            parts.append((op, current.strip()))
            op, current, i = f" {pair} ", "", i + 2
            continue
        depth += (cond[i] == "(") - (cond[i] == ")")
        current += cond[i]
        i += 1
    return [*parts, (op, current.strip())]


def _difference(text: str, run: _Run) -> str:
    """Two statements making the words of X - Y or X + Y over two 64-bit values: one u64 temp and its low word."""
    lines = text.split("\n")
    scan = _scan(text, run)
    _registry(scan, run)
    i = 0
    while i < len(lines) - 1:
        first, second = _ASSIGN.match(lines[i]), _ASSIGN.match(lines[i + 1])
        i += 1
        if not (first and second and first[2].startswith("temp_") and second[2].startswith("temp_")):
            continue
        lo, hi = first[2], second[2]
        if (
            hi in run.banned
            or scan.counts.get(lo) != 1
            or scan.counts.get(hi) != 1
            or scan.types.get(lo, (0, 0))[0] != 32
        ):
            continue
        expr = f"{_low_forms(first[3], run)} ;; {_low_forms(second[3], run)}"
        found = _operands(_names(expr), run)
        if not found or len(found) != 2 or hi in (o.hi for o in found):
            continue
        for x, y in (found, found[::-1]):
            for op, sign in (("-", -1), ("+", 1)):
                if all(_sum_ok(run, (first[3], second[3]), (x, y), (a, b), sign) for a, b in _rows()):
                    lines[i - 1 : i + 1] = [
                        f"{first[1]}{_mark(hi)} = {x.text()} {op} {y.text()};",
                        f"{first[1]}{lo} = (u32) {_mark(hi)};",
                    ]
                    run.derived.add(hi)
                    run.locals.add(hi)
                    run.touch(hi, lo)
                    for o in (x, y):
                        run.touch(o.hi, *o.lows)
                    run.full[hi] = {lo}
                    break
            else:
                continue
            break
    return "\n".join(lines)


def _sum_ok(
    run: _Run, words: tuple[str, str], operands: tuple[_Operand, _Operand], values: tuple[int, int], sign: int
) -> bool:
    env: dict[str, int] = {}
    for operand, value in zip(operands, values, strict=True):
        _bind(env, operand, value)
    got = [_value(_low_forms(w, run), env) for w in words]
    want = (values[0] + sign * values[1]) & _M64
    return None not in got and (got[0] & 0xFFFFFFFF, got[1] & 0xFFFFFFFF) == (want & 0xFFFFFFFF, want >> 32)  # type: ignore[operator]


def _stores(text: str, run: _Run) -> str:
    """`Gh = H; Gl = L;` over adjacent globals and a whole value with its low word: one store of the whole value."""
    lines = text.split("\n")
    i = 0
    while i < len(lines) - 1:
        a = re.fullmatch(r"(\s*)(D_\w+) = (?:\x01)?(temp_\w+)(?:\x02)?;", lines[i])
        b = re.fullmatch(r"\s*(D_\w+) = (?:\(s32\) )?(temp_\w+);", lines[i + 1])
        if a and b and a[3] in run.full and b[2] in run.full[a[3]] and _adjacent(a[2], b[1]) and a[2] not in run.banned:
            lines[i : i + 2] = [f"{a[1]}{_mark(a[2])} = {_mark(a[3])};"]
            run.touch(a[2], b[1])
            run.touch(a[3], b[2])
        i += 1
    return "\n".join(lines)


def _convert(text: str, run: _Run) -> str:
    """The if/else of an unsigned 64-bit to float conversion (a compare helper and two float helpers) over the words
    of a whole value, as the cast of that value to f32."""

    def swap(m: re.Match[str]) -> str:
        args = _split(m["p"])
        hi, lo = m["h"], m["l"]
        if not (
            run.helpers.get(m["cmp"]) == "cmp"
            and run.helpers.get(m["cvt"]) == "f32"
            and hi in run.full
            and lo in run.full[hi]
            and len(args) == 3
            and all(_halved(args, m["a"], hi, lo, a) for a, _ in _rows())
        ):
            return m[0]
        run.used |= {m["cmp"], m["cvt"]}
        run.touch(hi, lo)
        return f"{m['i']}{m['v']} = (f32) {_mark(hi)};"

    return _FLOAT.sub(swap, text)


def _halved(args: list[str], temp: str | None, hi: str, lo: str, value: int) -> bool:
    """Whether the arguments are the words of (X >> 1) | (X & 1) for the value X made of the words hi and lo."""
    env = {hi: value >> 32, lo: value & 0xFFFFFFFF}
    if temp:
        env[temp] = _wrap(env[hi] << 31, 32, 1)
    got = [_value(a, env) for a in args[:2]]
    want = (value >> 1) | (value & 1)
    return None not in got and (got[0] & 0xFFFFFFFF, got[1] & 0xFFFFFFFF) == (want >> 32, want & 0xFFFFFFFF)  # type: ignore[operator]


def _divide(text: str, run: _Run) -> str:
    """Each helper division over the words of a call result times a constant, and the stores of its result."""
    scan = _scan(text, run)
    lines, types, counts, defs, calls = scan.lines, scan.types, scan.counts, scan.defs, scan.calls
    divide = {n for n, op in run.helpers.items() if op in "/%"}
    dead: set[int] = set()
    for i, line in enumerate(lines):
        m = _ASSIGN.match(line)
        call = _CALL.fullmatch(m[3]) if m else None
        if not (m and call and call[1] in divide and counts[m[2]] == 1 and len(args := _split(call[2])) == 4):
            continue
        hi, lo = args[2:]
        res = _result(lines, i, m[2])
        local = res is None and types.get(m[2], (0, 0))[0] == 64
        if local:
            res = ("", 0, m[2], "")
        seen, todo = set(), [args[0], args[1]]
        while todo:
            for n in re.findall(r"[A-Za-z_]\w*", _plain(todo.pop())):
                if n not in seen:
                    seen.add(n)
                    todo += [defs[n]] if n in defs else []
        names = seen & calls.keys()
        leaf = next(iter(names)) if len(names) == 1 else ""
        k = _factor(args[0], args[1], defs, types, leaf) if leaf and calls[leaf][0] < i else None
        if not (res and k and _adjacent(hi, lo)) or {hi, res[2]} & run.banned:
            continue
        at, text_call = calls[leaf]
        between = [x for x in lines[at + 1 : i] if x.strip() and not _pure_temp(x)]
        num = text_call if k == 1 else f"{text_call} * {k}ULL"
        if between:
            lines[at], num = f"{lines[at][: len(lines[at]) - len(lines[at].lstrip())]}tmp64 = {num};", "tmp64"
        else:
            dead.add(at)
        tail, span, store_hi, store_lo = res
        glob = bool(_GLOBAL.fullmatch(store_hi))
        row = f"{m[1]}{_mark(store_hi)} = {num} {run.helpers[call[1]]} {_mark(hi)}{tail};"
        if store_lo and not glob:  # the low word lives on as a temp that holds the low word of the u64 temp
            row += f"\n{m[1]}{store_lo} = (u32) {_mark(store_hi)};"
            run.full[store_hi] = {store_lo}
        lines[i] = row
        dead |= set(range(i + 1, i + 1 + span))
        run.locals |= {store_hi} if not glob else set()
        run.touch(hi, lo)
        run.touch(store_hi, *filter(None, [store_lo]))
        run.callee.add(text_call.partition("(")[0] if re.fullmatch(r"\w+\(.*\)", text_call) else "")
        run.used.add(call[1])
    return "\n".join(x for j, x in enumerate(lines) if j not in dead)


def _unresolved(text: str, run: _Run) -> set[str]:
    """The whole values some statement still reads as two words, or whose low word something still reads."""
    bad: set[str] = set()
    body = [
        x for x in text.split("\n") if not re.match(r"\s*(?!return|goto)(?:extern )?\w[\w *]* \w+(?:\[\w*\])?;$", x)
    ]
    for hi, lows in run.pairs.items():
        for line in body:
            if any(re.fullmatch(rf"\s*{lo} = \(u32\) \x01?{hi}\x02?;", line) for lo in lows):
                continue  # the definition of a low word: it goes when nothing reads the word
            if any(re.search(rf"\b{lo}\b", line) for lo in lows):
                bad.add(hi)
            if re.search(rf"(?<!\x01)\b{hi}\b(?!\x02)", line) and (
                _GLOBAL.fullmatch(hi) or not re.match(rf"\s*{hi} = ", line)
            ):
                bad.add(hi)
    return bad


def _attempt(text: str, helpers: Mapping[str, str], banned: set[str]) -> tuple[str, set[str]]:
    run, original = _Run(helpers, banned), text
    text = _alias(text, run)
    _registry(_scan(text, run), run)
    text = _difference(text, run)
    text = _divide(text, run)
    _registry(_scan(text, run), run)
    text = _convert(_stores(text, run), run)
    text = _condition(_nested(text, run), run)
    if bad := _unresolved(text, run):
        return text, bad
    if not run.pairs:
        return original, set()
    return _finish(text, run), set()


def _finish(text: str, run: _Run) -> str:
    """The text with the brackets gone, the dead words dropped and the declarations of the whole values widened."""
    s = _unread(_plain(text))
    for name in sorted(run.locals):
        s = re.sub(rf"^([ \t]+)\w+ {name};$", rf"\1u64 {name};", s, flags=re.M)
    for name in run.callee - {""}:
        s = re.sub(rf"^extern \w+ {name}\(", f"extern u64 {name}(", s, flags=re.M)
    for name in run.used:
        if len(re.findall(rf"\b{name}\b", s)) == 1:
            s = re.sub(rf"^[^\n]*\b{name}\([^\n]*\n", "", s, flags=re.M)
    if "tmp64" in s:
        s = re.sub(r"^(\S[^\n]*\) \{\n)", r"\1    u64 tmp64;\n", s, count=1, flags=re.M)
    for hi, lows in run.pairs.items():
        if _GLOBAL.fullmatch(hi):
            for name in lows:
                s = re.sub(rf"^extern \w+ {name};\n", "", s, flags=re.M)
            s = re.sub(rf"^extern \w+ {hi};\n", f"extern u64 {hi};\n", s, flags=re.M)
    return s


def collapse(text: str, helpers: Mapping[str, str]) -> str:
    """The m2c text with each helper call over hi/lo words, their stores, differences and comparisons as u64 code."""
    if not any(re.search(rf"\b{re.escape(h)}\(", text) for h in helpers):
        return text
    banned: set[str] = set()
    for _ in range(16):
        out, bad = _attempt(text, helpers, banned)
        if not bad:
            return out if out != _plain(text) else text
        banned |= bad
    return text
