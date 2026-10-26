"""Slice packs (CLEAN-PLAN-O1 T4): `recipe name next --step S --target SLICE --count 20 --out DIR`.

One read-only pack per call, built on the shared warm tree (nothing is written to the tree, no lease, no patch). A card
per function carries everything a lane needs to name the function, its parameters and its locals in one go: the body,
callers and callees by current name, the strings the code loads, the SDK / debug-catalog hits, a proposal for every
unnamed slot and every unnamed member the body touches, the suggestions other lanes left for it, and row skeletons
keyed by the ledger's stable keys. When the slice has fewer open functions than the count, its data fills the pack.

Evidence forms added here (checked by `verifies`, so a leaf function with no caller or callee is still namable):
  body L<n>: <note>    line n of the target's source is inside its function; a quoted note must occur on that line
  string TEXT          a string the target loads (or the datum's own bytes) contains TEXT
  sdk NAME             the project's libultra table names the target's address NAME"""

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from unbake.contracts import Config, Json

from recipemaker import access, debug, host, locals_, slices, state, structure, style, table

BODY_LIMIT = 4000
PRINTABLE = re.compile(r"[\x20-\x7e\n\t]{4,}")
_BYTES = re.compile(r"\.byte\s+([^\n#]+)")
_TEXT = re.compile(r"\.(?:ascii|asciz|string)\s+\"((?:[^\"\\]|\\.)*)\"")
_NOT_RECORDS = {"return", "void", "char", "int", "short", "long", "float", "double", "unsigned", "signed", "else"}
_MEMBER = re.compile(r"(\w+)\s*(?:->|\.)\s*(unk_?([0-9A-Fa-f]+))\b")
_BODY = re.compile(r"body L(\d+)\s*(?::\s*(.*))?$")
_STRING = re.compile(r"string\s+(.+)$")
_SDK = re.compile(r"sdk\s+(\S+)$")


def decode(asm: str) -> list[str]:
    """The text strings an extracted data file holds: `.byte` runs split at the NUL, and `.ascii`/`.asciz` lines. Only
    runs of four or more printable characters are strings."""
    raw = bytearray()
    for line in asm.splitlines():
        if m := _BYTES.search(line):
            for tok in m[1].split(","):
                tok = tok.strip()
                if re.fullmatch(r"0[xX][0-9A-Fa-f]{1,2}|\d{1,3}", tok):
                    raw.append(int(tok, 0) & 0xFF)
        elif m := _TEXT.search(line):
            raw += m[1].encode("latin-1", "replace").replace(b"\\n", b"\n").replace(b"\\t", b"\t")
            raw.append(0 if ".ascii " not in line else 1)
    out = []
    for part in bytes(raw).split(b"\0"):
        text = part.decode("latin-1")
        if PRINTABLE.fullmatch(text):
            out.append(text)
    return out


class Source:
    """The function definitions of the units a pack reads, parsed once each."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.units: dict[str, tuple[str, str, dict[str, locals_.Function]]] = {}

    def unit(self, path: str) -> tuple[str, str, dict[str, locals_.Function]]:
        if path not in self.units:
            try:
                text = (self.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self.units[path] = (text, access.mask(text), {f.name: f for f in locals_.functions(text)} if text else {})
        return self.units[path]

    def get(self, path: str, name: str) -> tuple[str, str, locals_.Function] | None:
        """(text, masked text, function) of NAME in the unit at PATH."""
        text, masked, fns = self.unit(path) if path else ("", "", {})
        return (text, masked, fns[name]) if name in fns else None


def asm_index(ctx: Any) -> dict[str, Path]:
    """File stem -> extracted data assembly (`asm/<version>/data/<segment>/<name>.s`), the names_from version first.
    One directory listing per command: `api.asm_path` parsed the symbol tables again for every spelling."""
    if getattr(ctx, "_asm_data", None) is None:
        found: dict[str, Path] = {}
        for version in sorted(ctx.doc["versions"], key=lambda v: v != ctx.doc["names_from"]):
            for file in sorted((Path(ctx.root) / "asm" / version / "data").glob("*/*.s")):
                found.setdefault(file.stem, file)
        ctx._asm_data = found
    return ctx._asm_data  # type: ignore[no-any-return]


def asm_text(config: Config, ctx: Any, name: str) -> str:
    """The extracted assembly of a datum, "" when the project has none for it."""
    member = ctx.members.get(ctx.canon(name))
    index = asm_index(ctx)
    for spelled in dict.fromkeys([name, *(ctx.spellings(name) if member else ())]):
        if spelled in index:
            return index[spelled].read_text(encoding="utf-8", errors="replace")
    return ""


def prototypes(config: Config, root: Path) -> locals_.Prototypes:
    """The project's prototypes (parameter names per function), kept in `.unbake/recipe/protos.json` for as long as the
    tree state and the tools are the same: reading every source takes ten seconds."""
    index = locals_.Prototypes(root)
    key, where = structure._state(config), root / ".unbake" / "recipe" / "protos.json"
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
        if key and held["key"] == key:
            index.found = held["protos"]
            return index
    except (OSError, ValueError, KeyError):
        pass
    index.get(())
    if key:
        host.write(where, json.dumps({"key": key, "protos": index.found}, separators=(",", ":")).encode())
    return index


class Strings:
    """The text strings of data by name, read from the assembly on first use."""

    def __init__(self, config: Config, ctx: Any, read: Callable[[Config, Any, str], str] | None = None) -> None:
        self.config, self.ctx, self.read = config, ctx, read or asm_text
        self.held: dict[str, list[str]] = {}

    def of(self, datum: str) -> list[str]:
        if datum not in self.held:
            try:
                self.held[datum] = decode(self.read(self.config, self.ctx, datum))
            except Exception:
                self.held[datum] = []
        return self.held[datum]

    def loaded(self, name: str) -> list[dict[str, str]]:
        """The strings a function loads: [{datum, text}] over the data its code reads."""
        return [{"datum": d, "text": t} for d in self.ctx.loads.get(name, ()) for t in self.of(d)[:3]]


# ---------------------------------------------------------------- evidence


def body_range(ctx: Any, source: Source, name: str) -> tuple[int, int] | None:
    """(first, last) 1-based file lines of function NAME's definition."""
    member = ctx.members.get(name)
    got = source.get(member.get("unit") or "", name) if member else None
    if got is None:
        return None
    text, _, fn = got
    return text.count("\n", 0, fn.start) + 1, text.count("\n", 0, fn.end) + 1


def verifies(ctx: Any, item: str, names: Sequence[str], strings: Strings, source: Source) -> bool:
    """Whether a `body` or `string` evidence item is true of the target (the functions `names` speak for it)."""
    if m := _BODY.match(item.strip()):
        line, note = int(m[1]), (m[2] or "").strip()
        quote = re.search(r'"([^"]+)"', note)
        for n in names:
            span = body_range(ctx, source, n)
            if span and span[0] <= line <= span[1]:
                text = source.unit(ctx.members[n].get("unit") or "")[0].splitlines()
                if not quote or quote[1] in text[line - 1]:
                    return True
        return False
    if m := _SDK.match(item.strip()):  # the project's libultra table names the target's address so
        return any(ctx.sdk_name(ctx.canon(n)) == m[1] for n in names)
    if m := _STRING.match(item.strip()):
        want = m[1].strip().strip("\"'")
        if not want:
            return False
        held = [t for n in names for t in [*strings.of(n), *(x["text"] for x in strings.loaded(n))]]
        return any(want in t for t in held)
    return False


EXTRA = (_BODY, _STRING, _SDK)


def extra_form(item: str) -> bool:
    return any(rx.match(item.strip()) for rx in EXTRA)


# ---------------------------------------------------------------- cards


def _done(directory: Path | None) -> set[str]:
    """The keys the ledger already holds (a name given, a row accepted): a pack offers what is still open."""
    keys: set[str] = set()
    try:
        lines = ((directory or slices.names_dir()) / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return keys
    for line in lines:
        try:
            keys.add(str(json.loads(line).get("key") or ""))
        except ValueError:
            continue
    return keys


def _names_by_key(ctx: Any, first: str) -> dict[str, str]:
    """Function key -> current name, over every function member."""
    out: dict[str, str] = {}
    for name, m in ctx.members.items():
        where = slices._first(m["placements"], first)
        if where:
            out[slices.fn_key(*where)] = name
    return out


def _declared(masked: str) -> dict[str, str]:
    """variable -> declared record type for `TYPE *var` in a function's text (a heuristic for the member keys)."""
    found = {
        m[2]: m[1]
        for m in re.finditer(
            r"(?:^|[(,;{])\s*((?:struct\s+|union\s+)?[A-Za-z_]\w*)\s*\*+\s*(\w+)\s*[,;)=\[]", masked, re.M
        )
    } | {m[2]: m[1] for m in re.finditer(r"(?:^|[(,;{])\s*([A-Z]\w*)\s+(\w+)\s*[;=\[]", masked, re.M)}
    return {
        v: t
        for v, t in found.items()
        if t not in _NOT_RECORDS and not re.fullmatch(r"[su](?:8|16|32|64)|f(?:32|64)", t)
    }


def _use(text: str, masked: str, fn: locals_.Function, slot: str) -> str:
    """A `body L<n>: ...` evidence item citing the first line after the declaration that uses SLOT ("" when none)."""
    body = masked[fn.start : fn.end]
    declaration = re.compile(rf"^\s*[A-Za-z_][\w\s*]*[\s*]{re.escape(slot)}\s*(?:\[[^\]]*\])?\s*;\s*$")
    lines = text.splitlines()
    for m in re.finditer(rf"\b{re.escape(slot)}\b", body[body.index("{") :]):
        line = masked.count("\n", 0, fn.start + body.index("{") + m.start()) + 1
        shown = lines[line - 1] if line <= len(lines) else ""
        if not declaration.match(shown):
            return f"body L{line}: " + " ".join(shown.replace('"', "").split())[:70]
    return ""


def _members(masked: str, text: str, fn: locals_.Function, first_line: int) -> list[Json]:
    """The unnamed members a body touches: member key (`member:TYPE+0xOFF` when the receiver's type is declared),
    the line, and the statement."""
    body = masked[fn.start : fn.end]
    types = _declared(body)
    out: list[Json] = []
    seen: set[tuple[str, str]] = set()
    for m in _MEMBER.finditer(body):
        if (m[1], m[2]) in seen:
            continue
        seen.add((m[1], m[2]))
        line = masked.count("\n", 0, fn.start + m.start()) + 1
        lines = text.splitlines()
        shown = lines[line - 1].strip() if line <= len(lines) else ""
        kind = types.get(m[1], "")
        kind = re.sub(r"^(?:struct|union)\s+", "", kind)
        out.append(
            {
                "member": m[2],
                "receiver": m[1],
                "type": kind,
                "line": line,
                "text": shown,
                "key": slices.member_key(kind, int(m[3], 16)) if kind else "",
            }
        )
    return out


def _skeleton(key: str, evidence: Iterable[str]) -> Json:
    return {"key": key, "name": "", "doc": "", "evidence": list(evidence)[:3], "confidence": "high"}


def _fn_card(
    ctx: Any, name: str, key: str, first: str, source: Source, strings: Strings, extra: dict[str, Any]
) -> Json:
    member = ctx.members[name]
    path = member.get("unit") or ""
    got = source.get(path, name)
    callees = [c for c in ctx.calls.get(name, ())]
    callers = sorted(
        dict.fromkeys(ctx.callers.get(name, ())), key=lambda c: extra["keys"].get(c, "").split(":")[1:2] != [first]
    )
    loaded = strings.loaded(name)
    folder = ctx.folder_of(name)
    word = ctx.word(folder)
    card: dict[str, Any] = {
        "key": key,
        "kind": "function",
        "target": name,
        "file": path or ctx.modules.get(ctx.module_at(name), {}).get("source", ""),
        "subsystem": folder,
        "prefix": (style.pascal(word) + "_") if word else "",
        "size": sum(p["rom_end"] - p["rom_start"] for p in member["placements"] if p["version"] == first),
        "fan_in": len(callers),
        "callers": [{"name": c, "key": extra["keys"].get(c, "")} for c in callers[:12]],
        "callees": [{"name": c, "key": extra["keys"].get(c, ""), "sdk": ctx.sdk_name(c) or ""} for c in callees[:16]],
        "tags": dict(ctx.tags([name])),
        "strings": loaded[:8],
        "loads": list(ctx.loads.get(name, ()))[:12],
    }
    proposals: dict[str, Any] = {"function": [], "slots": [], "members": []}
    if ctx.catalog is not None:
        rows = debug.rows(ctx.catalog, [member])
        card["catalog"] = {"name": rows[0]["name"] if rows else "", "variables": list(extra["variables"].get(name, ()))}
        proposals["function"] += [{"name": r["name"], "evidence": r["evidence"], "why": "debug catalog"} for r in rows]
    sdk = ctx.sdk_name(name)
    if sdk:
        proposals["function"].append({"name": sdk, "evidence": [], "why": "libultra table"})
    slots: list[Json] = []
    if got is not None:
        text, masked, fn = got
        body = masked[fn.start : fn.end]
        card["line"] = text.count("\n", 0, fn.start) + 1
        shown = text[fn.start : fn.end]
        card["body"] = shown if len(shown) <= BODY_LIMIT else shown[:BODY_LIMIT] + "\n/* ... cut ... */"
        card["body_cut"] = len(shown) > BODY_LIMIT
        if card["body_cut"]:  # the rest is in the source: where to read it
            card["body_lines"] = f"{card.get('file', '')}:{card['line']}-{card['line'] + shown.count(chr(10))}"
        fn_slots = locals_.slots(text, fn)
        for s in fn_slots:
            hints = locals_.suggestions(
                body, s.name, extra["protos"].get(locals_.calls(body)), extra["variables"].get(name, ())
            )
            hints = hints or [h for h in [_use(text, masked, fn, s.name)] if h]
            row = _skeleton(slices.slot_key(key, s.name), hints)
            slots.append(
                {"slot": s.name, "kind": s.kind, "type": s.type, "line": s.line, "evidence": hints, "row": row}
            )
        proposals["members"] = _members(masked, text, fn, card["line"])
    proposals["slots"] = slots
    card["proposals"] = proposals
    top = proposals["function"][0] if proposals["function"] else {}
    hint = (
        [e for e in top.get("evidence", [])]
        or [f"callee {c}" for c in callees[:1]]
        or [f"caller {c}" for c in callers[:1]]
        or (
            [f"body L{card['line']}: " + " ".join(card["body"].splitlines()[0].replace('"', "").split())[:70]]
            if "body" in card
            else []
        )
    )
    card["rows"] = {"function": _skeleton(key, hint), "slots": [s["row"] for s in slots]}
    card["suggestions"] = extra["suggested"].get(key, [])
    card["open"] = {
        "function": style.neutral(name) and key not in extra["done"],
        "slots": [s["slot"] for s in slots if s["row"]["key"] not in extra["done"]],
    }
    return card


def _datum_card(ctx: Any, name: str, key: str, strings: Strings, extra: Json) -> Json:
    readers = list(dict.fromkeys(ctx.callers.get(name, ())))
    scope = "global" if name in ctx.externs() or len({ctx.module_at(c) for c in readers}) >= 2 else "static"
    return {
        "key": key,
        "kind": "datum",
        "target": name,
        "scope": scope,
        "prefix": "g_" if scope == "global" else "s_",
        "readers": [{"name": r, "key": extra["keys"].get(r, "")} for r in readers[:12]],
        "strings": [{"datum": name, "text": t} for t in strings.of(name)[:4]],
        "suggestions": extra["suggested"].get(key, []),
        "rows": {"datum": _skeleton(key, [f"caller {readers[0]}"] if readers else [])},
    }


def build(config: Config, params: Json, ctx: Any, doc: Json) -> Json:
    """The pack for slice `params["target"]` (an id, or a lane that holds slices): see the module text."""
    sdoc = slices.load(params.get("names_dir"))
    if sdoc is None:
        raise state.stop(
            "recipe.name", "no slices.json: the planner computes it", action="recipe name slices --lanes N"
        )
    target = str(params.get("target") or "")
    ids = {s["id"]: s for s in sdoc["slices"]}
    mine = slices.lane_slices(sdoc, target) if target not in ids else [target]
    if not mine:
        raise state.stop("recipe.name", f"{target} is no slice or lane with a slice", action="recipe name slices")
    first = doc["names_from"]
    root = config.project.root
    names = _names_by_key(ctx, first)
    done = _done(params.get("names_dir"))
    source, strings = Source(root), Strings(config, ctx, params.get("asm_reader") or asm_text)
    chosen = [ids[i] for i in mine]
    fn_keys = [k for s in chosen for k in s.get("fns", []) if k in names]
    rank = {k: len(ctx.callers.get(names[k], ())) for k in fn_keys}
    extra: dict[str, Any] = {
        "keys": {n: k for k, n in names.items()},
        "variables": debug.variables(config) if ctx.catalog is not None else {},
        "protos": prototypes(config, root),
        "done": done,
        "suggested": slices.suggestions_for(
            [*fn_keys, *[d for s in chosen for d in s.get("data", [])]], params.get("names_dir"), first
        ),
    }
    cards: list[Json] = []
    wanted = int(params.get("count") or 20)
    for key in sorted(fn_keys, key=lambda k: (-rank[k], k)):
        if len(cards) >= wanted:
            break
        card = _fn_card(ctx, names[key], key, first, source, strings, extra)
        if card["open"]["function"] or card["open"]["slots"]:
            cards.append(card)
    data_names = {
        k: n
        for n, m in ctx.members.items()
        if (w := slices._first(m["placements"], first))
        for k in [slices.datum_key(*w)]
    }
    for key in (k for s in chosen for k in s.get("data", [])):
        if len(cards) >= wanted:
            break
        name = data_names.get(key)
        if name and style.neutral(name) and key not in done:
            cards.append(_datum_card(ctx, name, key, strings, extra))
    out = Path(params["out"]) if params.get("out") else root / ".unbake/recipe/name"
    out = out if out.is_absolute() else root / out
    k = 1 + max([int(m[1]) for p in out.glob("pack-*-*.json") if (m := re.search(r"-(\d+)\.json$", p.name))], default=0)
    label = "+".join(mine)
    pack_path, answer = out / f"pack-{label}-{k}.json", out / f"names-{label}-{k}.jsonl"
    rules = table("knowledge/style.toml")
    pack = {
        "step": "S",
        "k": k,
        "slice": mine,
        "answer": str(answer),
        "targets": cards,
        "evidence_forms": [*rules["evidence_forms"], "body L<n>: note", "string TEXT", "sdk NAME"],
        "evidence_note": rules["evidence_note"],
        "names": rules["names"],
        "banned": rules["banned"],
        "row": {
            "key": "KEY",
            "name": "NEW",
            "doc": "what it is or does",
            "evidence": ["callee X"],
            "confidence": "high|low",
        },
        "rule": "one row per function, per slot and per datum you can name; members become rows with suggest:true",
    }
    structure.write(pack_path, json.dumps(pack))
    left = len(fn_keys) - sum(1 for c in cards if c["kind"] == "function")
    count = Counter(c["kind"] for c in cards)
    return {
        "action": "next",
        "step": "S",
        "slice": mine,
        "pack": str(pack_path),
        "answer": str(answer),
        "targets": len(cards),
        "functions": count["function"],
        "data": count["datum"],
        "slots": sum(len(c["proposals"]["slots"]) for c in cards if c["kind"] == "function"),
        "functions_left": max(0, left),
        "next": f"recipe name submit --ledger {target} {answer}" if cards else "recipe name slices",
    }
