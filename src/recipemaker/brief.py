"""The brief a reader gets: asm, candidate, worst diff and what was auto-fixed."""

import re
from collections.abc import Sequence
from itertools import zip_longest
from pathlib import Path

from unbake.contracts import Finding, Json, Snapshot

from recipemaker import crack, draft, errors, host, index, ladder, state
from recipemaker import regions as target
from recipemaker.ledger import Left

MAX_LINES = 120
_DIFF = 60  # diff lines shown: the first hunks, which start at the first byte that differs
_ERRORS = 5  # compiler error lines shown
_REGIONS = 40  # differing regions listed
_SHOWN = 2  # differing regions whose lines are shown
_DUMP = 12  # data rows shown, 16 bytes each
_USERS = 8  # functions that reference a data item shown
_WIDTH = "lb=s8 lbu=u8 lh=s16 lhu=u16 lw=32 lwc1=f32 ldc1=f64 sb=8 sh=16 sw=32 swc1=f32 sdc1=f64"
_ACCESS = dict(w.split("=") for w in _WIDTH.split())
WINDOW = 0x40  # bytes either side of a symbol that count as its neighbours


def scores(score: Json) -> str:
    return ", ".join(f"{version} {state.percent(value)}" for version, value in score.items())


def refusals(findings: Sequence[Finding]) -> list[str]:
    """What unbake refused, once per distinct text, led by the versions it concerns."""
    out = []
    for found in findings:
        rows = [gap.partition(": ") for gap in found.missing]
        out += [
            found.reason,
            *crack.collapse([(v.removeprefix("version "), t.replace("\n", " | ")) for v, _, t in rows], len(rows)),
        ]
    return out


_FRAME = re.compile(r"addiu\s+\$sp,\s*\$sp,\s*-")
_WORD = re.compile(r"/\*[^*]*\*/\s+(\w+\s.*?)\s*$")


def _entry(asm: str) -> list[str]:
    """The instructions in front of the first stack frame adjustment."""
    out: list[str] = []
    for line in asm.splitlines():
        if (found := _WORD.match(line.strip())) is None:
            continue
        if _FRAME.match(found[1]):
            return out
        out.append(" ".join(found[1].split()))
    return []


def entry_artifacts(snapshot: Snapshot, packet: Json) -> list[str]:
    """The versions whose target runs instructions before the frame that every other version lacks."""
    entries = {v: _entry(index.source(snapshot, t["asm"])) for v, t in packet["target"].items()}
    out = []
    for version, mine in sorted(entries.items()):
        others = [e for v, e in entries.items() if v != version]
        extra = [i for i in mine if others and all(i not in e for e in others)]
        if extra and all(len(e) < len(mine) for e in others):
            out.append(
                f"{version}: extra instructions at entry not present in other versions ({'; '.join(extra)}); "
                f"wrap the difference in #if VERSION_{version.upper().replace('-', '_')}"
            )
    return out


def _paired(diff: str) -> list[str]:
    """A unified diff with each run of target (-) lines interleaved with the built (+) run."""
    out: list[str] = []
    old: list[str] = []
    new: list[str] = []
    for line in [*diff.splitlines(), None]:
        if line and line[0] in "-+" and not line.startswith(("---", "+++")):
            (old if line[0] == "-" else new).append(line)
            continue
        out += [x for pair in zip_longest(old, new) for x in pair if x is not None]
        old, new = [], []
        if line is not None:
            out.append(line)
    return out


def _cut(lines: Sequence[str], limit: int) -> list[str]:
    return [*lines[:limit], f"... {len(lines) - limit} more lines"] if len(lines) > limit else list(lines)


def _first(diff: str, version: str) -> list[str]:
    return [
        f"## First differences in the lowest-scoring version {version}, target (-) against built (+)",
        *_cut(_paired(diff), _DIFF),
    ]


def first_open(rows: Sequence[Json]) -> str:
    """Where to start: the first region that differs, with its place and kind, else ""."""
    found = next(((i, r) for i, r in enumerate(rows, 1) if r["kind"] != "match"), None)
    if found is None:
        return ""
    i, r = found
    return f"region {i} at +0x{r['start']:X}" + f" ({r['anchors']})" * bool(r["anchors"]) + f": {r['kind']}"


def regions(rows: Sequence[Json], version: str) -> list[str]:
    """A fixed-size view of a function of any size: the count, the differing regions, the first few diffs."""
    bad = [(i, r) for i, r in enumerate(rows, 1) if r["kind"] != "match"]
    where = f" in {version}, the lowest-scoring version," if version else ""
    lines = [f"## Regions{where} matching {len(rows) - len(bad)} of {len(rows)}"]
    for i, r in bad[:_REGIONS]:
        lines.append(
            f"- region {i} at +0x{r['start']:X}, {r['size']} bytes: {r['kind']}"
            + f" ({r['anchors']})" * bool(r["anchors"])
        )
    lines += [f"... {len(bad) - _REGIONS} more differing regions"] * (len(bad) > _REGIONS)
    first = [line for _, r in bad[:_SHOWN] for line in r["lines"]]
    if first:
        lines += [
            "",
            f"## First differing regions{where} target (-) against built (+)",
            *_cut(_paired("\n".join(first)), _DIFF),
        ]
    return lines


def _near(snapshot: Snapshot, name: str) -> dict[str, int]:
    """The named symbols within a few words of `name` in the names_from version, with their signed distance."""
    symbols = snapshot.versions[snapshot.config.project.names_from].symbols
    at = symbols.get(name)
    return {
        n: a - at
        for n, a in sorted(symbols.items(), key=lambda t: t[1])
        if at is not None and n != name and abs(a - at) <= WINDOW
    }


def neighbours(snapshot: Snapshot, key: str, what: str) -> str:
    name = what.rsplit(" ", 1)[-1]
    near = _near(snapshot, name) if key == "recipe.neighbour" else {}
    if not near:
        return ""
    return "; neighbours: " + ", ".join(f"{n} at {d:+d}" for n, d in near.items())


def _left(snapshot: Snapshot, rows: Sequence[Left]) -> list[str]:
    out = []
    for row in rows:
        named = [n for n, d in _near(snapshot, row.base.removeprefix("&")).items() if d == row.offset]
        if named:
            out.append(f"- {row.base} at {row.offset:+d}: that offset is the symbol {named[0]}, not a field")
            continue
        if row.type:
            views = ", ".join(row.identity)
            tail = "another view has a field there" if row.elsewhere else "no view has a field there"
            out.append(f"- {row.base} at 0x{row.offset:X}: type {row.type}, views {views}; {tail}")
        else:
            fits = (
                f"; types with a field for every offset used on it: {', '.join(row.candidates)}"
                if row.candidates
                else "; none fits"
            )
            out.append(f"- {row.base} at 0x{row.offset:X}: the candidate declares no type for it{fits}")
    return out


def contradictions(snapshot: Snapshot, packet: Json) -> list[str]:
    """Where the target assembly disagrees with a declaration: a pointer global, a callee with extra arguments."""
    asm = index.source(snapshot, next(iter(packet["target"].values()))["asm"])
    calls, _, through = target.uses(asm)
    out = [
        f"- {n} is declared {k} but used as a pointer here: this is a declaration item"
        " (recipe next --kind declaration names it)"
        for n, k in target.misdeclared(asm, index.get(snapshot).declarations).items()
    ]
    out += [
        f"- the call to {n} passes arg{k} through untouched: the function takes {max(calls[n]) + 1} arguments"
        for n, ks in through.items()
        for k in ks
    ]
    for text in packet["types"]["declarations"]:
        if (found := re.search(r"(\w+)\(([^)]*)\)\s*;", text)) and (sent := calls.get(found[1])):
            count = 0 if found[2].strip() in ("", "void") else found[2].count(",") + 1
            if count <= max(sent):
                out.append(f"- {found[1]} is declared with {count} parameters but the call sets $a{max(sent)}")
    return out


def data_facts(snapshot: Snapshot, member: str) -> list[str]:
    """What the target says of a data item: its home, bytes as words and as ASCII, and how functions reach it."""
    entry, version, places, raw = draft.data_image(snapshot, member)
    symbol = draft.data_symbol(snapshot, entry, version, places[0].vram)
    sections = ", ".join(f"{p.section} 0x{p.vram:08X}+0x{p.size:X}" for p in places)
    out = ["## Data", f"symbol: {symbol} ({version}); {sections}"]
    out.append(f"definition goes in {entry.group}, in place of its INCLUDE_DATA line")
    out.append("bytes (offset, words, ASCII):")
    for at in range(0, min(len(raw), 16 * _DUMP), 16):
        row = raw[at : at + 16]
        words = " ".join(row[i : i + 4].hex().upper() for i in range(0, len(row), 4))
        out.append(f"+0x{at:04X}  {words:<35}  |{''.join(chr(b) if 32 <= b < 127 else '.' for b in row)}|")
    code = draft.data_code(snapshot, symbol, raw)
    if code and (decoded := draft.data_values(raw, code)):
        out.append(f"decoded as {draft.C_TYPES[code]}[{len(decoded)}]: {', '.join(decoded[: 4 * _DUMP])}")
    users: dict[str, list[str]] = {}
    for name, mnemonic, offset in index.get(snapshot).uses.get(symbol, ()):
        users.setdefault(name, []).append(f"{mnemonic} +0x{offset:X} ({_ACCESS.get(mnemonic, 'address')})")
    out.append(f"referenced by {len(users)} functions:" if users else "referenced by no function")
    return out + [f"- {n}: {', '.join(dict.fromkeys(u))}" for n, u in list(users.items())[:_USERS]]


def _sections(snapshot: Snapshot, packet: Json, fixes: Sequence[str], left: Sequence[Left]) -> list[str]:
    root = snapshot.config.project.root
    member = packet["member"]
    first = next(iter(packet["target"].values()))
    lines = [f"# {member}", "", f"kind: {packet['kind']}, size {first['size']} bytes", ""]
    lines += ["## Target assembly (read only, never edit)", f"file: {root / first['asm']}", ""] * (
        packet["kind"] != "data"
    )
    source = root / packet["best"]["source"]
    lines += ["## Candidate", f"file: {source}", f"score: {scores(packet['best']['score'])}"]
    lines.append("")
    if packet["kind"] == "data":
        lines += [*data_facts(snapshot, member), ""]
    if packet["best"]["errors"]:
        lines += ["## Does not compile", *_cut(packet["best"]["errors"], _ERRORS), ""]
    if why := [*(entry_artifacts(snapshot, packet) if packet["kind"] == "function" else []), *packet["why"]]:
        lines += ["## Why it is not exact", *[f"- {r}" for r in why], ""]
    if packet["twin"]:
        twin = packet["twin"]
        lines += [
            "## Twin",
            f"{twin['function']} in {twin['file']} ({twin['version']}); read it: recipe look {twin['function']}",
        ]
        lines += [""]
    if packet["regions"] or packet["diff"]:
        more = []
        if packet["diff"]:
            full = ladder.candidate_path(snapshot.config, member).with_suffix("") / "diff.txt"
            host.write(full, packet["diff"].encode())
            more = [f"full diff: {full}"] * (packet["diff"].count("\n") >= _DIFF)
        shown = (
            regions(packet["regions"], packet["worst"])
            if packet["regions"]
            else _first(packet["diff"], packet["worst"])
        )
        lines += [*shown, *more, ""]
    refused = errors.held(snapshot, source.read_text(encoding="utf-8"), member)
    if refused:
        lines += [
            "## Must change before try",
            *[f"- line {n}: {what}: {text}{neighbours(snapshot, key, what)}" for key, what, _, n, text in refused[:8]],
            *dict.fromkeys(f"fix: {why}" for _, _, why, _, _ in refused),
            "",
        ]
    if fixes:
        lines += ["## Fixed for you", *[f"- {f}" for f in fixes], ""]
    if left:
        lines += ["## Left as placeholders", *_left(snapshot, left), ""]
    if packet["kind"] == "function" and (disagree := contradictions(snapshot, packet)):
        lines += ["## Declarations the target assembly contradicts", *disagree, ""]
    types = packet["types"]
    lines += ["## Types", types["signature"] or "(no prototype known)", *types["declarations"], ""]
    start = first_open(packet["regions"])
    lines += [
        f"next: start at {start}; edit the file, then run: recipe try {member}"
        if start
        else f"next: recipe try {member}"
    ]
    return lines


def write(snapshot: Snapshot, member: str, packet: Json, auto_fixes: Sequence[str], left: Sequence[Left] = ()) -> Path:
    lines = _sections(snapshot, packet, auto_fixes, left)
    if len(lines) > MAX_LINES:
        lines = [*lines[: MAX_LINES - 2], "... brief cut", lines[-1]]
    path = ladder.candidate_path(snapshot.config, member).with_suffix("") / "brief.md"
    host.write(path, ("\n".join(lines) + "\n").encode())
    return path
