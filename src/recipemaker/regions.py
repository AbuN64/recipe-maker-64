"""Regions of a function: the target's basic-block runs, the names each reads, and which of them the build matches."""

import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise

from recipemaker import index

_INSTRUCTION = re.compile(r"^\s*/\*\s*\w+\s+[0-9A-F]{8}\s+[0-9A-F]{8}\s*\*/\s+(\S+)\s*(.*)$")
_LABEL = re.compile(r"^\s*\.L\w+:")
_CONSTANT = re.compile(r"\b0x[0-9A-Fa-f]{4,}\b")
_REGISTER = re.compile(r"\$\w+")
ANCHORS = 3  # names shown for one region


@dataclass(frozen=True)
class Region:
    start: int  # byte offset from the function's first instruction
    size: int
    calls: tuple[str, ...]
    reads: tuple[str, ...]  # globals and constants
    kind: str = "match"  # match | order | compare | register | missing | extra | other
    lines: tuple[str, ...] = ()  # the diff lines that fall inside the region

    def anchors(self) -> str:
        parts = [f"calls {', '.join(self.calls[:ANCHORS])}"] * bool(self.calls)
        return ", ".join([*parts, *[f"reads {', '.join(self.reads[:ANCHORS])}"] * bool(self.reads)])


def _branch(mnemonic: str) -> bool:
    return mnemonic in ("j", "jr") or (mnemonic.startswith("b") and mnemonic != "break")


def split(asm: str) -> list[Region]:
    """The runs of instructions between labels and branches, a branch's delay slot staying with it."""
    starts, rows = {0}, list[str]()
    label, cut = False, -1
    for line in asm.splitlines():
        if _LABEL.match(line):
            label = True
        elif found := _INSTRUCTION.match(line):
            at = len(rows)
            if label or at == cut:
                starts.add(at)
            label = False
            cut = at + 2 if _branch(found[1]) else cut
            rows.append(f"{found[1]} {found[2]}")
    bounds = [*sorted(starts), len(rows)]
    return [
        Region(
            4 * a,
            4 * (b - a),
            tuple(dict.fromkeys(n for r in rows[a:b] for n in index.CALL.findall(r))),
            tuple(dict.fromkeys(n for r in rows[a:b] for n in [*index.REFERENCE.findall(r), *_CONSTANT.findall(r)])),
        )
        for a, b in pairwise(bounds)
        if b > a
    ]


_COMPARE = re.compile(r"(sltu?)\s+(\$\w+),\s*(\$\w+),\s*(\$\w+)")


def _swapped(target: str, built: str) -> bool:
    """A compare whose two operands are the other way round in the build."""
    a, b = _COMPARE.fullmatch(target.strip()), _COMPARE.fullmatch(built.strip())
    return bool(a and b and a[1] == b[1] and a[2] == b[2] and a[3] == b[4] and a[4] == b[3] and a[3] != a[4])


def kind(target: Sequence[str], built: Sequence[str]) -> str:
    """How the instructions only the target has differ from the ones only the build has."""
    if not target or not built:
        return "missing" if target else "extra" if built else "match"
    if len(target) != len(built):
        return "other"
    if sorted(target) == sorted(built):
        return "order"
    if all(map(_swapped, target, built)):
        return "compare"
    swaps = {
        (x, y)
        for t, b in zip(target, built, strict=True)
        for x, y in zip(*map(_REGISTER.findall, (t, b)), strict=False)
    }
    plain = all(_REGISTER.sub("$", t) == _REGISTER.sub("$", b) for t, b in zip(target, built, strict=True))
    return "register" if plain and len({x for x, _ in swaps}) == len({y for _, y in swaps}) == len(swaps) else "other"


def classify(regions: Sequence[Region], diff: str, vram: int) -> list[Region]:
    """Each region with the kind of difference the diff shows in it."""
    starts = [r.start for r in regions]
    minus: list[list[str]] = [[] for _ in regions]
    plus: list[list[str]] = [[] for _ in regions]
    lines: list[list[str]] = [[] for _ in regions]
    at = 0
    for line in diff.splitlines():
        sign, address, _, text = line[:1], *line[1:].partition(": ")
        if sign not in (" ", "-", "+") or line.startswith(("---", "+++")) or not text:
            continue
        if sign != "+":
            at = max(bisect_right(starts, int(address, 16) - vram) - 1, 0)
        if sign != " ":
            (minus if sign == "-" else plus)[at].append(text)
            lines[at].append(line)
    return [replace(r, kind=kind(minus[i], plus[i]), lines=tuple(lines[i])) for i, r in enumerate(regions)]


_STORES = ("sb", "sh", "sw", "sd", "swl", "swr", "swc1", "sdc1")


def uses(asm: str) -> tuple[dict[str, tuple[int, ...]], list[str], dict[str, tuple[int, ...]]]:
    """Per callee: the argument registers set before its jal, those below its highest never written, and pointers."""
    calls: dict[str, tuple[int, ...]] = {}
    through: dict[str, tuple[int, ...]] = {}
    written: set[int] = set()
    loaded: dict[str, str] = {}
    pointers: list[str] = []
    set_args: set[int] = set()
    waiting = ""
    for line in asm.splitlines():
        if _LABEL.match(line):
            set_args.clear()
        if not (found := _INSTRUCTION.match(line)):
            continue
        op, rest = found[1], found[2]
        regs, base, symbol = _REGISTER.findall(rest), re.search(r"\((\$\w+)\)", rest), re.search(r"%lo\((\w+)\)", rest)
        if base and base[1] in loaded and op not in ("jr", "jalr"):
            pointers.append(loaded[base[1]])
        wrote = regs[0] if regs and op not in _STORES and not _branch(op) and not op.startswith("jr") else ""
        loaded.pop(wrote, None)
        if op == "lw" and symbol and wrote:
            loaded[wrote] = symbol[1]
        if wrote.startswith("$a") and wrote[2:].isdigit():
            set_args.add(int(wrote[2:]))
            written.add(int(wrote[2:]))
        if waiting:
            calls.setdefault(waiting, tuple(sorted(set_args)))
            through.setdefault(waiting, tuple(k for k in range(max(set_args, default=0)) if k not in written))
            waiting = ""
            set_args.clear()
        if op == "jal":
            waiting = rest.split()[0]
    return calls, list(dict.fromkeys(pointers)), through


def missing_calls(found: Sequence[Region]) -> list[str]:
    """The regions whose target diff lines hold a jal the build lacks, with the callees the region names."""
    return [
        f"target calls {', '.join(r.calls)} here; your candidate does not"
        for r in found
        if r.calls
        and sum(x[:1] == "-" for x in r.lines if ": jal " in x) > sum(x[:1] == "+" for x in r.lines if ": jal " in x)
    ]


def misdeclared(asm: str, declared: Mapping[str, tuple[str, int, str]]) -> dict[str, str]:
    """The globals the target uses as pointers whose declaration is a plain value, with their declared type."""
    kinds = {n: declared[n][2].removeprefix("extern ").rsplit(n, 1)[0].strip() for n in uses(asm)[1] if n in declared}
    return {n: k for n, k in kinds.items() if "*" not in k and "[" not in k}
