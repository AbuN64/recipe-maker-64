"""First-draft C for one function: m2c output with GBI rewrites (moved from unbake)."""

from __future__ import annotations

import math
import os
import re
import struct
from bisect import bisect_right
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path

from unbake import compare, effort, headers, layout, native, pool, process, recipes, report, store, symbols, versions
from unbake.contracts import Config, Finding, Json, Member, Placement, Refusal, Snapshot, UnitSpec, digest

from recipemaker import errors, gbi, idiom64, index, m2c_target, records, repair, resource, signatures, state

C_TYPES = {
    "I": "unsigned int",
    "b": "signed char",
    "B": "unsigned char",
    "h": "short",
    "H": "unsigned short",
    "f": "float",
    "d": "double",
}
_ACCESS = {"lb": "b", "lbu": "B", "lh": "h", "lhu": "H", "lwc1": "f", "swc1": "f", "ldc1": "d", "sdc1": "d"}


def _tables(snapshot: Snapshot, version: str) -> dict[str, str]:
    """Each jump table the layout holds as a data member of the version, as assembly read from its ROM bytes."""
    record, found = snapshot.versions[version], {}
    for name, member in snapshot.layout.members.items():
        places = sorted((p for p in member.placements if p.version == version), key=lambda p: p.vram)
        if not (name.startswith("jtbl_") and places):
            continue
        raw = b"".join(versions.rom_bytes(record, p.rom_start, p.rom_end) for p in places)
        if places[0].vram % 4 or len(raw) % 4:  # m2c cannot read this table: say so with its first word as bytes
            rows = [f"  .byte 0x{b:02X}" for b in raw]
        else:
            rows = [f"  .word .Lauto_{int.from_bytes(raw[i : i + 4], 'big'):08X}" for i in range(0, len(raw), 4)]
        found[name] = "\n".join([f"dlabel {name}", *rows, f"enddlabel {name}"])
    return found


def _inside(snapshot: Snapshot, version: str, name: str) -> str:
    """A jump table inside a larger data member, read from the version's symbol address to the next symbol."""
    record = snapshot.versions[version]
    start = record.symbols.get(name)
    places = [p for m in snapshot.layout.members.values() for p in m.placements if p.version == version]
    held = next((p for p in places if start is not None and p.vram <= start < p.vram + p.size), None)
    if start is None or held is None:
        why = "no address in the version's symbols" if start is None else "no data member holds it"
        return f"/* unresolved {name}: {why} */"
    stop = min([v for v in record.symbols.values() if v > start] + [held.vram + held.size])
    raw = versions.rom_bytes(record, held.rom_start + start - held.vram, held.rom_start + stop - held.vram)
    rows = [f"  .word .Lauto_{int.from_bytes(raw[i : i + 4], 'big'):08X}" for i in range(0, len(raw) - 3, 4)]
    return "\n".join([f"dlabel {name}", *rows, f"enddlabel {name}"])


def _label(text: str, member: str) -> str:
    """The function label the assembly defines for `member`: members of other versions are defined under their own."""
    names = re.findall(r"^glabel (\w+)", text, re.M)
    return member if member in names or not names else names[0]


def _retarget(wanted: list[str], text: str) -> list[str]:
    """Jump tables whose words lack the address high bit (an overlay relocates them at load): each word that is no
    address of the function but is one with the bit set is spelled as that label, so m2c finds the target."""
    here = {int(m[1], 16) for m in re.finditer(r"/\* \w+ (\w{8}) \w{8} \*/", text)}

    def fix(found: re.Match[str]) -> str:
        word = int(found[1], 16)
        return f".Lauto_{word | 0x80000000:08X}" if word not in here and word | 0x80000000 in here else found[0]

    return [re.sub(r"\.Lauto_([0-9A-Fa-f]{8})\b", fix, table) for table in wanted]


def _jumps(snapshot: Snapshot, member: str, version: str, asm: Path) -> list[str]:
    """The files m2c reads for `member`: its assembly and the jump tables it reads, with each table target labelled."""
    root, text = snapshot.config.project.root, asm.read_text(errors="replace")
    tables = effort.memo(("draft.jtbl", snapshot.digest, version), lambda: _tables(snapshot, version))
    names = list(dict.fromkeys(re.findall(r"%lo\((jtbl_\w+)\)", text)))
    wanted = [tables[n] if n in tables else _inside(snapshot, version, n) for n in names]
    if not wanted:
        return [str(asm)]
    wanted = _retarget(wanted, text)
    targets = set(re.findall(r"\.Lauto_(\w+)", "".join(wanted)))
    labelled = set(re.findall(r"^\s*\.Lauto_(\w+):", text, re.M))
    lines = [
        f"  .Lauto_{m[1]}:\n{line}"
        if (m := re.search(r"/\* \w+ (\w{8}) \w{8} \*/", line)) and m[1] in targets - labelled
        else line
        for line in text.splitlines(keepends=True)
    ]
    out = root / ".unbake/recipe/jumps" / member
    store.write(out / asm.name, "".join(lines).encode())
    store.write(out / "tables.s", (".section .rodata\n" + "\n".join(wanted) + "\n").encode())
    return [str(out / asm.name), str(out / "tables.s")]


def _m2c_call(item: tuple[Snapshot, str, str, bool, Path]) -> tuple[list[str], str]:
    """m2c's argv and its cache key."""
    snapshot, member, version, stack_structs, context = item
    config = snapshot.config
    unit = layout.unit_of(snapshot, member)
    target = m2c_target(unit.toolchain if unit is not None else config.project.toolchain)
    argv = [str(state.tool("m2c")), "-t", target, "--valid-syntax", *["--stack-structs"] * stack_structs]
    asm = versions.asm_path(config, version, member)
    files = _jumps(snapshot, member, version, asm)
    argv += ["--context", str(context), "--function", _label(asm.read_text(errors="replace"), member), *files]
    return argv, digest((argv, [Path(p).read_bytes() for p in files], os.stat(argv[0]).st_mtime_ns))


def _failure(stdout: str) -> str:
    """The reason m2c printed as a comment on stdout: its failure text, or the last line of an internal error."""
    blocks = re.findall(r"/\*\s*((?:Decompilation failure|Internal error)[\s\S]*?)\*/", stdout)
    if not blocks:
        return ""
    lines = [" ".join(x.split()) for x in blocks[-1].splitlines() if x.strip()]
    return " ".join(lines) if lines[0].startswith("Decompilation") else f"{lines[0].rstrip(':')}: {lines[-1]}"


def _misaligned(config: Config, member: str) -> str:
    """Name each jump table the function reads whose words begin at a .byte: m2c cannot read such a table."""
    tables = config.project.root / ".unbake/recipe/jumps" / member / "tables.s"
    text = tables.read_text(errors="replace") if tables.is_file() else ""
    names = re.findall(r"^dlabel (jtbl_\w+)\n\s*(?:/\*[^*]*\*/\s*)?\.byte\b", text, re.M)
    lost = re.findall(r"^/\* (unresolved jtbl_\w+: .*?) \*/$", text, re.M)
    return "; ".join(
        [f"jump table {n} is misaligned (it starts with .byte): the split alignment is wrong" for n in names] + lost
    )


def _function_text(item: tuple[Snapshot, str, str, bool, Path]) -> str:
    argv = _m2c_call(item)[0]
    config, member, version = item[0].config, item[1], item[2]
    with effort.stage(f"m2c {member} {'stack' if item[3] else 'flat'}"):  # one path per job, not one shared by all
        result = process.run("m2c", argv, cwd=config.project.root, tmp=process.scratch(config.project.root))
    if result.exit != 0 or not result.stdout.strip():
        reason = _failure(result.stdout.decode(errors="replace")) or "; ".join(
            result.stderr.decode(errors="replace").strip().splitlines()[-3:]
        )
        misaligned = _misaligned(config, member)
        reason = "; ".join(filter(None, [reason or f"m2c exited {result.exit} with no output", misaligned]))
        raise Refusal(Finding("draft.m2c", reason, unit=member, versions=(version,)))
    return result.stdout.decode(errors="replace")


def _member_of(snapshot: Snapshot, member: str) -> Member:
    entry = snapshot.layout.members.get(member)
    if entry is None or not entry.placements:
        raise Refusal(Finding("land.request", f"{member} is not a member of this layout", unit=member))
    return entry


def data_image(snapshot: Snapshot, member: str) -> tuple[Member, str, tuple[Placement, ...], bytes]:
    entry = _member_of(snapshot, member)
    version = entry.reference(snapshot.config.project.names_from)
    placements = tuple(
        sorted((p for p in entry.placements if p.version == version and p.section != ".bss"), key=lambda p: p.vram)
    )
    if not placements:
        raise Refusal(Finding("draft.data", "no ROM bytes to draft", unit=entry.name, versions=(version,)))
    record = snapshot.versions[version]
    return entry, version, placements, b"".join(versions.rom_bytes(record, p.rom_start, p.rom_end) for p in placements)


def data_symbol(snapshot: Snapshot, entry: Member, version: str, vram: int) -> str:
    named = sorted(n for n, v in snapshot.versions[version].symbols.items() if v == vram)
    own = entry.name.rsplit("/", 1)[-1].removesuffix(".c")
    return own if own in named else named[0] if named else f"D_{vram:08X}"


def data_code(snapshot: Snapshot, symbol: str, raw: bytes) -> str:
    """The struct code of the one element type every reference to the item agrees on, else ""."""
    codes = {_ACCESS.get(m, m) for _, m, _ in index.get(snapshot).uses.get(symbol, ()) if m not in ("addiu", "ori")}
    code = codes.pop() if len(codes) == 1 else ""
    return code if code in C_TYPES and not len(raw) % struct.calcsize(">" + code) else ""


def data_values(raw: bytes, code: str) -> list[str] | None:
    """Each element as a C literal: hex for bytes and words, the shortest round-tripping decimal for floats."""
    size, out = struct.calcsize(">" + code), []
    for at in range(0, len(raw), size):
        chunk, value = raw[at : at + size], struct.unpack(">" + code, raw[at : at + size])[0]
        if code in "BI":
            out.append(f"0x{value:0{2 * size}X}")
        elif code not in "fd":
            out.append(str(value))
        elif math.isfinite(value):
            text = next(
                t for t in (f"{value:.{p}g}" for p in range(1, 18)) if struct.pack(">" + code, float(t)) == chunk
            )
            text = format(Decimal(text), "f") if 1e-4 <= abs(value) < 1e15 else text  # plain where it reads well
            out.append(text + ".0" * (not set(".en") & set(text)) + "f" * (code == "f"))
        else:
            return None
    return out


def _into(
    snapshot: Snapshot,
    held: dict[str, tuple[list[tuple[int, int, Member]], list[int]]],
    version: str,
    value: int,
) -> tuple[Member, int] | None:
    """The innermost member of the version whose span holds the address `value`, with the offset into it."""
    if version not in held:
        spans = sorted(
            (
                (p.vram, p.vram + p.size, m)
                for m in snapshot.layout.members.values()
                for p in m.placements
                if p.version == version and p.size
            ),
            key=lambda span: span[:2],
        )
        held[version] = (spans, [lo for lo, _, _ in spans])
    spans, starts = held[version]
    found = bisect_right(starts, value) - 1
    while found >= 0 and spans[found][1] <= value:  # an outer span may hold what an inner one before it does not
        found -= 1
    if value < 0x80000000 or found < 0:
        return None
    target = spans[found][2]
    return target, value - min(p.vram for p in target.placements if p.version == version)


def _agrees(
    snapshot: Snapshot,
    held: dict[str, tuple[list[tuple[int, int, Member]], list[int]]],
    images: dict[str, bytes],
    entry: Member,
    version: str,
    at: int,
    hit: tuple[Member, int],
) -> bool:
    """Whether the same word of every other version points to the same member at the same offset: only then is it a
    pointer rather than a number that happens to fall inside a member here. `images` keeps each version's bytes."""
    for other in sorted({p.version for p in entry.placements} - {version}):
        if other not in images:
            parts = sorted((p for p in entry.placements if p.version == other and p.section != ".bss"), key=_vram)
            images[other] = b"".join(
                versions.rom_bytes(snapshot.versions[other], p.rom_start, p.rom_end) for p in parts
            )
        image = images[other]
        same = _into(snapshot, held, other, int.from_bytes(image[at : at + 4], "big")) if len(image) >= at + 4 else None
        if same is None or (same[0].name, same[1]) != (hit[0].name, hit[1]):
            return False
    return True


def _vram(p: Placement) -> int:
    return p.vram


def data_text(snapshot: Snapshot, member: str) -> str:
    entry, version, placements, raw = data_image(snapshot, member)
    symbol = data_symbol(snapshot, entry, version, placements[0].vram)
    code = data_code(snapshot, symbol, raw)
    values = data_values(raw, code) if code else None
    if values is None:
        code = "I" if len(raw) % 4 == 0 else "B"
        values = data_values(raw, code) or []
    referenced: dict[str, str] = {}
    held: dict[str, tuple[list[tuple[int, int, Member]], list[int]]] = {}
    images: dict[str, bytes] = {}
    for at in range(0, len(raw), 4) if code == "I" else ():
        value = int.from_bytes(raw[at : at + 4], "big")
        hit = _into(snapshot, held, version, value)
        if hit is None or not _agrees(snapshot, held, images, entry, version, at, hit):
            continue
        target, offset = hit
        base = value - offset
        if target.kind == "function":
            if offset == 0:
                referenced[target.name] = f"extern void {target.name}(void);"
                values[at // 4] = target.name
            continue
        name = data_symbol(snapshot, target, version, base)
        if target.name == entry.name:
            if offset % 4 == 0:
                values[at // 4] = f"&{name}[{offset // 4}]"
            continue
        referenced[name] = f"extern unsigned char {name}[];"
        values[at // 4] = f"&{name}[{offset}]" if offset else f"&{name}"
    qualifier = "const " if all(p.section == ".rodata" for p in placements) else ""
    lines = [f"    {', '.join(values[i : i + 8])}," for i in range(0, len(values), 8)]
    head = [referenced[name] for name in sorted(referenced)]
    return "\n".join([*head, f"{qualifier}{C_TYPES[code]} {symbol}[] = {{", *lines, "};", ""])


def refuse_fragment(snapshot: Snapshot, member: str) -> None:
    successor = report.split_slot(snapshot, member)
    if successor is not None:
        reason = f"{member} ends on a branch or jump whose delay slot is the first word of {successor}"
        raise Refusal(Finding("member.split-delay-slot", reason, unit=member))


# m2c's own annotations
_NOISE = re.compile(r"[ \t]*/\* (?:extern|size =? ?0x[0-9A-Fa-f]+) \*/|/\* 0x[0-9A-Fa-f]+ \*/ ?")


def declared_names(snapshot: Snapshot) -> dict[str, tuple[str, int, str]]:
    """Where each name a draft may use is declared or, for a macro, defined, read once per snapshot."""

    return effort.memo(("draft.declared", snapshot.digest), lambda: {**macros(snapshot), **headers.catalog(snapshot)})


def macros(snapshot: Snapshot) -> dict[str, tuple[str, int, str]]:
    """Each macro a project header defines, with its header."""

    def read() -> dict[str, tuple[str, int, str]]:
        return {
            m[1]: (path, 0, "")
            for path in headers.sources(snapshot)
            for m in re.finditer(r"^#define (\w+)", snapshot.read(path).decode(errors="replace"), re.M)
        }

    return effort.memo(("draft.macros", snapshot.digest), read)


def include_paths(declared: dict[str, tuple[str, int, str]], text: str) -> list[str]:
    return sorted({declared[n][0].removeprefix("include/") for n in set(re.findall(r"\w+", text)) & declared.keys()})


def _unread(text: str) -> str:
    """The text without the stores m2c makes to a stack slot that holds a saved register and nothing reads."""
    return re.sub(r"^[ \t]*(unksp\w+) = [^\n]*;\n", lambda m: "" if text.count(m[1]) == 1 else m[0], text, flags=re.M)


def _single_use(text: str) -> Callable[[re.Match[str]], str]:
    """A substitution that drops a match whose captured name occurs in `text` only there."""
    return lambda m: "" if text.count(m[1]) == 1 else m[0]


def _stages(
    snapshot: Snapshot,
    member: str,
    version: str,
    known: dict[str, str],
    callees: list[str],
    texts: list[str],
    helpers: dict[str, str],
) -> Iterator[Json]:
    """Candidate drafts, richest first: m2c with and without stack structs, GBI rewrites and project headers."""
    declared = declared_names(snapshot)
    named = known.keys() | declared.keys()
    defined = macros(snapshot)  # m2c declares a symbol a header spells as a macro: the extern cannot parse
    for text in texts:
        raw = re.sub(r"^.*?(\w+)\(.*\);\s*/\* extern \*/\n", lambda m: "" if m[1] in named else m[0], text, flags=re.M)
        raw = _NOISE.sub("", raw)
        raw = re.sub(r"^extern [^;(]*?\b(\w+);\n", lambda m: "" if m[1] in defined else m[0], raw, flags=re.M)
        raw = _unread(raw)
        raw = idiom64.collapse(raw, helpers)
        raw = re.sub(r"struct (_m2c_stack_\w+) \{[^}]*\};\n*", _single_use(raw), raw)
        kind = known.get(member, "").partition(member)[0]  # the project declares the function: define it the same way
        if kind:  # an undeclared function keeps the return type m2c gave it
            raw = re.sub(
                rf"^[^\n(=;]*\b{re.escape(member)}\((?=.*\) \{{$)", f"{kind}{member}(", raw, count=1, flags=re.M
            )
        raw = repair.repair(raw)
        raw, extras = _folded(snapshot, raw)
        raw = repair.members(raw, _structs(snapshot, extras)) if "M2C_BITWISE(" in raw else raw
        used = [n for n in callees if n not in helpers or n in raw]
        head = "".join(f"{known[n]}\n" for n in used if n in known and n not in declared)
        shaped, _, prelude = gbi.recover(raw)
        bodies = [(repair.uncast(shaped), prelude), (shaped, prelude), (repair.uncast(raw), ""), (raw, "")]
        for body, before in dict.fromkeys(bodies):  # the microcode precedes the headers; casts off first
            bare = "".join(f"{known[n]}\n" for n in used if n in known)  # prototypes stand in for headers that conflict
            for wanted, protos in ((include_paths(declared, head + body), head), ([], bare)):
                code = repair.unfake(repair.unknown(protos + body), member)  # no M2C_UNK, no per-function records
                yield {"text": before + "".join(f'#include "{p}"\n' for p in wanted) + code, "extras": extras}


def _headers(snapshot: Snapshot) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Every project header's text, where each struct is defined and each struct typedef's tag, once per snapshot."""

    def read() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        texts = {p: snapshot.read(p).decode(errors="replace") for p in headers.sources(snapshot)}
        return texts, records.where(texts), records.aliases(texts)

    return effort.memo(("draft.records", snapshot.digest), read)


def header_texts(snapshot: Snapshot) -> dict[str, str]:
    """Every project header's text, read once per snapshot."""
    return _headers(snapshot)[0]


def _folded(snapshot: Snapshot, text: str) -> tuple[str, dict[str, bytes]]:
    """The draft without the project structs m2c printed again, and the header edits that take their new fields."""
    texts, defined, alias = _headers(snapshot)
    out, edits, _ = records.fold(text, texts, defined, alias)
    return out, {p: t.encode() for p, t in edits.items()}


def _structs(snapshot: Snapshot, extras: dict[str, bytes]) -> dict[str, str]:
    """struct tag or typedef name -> its body text, the header edits applied."""
    texts, defined, alias = _headers(snapshot)
    found: dict[str, str] = {}
    for name, path in defined.items():
        text = extras[path].decode() if path in extras else texts[path]
        body = records.bodies(path, text).get(name)
        if body is not None:
            found[name] = body.text
    for name, tag in alias.items():
        if tag in found:
            found.setdefault(name, found[tag])
    return found


def _helpers(snapshot: Snapshot, version: str) -> dict[str, str]:
    """The 64-bit libgcc helpers the project names, by every name the assembly calls them."""
    try:
        table = symbols.load(snapshot.read, snapshot.config.project.versions)
    except Refusal:  # a project without a readable symbol table names no helper
        return {}
    return idiom64.aliases(table, version)


def with_macros(text: str) -> str:
    """The text as measured: m2c's macro definitions in front of a candidate that uses them, never in its file."""
    return resource("tables/m2c_macros.h").decode() + text if "M2C_" in text else text


def bind(snapshot: Snapshot, path: Path, member: str) -> tuple[UnitSpec, Snapshot]:
    if "M2C_" in (text := path.read_text(encoding="utf-8")):
        path = path.with_suffix("") / "measured.c"
        store.write(path, with_macros(text).encode())
    return compare.bind(snapshot, path, member)


def _checked(item: tuple[Snapshot, str, str, Json]) -> Json:
    """A pool job: the draft with why it does not compile under the project's toolchain ("" when it does)."""
    snapshot, member, version, draft = item
    text = draft["text"]
    unit, writes = layout.unit_options(snapshot, member, with_macros(text).encode())[-1]
    with store.work(snapshot.config) as work, effort.stage(f"compile {member} {digest(text)[:8]}"):
        proof = native.prove(
            layout.overlay(snapshot, {**draft.get("extras", {}), **writes}),
            unit,
            version,
            recipes.resolve(snapshot.config, unit, {}),
            work,
        )[0]
    return {**draft, "reason": next((m for m in proof.missing if errors.unbuilt(m)), "")}


def _text_key(job: tuple[Snapshot, str, str, bool, Path]) -> str:
    return str(_m2c_call(job)[1])


def create(snapshot: Snapshot, member: str, out: Path) -> str:
    """Draft the function `member` into `out`: why it is no draft ("" when it compiles)."""
    return drafted(snapshot, member, out)[0]


def drafted(snapshot: Snapshot, member: str, out: Path) -> tuple[str, dict[str, bytes]]:
    """Draft the function `member` into `out`: m2c and prototypes in one dispatch, then compile probes. Returns why it
    is no draft ("" when it compiles) and the header edits it compiles with (the struct fields m2c read)."""
    with effort.stage("draft.create"):
        config = snapshot.config
        entry = _member_of(snapshot, member)
        refuse_fragment(snapshot, member)
        version = entry.reference(config.project.names_from)
        asm = index.asm(snapshot, member, version)
        callees = [n for n in dict.fromkeys(re.findall(r"\bjal\s+(\w+)", asm)) if n != member]
        context = signatures.context(snapshot, version)  # built once here, never inside each m2c job
        jobs = [(snapshot, member, version, s, context) for s in (True, False)]
        texts = pool.map(config, "draft.m2c", _function_text, jobs, _text_key)
        known = signatures.declarations(snapshot, [member, *callees])
        helpers = _helpers(snapshot, version)
        stages = list(
            {s["text"]: s for s in _stages(snapshot, member, version, known, callees, texts, helpers)}.values()
        )
        results = pool.map(config, "draft.compile", _checked, [(snapshot, member, version, s) for s in stages])
        result = next((r for r in results if not r["reason"]), None) or _dereferenced(
            snapshot, member, version, results
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result["text"], encoding="utf-8")
        why = str(result["reason"]) or _hollow(entry, version, member, result["text"])
        return why, dict(result.get("extras", {})) if not why else {}


_ROUNDS = 4  # pointer levels a scalar may be given before the draft is left as it was


def _dereferenced(snapshot: Snapshot, member: str, version: str, results: list[Json]) -> Json:
    """The richest draft when none builds, else the first whose dereferenced scalars got pointer types until it does."""
    for first in (r for r in results if repair.refused(r["reason"])):
        now = first
        for _ in range(_ROUNDS):
            skew = with_macros(now["text"]).count("\n") - now["text"].count("\n")
            fixed = repair.deref(now["text"], now["reason"], skew)
            if fixed == now["text"]:
                break
            now = _checked((snapshot, member, version, {**now, "text": fixed}))
            if not now["reason"]:
                return now
    return results[0]


_EMPTY = 64  # bytes: a function this large with no statement was not decompiled


def _hollow(entry: Member, version: str, member: str, text: str) -> str:
    """Why a draft that compiles is still no draft: m2c left a large function with an empty body."""
    size = sum(p.size for p in entry.placements if p.version == version)
    body = re.search(rf"\b{re.escape(member)}\s*\([^;{{)]*\)\s*\{{\s*\}}", text)
    return f"m2c produced an empty body for a {size} byte function" if body and size >= _EMPTY else ""


def skeleton(snapshot: Snapshot, member: str) -> str:
    """What `try` starts from when m2c cannot draft a function: its prototype, an empty body and the target assembly."""
    entry = _member_of(snapshot, member)
    version = entry.reference(snapshot.config.project.names_from)
    known = signatures.declarations(snapshot, [member]).get(member)
    signature = known.removesuffix(";") if known else f"void {member}(void)"
    asm = versions.asm_path(snapshot.config, version, member).relative_to(snapshot.config.project.root)
    wanted = include_paths(declared_names(snapshot), signature)
    return "".join(f'#include "{p}"\n' for p in wanted) + f"\n/* target assembly: {asm} */\n{signature} {{\n}}\n"
