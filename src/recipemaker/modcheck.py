"""Module-map contiguity check, the dry check of `recipe structure --map` (adopted from CLEAN-MAP-O1's modcheck.py).

A module map is a TSV with a header row; the check reads its `unit` (a source path as the link script names it without
the `.o`) and `module` (the merged file the unit belongs to) columns, in module order: the units of one module, in the
order the map gives them, are the order of the merged file.

What it proves for every module of 2+ units, in every version:
  1. per output section and per section class (text, data, rodata, bss), the module's non-empty link entries are
     ADJACENT (no other non-empty entry of any class between them), so one merged object takes their place in place;
  2. the module's entries of one class sit in one output section;
  3. the units appear in the same relative order in every class and every version (the map's order);
  4. all units share one compile identity (toolchain + effective cflags), a hard translation-unit boundary.
Not failures but reported: an `ALIGN(n)` between two module entries that inserts padding (the merged object must
reproduce the padding itself) and the entries of another class between a module's own entries (`reclass`: zero
alignment padding gcc emits itself, a constant declared without `const`, or data inside a text run).

Project facts are arguments: the link script per version, the object per version and unit, the versions, and the
unbake-shaped config.toml with its `[library."dir/"]` and `[file."path"]` tables."""

import os
import re
import struct
import tomllib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

CLASS_OF = {".text": "text", ".data": "data", ".rodata": "rodata", ".rdata": "rodata", ".bss": "bss", ".sbss": "bss"}
ENTRY = re.compile(r"^\s*(\S+?)\.o\(([^)]*)\);")
ALIGN = re.compile(r"^\s*\.\s*=\s*ALIGN\((\d+)\)")
OUTSEC = re.compile(r"^\s*(\.[A-Za-z0-9_.]+)\s+(?:0x[0-9A-Fa-f]+\s*)?(?:\(NOLOAD\)\s*)?:")
LINK = "build/{v}/link.ld"
OBJECT = "build/{v}/{unit}.o"
Facts = dict[str, list[dict[str, Any]]]


class Reclass(NamedTuple):
    module: str
    unit: str
    action: str  # "drop" (zero padding), "const" (data to rodata) or "text" (data inside code)
    versions: tuple[str, ...]


def elf_sizes(path: str) -> dict[str, int]:
    """{class: bytes} of one ELF32 object, from its section headers."""
    out: dict[str, int] = defaultdict(int)
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] != b"\x7fELF":
        raise ValueError(f"{path} is not ELF")
    e = ">" if data[5] == 2 else "<"
    shoff = struct.unpack_from(e + "I", data, 0x20)[0]
    shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", data, 0x2E)
    hdrs = [struct.unpack_from(e + "IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)]
    stro = hdrs[shstrndx][4]
    for h in hdrs:
        name = data[stro + h[0] : data.index(b"\0", stro + h[0])].decode()
        if cls := CLASS_OF.get(name):
            out[cls] += h[5]
    return dict(out)


def parse_ld(path: str) -> list[tuple[str | None, str, str, tuple[str, ...], int]]:
    """[(output section, kind, unit, classes, align)] in script order; kind is `entry` or `align`."""
    rows: list[tuple[str | None, str, str, tuple[str, ...], int]] = []
    outsec: str | None = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            m = OUTSEC.match(line)
            if m and "{" not in line and not line.strip().startswith(("src", "assets")):
                outsec = m.group(1)
                continue
            if m := ALIGN.match(line):
                rows.append((outsec, "align", "", (), int(m.group(1))))
            elif m := ENTRY.match(line):
                classes = tuple(sorted({CLASS_OF[s] for s in m.group(2).split() if s in CLASS_OF}))
                rows.append((outsec, "entry", m.group(1), classes, 0))
    return rows


def identity(config: Path | str) -> Callable[[str], str]:
    """unit -> compile identity (toolchain and effective flags) from the project's config.toml tables."""
    if not config or not os.path.exists(config):
        return lambda unit: "assembler" if unit.endswith((".s", ".S")) else ""
    with open(config, "rb") as handle:
        cfg = tomllib.load(handle)
    default = cfg.get("toolchain", "")
    libs, files = cfg.get("library", {}), cfg.get("file", {})

    def ident(unit: str) -> str:
        if unit.endswith((".s", ".S")):
            return "assembler"  # an assembled object never merges with a compiled one
        tc, flags = default, list[str]()
        for prefix in sorted(libs, key=len):
            if unit.startswith(prefix):
                tc = libs[prefix].get("toolchain", tc)
                flags = list(libs[prefix].get("cflags", flags))
        if unit in files:
            tc = files[unit].get("toolchain", tc)
            flags = list(files[unit].get("cflags", flags))
        return str(tc) + " " + " ".join(flags)

    return ident


def facts(tree: str, versions: Sequence[str], ld: str = LINK, obj: str = OBJECT) -> Facts:
    """Per version: the link rows with each entry's byte size and its start offset in its output section."""
    out: Facts = {}
    cache: dict[tuple[str, str], dict[str, int] | None] = {}
    for v in versions:
        seq: list[dict[str, Any]] = []
        pos: dict[str | None, int] = defaultdict(int)
        for outsec, kind, unit, classes, align in parse_ld(os.path.join(tree, ld.format(v=v))):
            if kind == "align":
                pad = (-pos[outsec]) % align
                pos[outsec] += pad
                seq.append({"o": outsec, "k": "align", "a": align, "pad": pad})
                continue
            key = (v, unit)
            if key not in cache:
                path = os.path.join(tree, obj.format(v=v, unit=unit))
                cache[key] = elf_sizes(path) if os.path.exists(path) else None
            sizes = cache[key]
            size = sum((sizes or {}).get(c, 0) for c in classes)
            cls = classes[0] if len(classes) == 1 else "+".join(classes)
            seq.append(
                {"o": outsec, "k": "entry", "u": unit, "c": cls, "n": size, "at": pos[outsec], "missing": sizes is None}
            )
            pos[outsec] += size
        out[v] = seq
    return out


def unbuilt(fx: Facts, scope: str = "src/") -> list[str]:
    """The linked units whose object is missing in some version (a tree that is not built proves nothing)."""
    return sorted(
        {e["u"] for seq in fx.values() for e in seq if e["k"] == "entry" and e["missing"] and e["u"].startswith(scope)}
    )


def read_map(path: str) -> list[tuple[str, str]]:
    """[(unit, module)] in map order."""
    rows: list[tuple[str, str]] = []
    with open(path, encoding="utf-8") as handle:
        head = handle.readline().rstrip("\n").split("\t")
        ui, mi = head.index("unit"), head.index("module")
        for line in handle:
            if line.strip() and not line.startswith("#"):
                cols = line.rstrip("\n").split("\t")
                rows.append((cols[ui], cols[mi]))
    return rows


def check(
    fx: Facts, rows: Sequence[tuple[str, str]], ident: Callable[[str], str]
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[tuple[Any, ...]]]:
    """(fails, softs, reclass) for every multi-unit module. A fail is (module, version, class, why)."""
    order: dict[str, int] = {}
    modules: dict[str, list[str]] = defaultdict(list)
    for u, m in rows:
        modules[m].append(u)
        order[u] = len(order)
    fails: list[tuple[str, ...]] = []
    softs: list[tuple[str, ...]] = []
    reclass: list[tuple[Any, ...]] = []
    for m, units in modules.items():
        ids = {ident(u) for u in units}
        if len(ids) > 1:
            fails.append((m, "*", "flags", f"units differ in toolchain/cflags: {sorted(ids)}"))
    multi = {m: set(us) for m, us in modules.items() if len(us) > 1}
    unit_mod = {u: m for m, us in multi.items() for u in us}
    for v, seq in fx.items():
        live = [i for i, e in enumerate(seq) if e["k"] == "entry" and e["n"] > 0]
        where: dict[tuple[str, str], list[tuple[int, str, str]]] = defaultdict(list)
        for i in live:
            e = seq[i]
            if mod := unit_mod.get(e["u"]):
                where[(mod, e["c"])].append((i, e["o"], e["u"]))
        nxt = dict(pairwise(live))
        for (mod, cls), hits in where.items():
            outs = {o for _, o, _ in hits}
            if len(outs) > 1:
                fails.append((mod, v, cls, f"entries in several output sections {sorted(outs)}"))
                continue
            for (i, _, ua), (j, _, ub) in pairwise(hits):
                if nxt.get(i) != j:
                    between = [seq[k] for k in live if i < k < j]
                    if all(unit_mod.get(e["u"]) == mod for e in between):
                        # one object cannot interleave its sections: data inside a text run is code (or a literal the
                        # splitter cut out), data inside a rodata run is rodata (a constant declared without const, or
                        # the zero padding gcc emits between literals), a rodata run broken by data means the data
                        # entries at its ends are the misdeclared ones
                        if cls in ("text", "rodata"):
                            targets = [(seq[k], cls) for k in range(i + 1, j) if seq[k]["k"] == "entry"
                                       and seq[k]["n"] > 0 and seq[k]["c"] == "data"]  # fmt: skip
                        else:
                            targets = [(seq[i], "rodata"), (seq[j], "rodata")]
                        reclass += [(mod, v, e["u"], e["c"], to, e["o"], e["at"], e["n"]) for e, to in targets]
                    else:
                        txt = [e["u"] + "(" + e["c"] + ")" for e in between if unit_mod.get(e["u"]) != mod][:4]
                        fails.append((mod, v, cls, f"{ua} .. {ub} not adjacent: {', '.join(txt)}"))
                if order[ua] > order[ub]:
                    fails.append((mod, v, cls, f"order {ub} before {ua} differs from the map"))
                softs += [
                    (mod, v, cls, f"ALIGN({seq[k]['a']}) pads {seq[k]['pad']} B before {ub}")
                    for k in range(i + 1, j)
                    if seq[k]["k"] == "align" and seq[k]["pad"]
                ]
    return fails, softs, reclass


def roms(tree: str, versions: Sequence[str], rom: str, ld: str = LINK) -> Mapping[str, tuple[bytes, dict[str, int]]]:
    """Per version (ROM bytes, {output section: ROM offset}) for the zero-padding test (no ROM: left out)."""
    out: dict[str, tuple[bytes, dict[str, int]]] = {}
    for v in versions:
        path = os.path.join(tree, rom.format(v=v))
        if not os.path.exists(path):
            continue
        secs: dict[str, int] = {}
        with open(os.path.join(tree, ld.format(v=v)), encoding="utf-8") as handle:
            for line in handle:
                if m := re.match(r"\s*(\.[\w.]+)\s+(0x[0-9A-Fa-f]+)\s*:\s*AT\((0x[0-9A-Fa-f]+)\)", line):
                    secs[m.group(1)] = int(m.group(3), 16)
        with open(path, "rb") as handle:
            out[v] = (handle.read(), secs)
    return out


def reclassify(found: Sequence[tuple[Any, ...]], held: Mapping[str, tuple[bytes, dict[str, int]]]) -> list[Reclass]:
    """The unit-level actions a `check` reclass list asks for: zero bytes that go into rodata are dropped (the merged
    object pads itself), other data that goes into rodata is declared `const`, data inside a text run joins the code."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for mod, v, unit, _frm, to, outsec, at, size in found:
        zero = False
        if v in held and outsec in held[v][1]:
            rom, secs = held[v]
            zero = not any(rom[secs[outsec] + at : secs[outsec] + at + size])
        one = seen.setdefault((mod, unit), {"to": to, "zero": True, "vs": set()})
        one["zero"] = one["zero"] and zero
        one["vs"].add(v)
    return [
        Reclass(
            mod,
            unit,
            "drop" if d["zero"] and d["to"] == "rodata" else "const" if d["to"] == "rodata" else "text",
            tuple(sorted(d["vs"])),
        )
        for (mod, unit), d in seen.items()
    ]
