"""`recipe structure --map MODULES.tsv` (T7): apply a module map. Units that the map puts in one module are merged
into that module's file in map order, a unit moves to its module's path, every unit's address header is merged into
the module's header and the includes follow. The result is one unified diff (`recipe apply` applies it and runs
`unbake sync`, which regenerates the link script and the version maps).

What it refuses, with the module and the reason (the rest of the selection still goes through):
- a module the contiguity check fails (units not adjacent in some version's link order, order differing from the
  map, mixed toolchain or flags);
- a module of assembler units, or one that mixes assembler with C;
- two units that define the same name with different text;
- a module part way merged (some of its units gone, some left);
- a module whose data has to join the code of its run (`text` reclass): by hand.
Zero alignment padding units a reclass finds are dropped, constants declared without `const` get it.

The map is a TSV with a header row; columns `unit`, `module`, `header`, `stage` are read, the rows of one module are
the units of its file in the order given."""

import csv
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from unbake.contracts import Json

from recipemaker import cparse, modcheck

_ASM = (".s", ".S")
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*([<"])([^>"\n]*)[>"][^\n]*$')
_SIGNATURE = re.compile(r"(\w+)\s*\([^;{]*\)\s*\{|(\w+)\s*(?:\[[^\]]*\]\s*)*(?:=|;)")
GUARD = re.compile(r"\A\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b[^\n]*\n(.*)\n\s*#\s*endif[^\n]*\s*\Z", re.S)


CONFIDENCE = {"pending": 0, "low": 1, "medium": 2, "high": 3}  # a map's name confidence; `n/a` (data) always passes


def read(path: Path) -> list[dict[str, str]]:
    """The map rows as dicts, in file order."""
    with open(path, encoding="utf-8", newline="") as handle:
        return [r for r in csv.DictReader((l for l in handle if l.strip() and not l.startswith("#")), delimiter="\t")]


def groups(rows: Iterable[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """module path -> its rows in map order."""
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        out[row["module"]].append(row)
    return dict(out)


def chosen(
    modules: dict[str, list[dict[str, str]]], select: Sequence[str] = (), limit: int = 0
) -> dict[str, list[dict[str, str]]]:
    """The modules a run takes: those whose module path starts with one of SELECT (all when empty) and that still have
    something to do (a unit not at its module path), until LIMIT units are taken (a module is never cut)."""
    out: dict[str, list[dict[str, str]]] = {}
    taken = 0
    for module, rows in modules.items():
        if select and not any(module.startswith(s) for s in select):
            continue
        if all(r["unit"] == module for r in rows) or any(r["kind"] in ("orphan", "support") for r in rows):
            continue
        if limit and taken and taken + len(rows) > limit:
            continue
        out[module] = rows
        taken += len(rows)
    return out


def split_unit(text: str) -> tuple[list[str], str]:
    """(the top include lines, the rest) of one unit: the includes the file opens with, blank lines between them."""
    lines = text.splitlines(keepends=True)
    at = 0
    while at < len(lines) and (not lines[at].strip() or _INCLUDE.match(lines[at].rstrip("\n"))):
        at += 1
    return [l.rstrip("\n") for l in lines[:at] if l.strip()], "".join(lines[at:])


def _spelling(line: str) -> str:
    found = _INCLUDE.match(line)
    return found[2] if found else ""


def _mask(text: str) -> str:
    """TEXT with every comment blanked, newlines and offsets kept: a `;` in a comment ends no statement."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _defined(stmt: str) -> str:
    """The name a definition statement introduces ('' when it defines none): a function or variable, a record or
    enum by its tag (`struct Tag`), a typedef by its alias."""
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", stmt, flags=re.S).strip()
    if text.startswith("extern") or ("{" not in text and "=" not in text):
        return ""
    if text.startswith("typedef"):
        alias = re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*;\s*$", text)
        return f"typedef {alias[1]}" if alias else ""
    tag = re.match(r"(?:static\s+)?(struct|union|enum)\s+(\w+)\s*\{", text)
    if tag:
        return f"{tag[1]} {tag[2]}"
    found = re.search(r"(\w+)\s*\([^;{]*\)\s*\{", text) or re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*=", text)
    return found[1] if found else ""


def _declared(stmt: str) -> str:
    """The name a declaration statement (extern data or a prototype) introduces ('' when it is none)."""
    text = _norm(stmt).removeprefix("extern ")
    if "{" in text or "=" in text or text.startswith(("typedef", "struct", "union", "enum", "static", "#")):
        return ""
    found = re.search(r"(\w+)\s*\(", text) if "(" in text else re.search(r"(\w+)\s*(?:\[[^\]]*\]\s*)*;$", text)
    return found[1] if found else ""


def disagreements(units: Sequence[str], src: dict[str, str]) -> list[str]:
    """Names that two units declare (extern or prototype, outside #if) with different text: the merged file may not
    compile (a conflicting type), where the compiler is the judge."""
    seen: dict[str, tuple[str, str]] = {}
    out: list[str] = []
    for unit in units:
        masked, depth = _mask(src[unit]), 0
        for lo, hi, directive in cparse.spans(masked):
            stmt = masked[lo:hi]
            if directive:
                word = stmt.lstrip().removeprefix("#").lstrip()
                depth += word.startswith("if") - word.startswith("endif")
            elif not depth and (name := _declared(stmt)):
                text = _norm(stmt).removeprefix("extern ")
                if name in seen and seen[name][0] != text and seen[name][1] != unit:
                    out.append(name)
                seen.setdefault(name, (text, unit))
    return list(dict.fromkeys(out))


def dedupe(text: str) -> tuple[str, list[str]]:
    """(text, clashes): a top-level statement identical to an earlier one outside any #if is dropped; two statements
    outside #if that define one name with different text are a clash."""
    depth, seen, named, drop, clashes = 0, set(), {}, [], []
    masked = _mask(text)
    for lo, hi, directive in cparse.spans(masked):
        stmt = masked[lo:hi]
        if directive:
            word = stmt.lstrip().removeprefix("#").lstrip()
            depth += word.startswith("if") - word.startswith("endif")
            continue
        if depth:
            continue
        key = _norm(stmt)
        if key in seen:
            drop.append((lo, hi))
            continue
        seen.add(key)
        name = _defined(stmt)
        if name:
            if name in named and named[name] != key:
                clashes.append(name)
            named[name] = key
    for lo, hi in reversed(drop):
        hi += text[hi : hi + 1] == "\n"
        text = text[:lo] + text[hi:]
    return text, clashes


def own_header(units_files: Any, unit: str) -> str | None:
    """The address header of one unit (include/ + the unit's path without `src/` or `src/<top>/`, extension .H or .h)."""
    parts = Path(unit).with_suffix("").parts
    if parts[0] != "src":
        return None
    for tail in (parts[1:], parts[2:]):
        for ext in (".H", ".h"):
            candidate = "include/" + "/".join(tail) + ext
            if units_files.get(candidate) is not None:
                return candidate
    return None


def fold(files: Any, path: str) -> str:
    """PATH spelled as the tree already spells it when only the case differs (`save_3.h` beside `save_3.H`)."""
    held = getattr(files, "_lower", None)
    if held is None or held[0] != len(files.text):
        held = (len(files.text), {p.lower(): p for p in files.everything()})
        files._lower = held
    return held[1].get(path.lower(), path)


def guard_of(header: str) -> str:
    return re.sub(r"\W", "_", header.removeprefix("include/")).upper()


def merge_headers(files: Any, target: str, olds: Sequence[str]) -> str | None:
    """Merge the headers OLDS into TARGET (made when absent); None when done, else why not."""
    includes: list[str] = []
    bodies: list[str] = []
    moved = {o.removeprefix("include/") for o in olds} | {target.removeprefix("include/")}
    existing = files.get(target) if target not in olds else None
    parts = [(o, files.get(o)) for o in olds]
    if existing is not None:
        parts.insert(0, (target, existing))
    for path, text in parts:
        found = GUARD.match(text or "")
        if not found:
            return f"{path} has no include guard of the shape #ifndef X / #define X / #endif"
        head, body = split_unit(found[2])
        includes += [l for l in head if _spelling(l) not in moved]
        bodies.append(body.strip("\n"))
    seen: set[str] = set()
    uniq = [l for l in includes if not (l in seen or seen.add(l))]
    guard = guard_of(target)
    text = f"#ifndef {guard}\n#define {guard}\n" + "".join(l + "\n" for l in uniq)
    text += "\n" + "\n\n".join(b for b in bodies if b) + "\n#endif\n"
    text, clashes = dedupe(text)
    if clashes:
        return f"{target}: {', '.join(clashes)} defined twice with different text"
    files.put(target, text)
    return None


def merge_module(
    files: Any, module: str, units: Sequence[str], header: str, headers: dict[str, str], src: dict[str, str], notes: list[str]
) -> tuple[str, str]:
    """(text, why): UNITS (read from SRC, in map order) merged into one file for MODULE with its HEADER. The module
    header and the unit headers are edited in FILES now; the units themselves are written by the caller, once every
    module of the run is known (a module's path is often a unit of another module)."""
    if any(u.endswith(_ASM) for u in units):
        return "", "assembler units merge by hand"
    header = fold(files, header) if header.startswith("include/") else header
    olds = [h for u in units if header.startswith("include/") and (h := own_header(files, u))]
    moved = {o.removeprefix("include/") for o in olds}
    ours = header.removeprefix("include/")
    top: list[str] = []
    body = ""
    for u in units:
        head, rest = split_unit(src[u])
        for line in head:
            line = f'#include "{ours}"' if _spelling(line) in moved else line
            if line not in top:
                top.append(line)
        body += ("\n" if body and not body.endswith("\n\n") else "") + rest
    if olds and f'#include "{ours}"' not in top:
        top.append(f'#include "{ours}"')
    merged, clashes = dedupe(body)
    if clashes:
        return "", f"{', '.join(dict.fromkeys(clashes))} defined twice with different text"
    notes.extend(f"{module}: {n} declared with different text in two units" for n in disagreements(units, src))
    if olds and (why := merge_headers(files, header, olds)):
        return "", why
    for old in olds:
        headers[old.removeprefix("include/")] = ours
    return "".join(l + "\n" for l in top) + ("\n" if top else "") + merged, ""


def prepare(src: dict[str, str], action: str, unit: str) -> str:
    """The reclass edit of one unit's text in SRC (why not, '' when done): `drop` is the caller's (zero padding the
    merged object pads itself), `const` declares its definitions const; `text` (data inside the code) is by hand."""
    if action == "drop":
        return ""
    if action == "const":
        text = src[unit]
        masked = _mask(text)
        for lo, hi, _directive in reversed([s for s in cparse.spans(masked) if not s[2]]):
            stmt, shown = text[lo:hi], masked[lo:hi]
            if "=" in shown and not re.search(r"\bconst\b|\bextern\b|\btypedef\b", shown.split("=")[0]):
                text = text[:lo] + re.sub(r"^(static\s+)?", lambda m: (m[1] or "") + "const ", stmt, count=1) + text[hi:]
        src[unit] = text
        return ""
    return f"{unit}: data inside the code run of its module joins the code by hand"


def rewrite_includes(files: Any, headers: dict[str, str]) -> int:
    """The includes of every file that spell a merged header now spell the module header; the merged headers go."""
    done = 0
    for old, new in headers.items():
        pattern = re.compile(rf'(#[ \t]*include[ \t]*[<"])(?:include/)?{re.escape(old)}([>"])')
        for path in files.including(old, "include/" + old):
            text = files.get(path)
            if text is not None and path != "include/" + new:
                changed, n = pattern.subn(lambda m: f"{m[1]}{new}{m[2]}", text)
                if n:
                    files.put(path, changed)
                    done += n
        if old != new and files.get("include/" + old) is not None:
            files.text["include/" + old] = None
    return done


def _rekey_rows(files: Any, where: dict[str, str]) -> None:
    """The `[file."unit"]` rows of config.toml follow the units to their module's path (the first row of a module
    stays, as the units of a module share their flags); a row for a unit that is gone goes."""
    text = files.get("config.toml")
    if not text:
        return
    row = re.compile(r'''(?ms)^([ \t]*\[[ \t]*file[ \t]*\.[ \t]*)(["\'])([^"\'\n]+)\2([ \t]*\][^\n]*\n.*?)(?=^\[|\Z)''')
    taken: set[str] = set()

    def one(m: re.Match[str]) -> str:
        path = m[3]
        dest = where.get(path)
        if dest is None:
            return m[0]
        if dest in taken:
            return ""
        taken.add(dest)
        return f"{m[1]}{m[2]}{dest}{m[2]}{m[4]}"

    files.put("config.toml", row.sub(one, text))


def run(config: Any, params: Json) -> Json:
    """`recipe structure --map FILE`: the patch that applies the map (see the module doc)."""
    from recipemaker import structure  # structure imports this module lazily

    root = Path(config.project.root)
    rows = read(Path(params["map"]))
    wanted = chosen(groups(rows), params.get("module") or (), int(params.get("units") or 40))
    files = structure.Files(root)
    stage = params.get("stage") or "now"
    refused: list[Json] = []
    deferred: list[str] = []
    todo: dict[str, list[dict[str, str]]] = {}
    for module, mrows in wanted.items():
        present = [r for r in mrows if files.get(r["unit"]) is not None]
        if not present:
            continue  # done already
        if len(present) != len(mrows):
            refused.append({"module": module, "reason": "part way merged: " + ", ".join(r["unit"] for r in mrows if r not in present)})
        elif stage == "now" and any(r["stage"] != "now" for r in mrows):
            deferred.append(module)
        else:
            todo[module] = present
    reclass: dict[str, list[modcheck.Reclass]] = defaultdict(list)
    if todo:
        tree = str(root)
        fx = modcheck.facts(tree, config.project.versions)
        gone = set(modcheck.unbuilt(fx))
        ident = modcheck.identity(root / "config.toml")
        existing = [(r["unit"], r["module"]) for r in rows if files.get(r["unit"]) is not None]
        fails, _soft, found = modcheck.check(fx, existing, ident)
        bad: dict[str, str] = {}
        for f in fails:
            bad.setdefault(str(f[0]), f"{f[1]} {f[2]}: {f[3]}")
        held = modcheck.roms(tree, config.project.versions, params.get("rom") or "roms/baserom.{v}.z64")
        for r in modcheck.reclassify(found, held):
            reclass[r.module].append(r)
        for module, mrows in list(todo.items()):
            why = bad.get(module) or (
                "tree not built: " + ", ".join(sorted(gone & {r["unit"] for r in mrows})[:3])
                if gone & {r["unit"] for r in mrows}
                else ""
            )
            if why:
                refused.append({"module": module, "reason": why})
                del todo[module]
    floor = CONFIDENCE.get(str(params.get("floor") or "medium"), 2)
    headers: dict[str, str] = {}
    merged: list[str] = []
    dropped: list[str] = []
    written: dict[str, str] = {}
    where: dict[str, str] = {}
    notes: list[str] = []
    src = {r["unit"]: files.get(r["unit"]) or "" for mrows in todo.values() for r in mrows}
    for module, mrows in todo.items():
        units = [r["unit"] for r in mrows]
        snap = (dict(files.text), dict(files.origin))
        mine = dict(src)
        why, text, left = "", "", []
        for r in reclass.get(module, ()):
            if r.unit in units:
                why = why or prepare(mine, r.action, r.unit)
                left += [r.unit] if r.action == "drop" else []
        keep = [u for u in units if u not in left]
        target, header = module, mrows[0]["header"]
        if CONFIDENCE.get(mrows[0]["confidence"], 3) < floor and keep:
            # a name below the floor is not given yet: the units merge under the first one's address stem and header
            target, header = keep[0], (own_header(files, keep[0]) or "-")
            if len(keep) == 1 and not left:
                continue
        if not why and keep:
            text, why = merge_module(files, target, keep, header, headers, mine, notes)
        if why:
            files.text.clear()
            files.text.update(snap[0])
            files.origin.clear()
            files.origin.update(snap[1])
            files._spelled = None  # the include index is rebuilt from the restored texts
            refused.append({"module": module, "reason": why})
        else:
            merged.append(module)
            dropped += left
            written[target] = text
            where[module] = target
    for module in merged:  # every unit goes first: a module's path is often the path of a unit of another module
        for r in todo[module]:
            files.text[r["unit"]] = None
    for module, text in written.items():
        if text:
            files.put(module, text)
    rewrite_includes(files, headers)
    _rekey_rows(files, {r["unit"]: where[m] for m in merged for r in todo[m]})
    patch = files.diff()
    return {
        "patch": patch,
        "files": files.changed(),
        "moves": len(merged),
        "modules": [where[m] for m in merged],
        "refused": refused,
        "deferred": deferred,
        "notes": notes,
        "dropped": dropped,
        "units": sum(len(wanted[m]) for m in merged),
    }
