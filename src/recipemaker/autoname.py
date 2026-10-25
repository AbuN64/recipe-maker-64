"""Mechanical auto-namers (CLEAN-PLAN-O1 T6): names no lane has to read code for, emitted as ordinary ledger rows.

`recipe name auto --kinds sdk,catalog,strings --out FILE` judges every candidate with the same rules a lane's row meets
(`naming.judge`) and writes the accepted ones as `{key, name, doc, evidence, confidence}` lines for
`recipe name submit --ledger auto FILE`; the refused ones are counted by rule, never written. Kinds:

  sdk      a function the project's `versions/<v>/libultra_names.tsv` names at its address: the SDK's own name
  catalog  a function or datum the original debug catalog records at its address (`debug NAME` evidence)
  locals   a parameter or local the source passes, and only passes, as one named parameter of one callee
           (`param CALLEE NAME` evidence, one distinct name): it takes that parameter's name. Low confidence.
  strings  a datum that holds exactly one text string: `g_`/`s_` + the words of the string, `Format` for a printf
           format, `Text` otherwise; the string is the evidence. Low confidence: a reviewer samples these."""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from unbake.contracts import Json

from recipemaker import debug, host, locals_, slicepack, slices, style

KINDS = ("sdk", "catalog", "strings", "locals")
DEFAULT = ("sdk", "catalog", "strings")  # `locals` is asked for by name: its names are the callee's, a weaker guess
_FORMAT = re.compile(r"%[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]*[diouxXeEfgGcsp%]")
_WORD = re.compile(r"[A-Za-z]{3,}")
MAX_WORDS = 3
MAX_LENGTH = 28  # the words of a name, so a long sentence is no name


def words(text: str) -> list[str]:
    """The words a string names itself by: the alphabetic runs of three or more letters outside printf conversions."""
    return _WORD.findall(_FORMAT.sub(" ", text))


def plausible(text: str) -> bool:
    """Whether a string reads as language and not as bytes that happen to print: mostly letters, at least one word of
    four letters with a vowel, and every word of three or more letters in one case (`score`, `Score`, `SCORE`)."""
    plain = _FORMAT.sub(" ", text)
    shown = [c for c in plain if not c.isspace()]
    if not shown or sum(c.isalpha() for c in shown) < 0.7 * len(shown):
        return False
    found = [w for w in _WORD.findall(plain)]
    if any(not (w.islower() or w.isupper() or (w[0].isupper() and w[1:].islower())) for w in found):
        return False
    return any(len(w) >= 4 and re.search(r"[aeiouyAEIOUY]", w) for w in found)


def camel(parts: Sequence[str]) -> str:
    """`player`, `Name`, `list` -> playerNameList (first word lower case)."""
    low = [p.lower() for p in parts]
    return low[0] + "".join(p.capitalize() for p in low[1:]) if low else ""


def string_name(text: str, scope: str) -> str:
    """The name a one-string datum takes ("" when its text says nothing a name can hold): `g_playerNameFormat` for
    "Player %s", `s_gameOverText` for "GAME OVER". Scope is `global` or `static`."""
    if not text.isascii() or any(c in text for c in '"\\'):
        return ""
    found = words(text)
    if (
        not plausible(text) or not found or len(found) > MAX_WORDS or sum(map(len, found)) > MAX_LENGTH
    ):  # a sentence is no name
        return ""
    suffix = "Format" if _FORMAT.search(text.replace("%%", "")) else "Text"
    return ("g_" if scope == "global" else "s_") + camel(found) + suffix


def _row(key: str, name: str, doc: str, evidence: Sequence[str], confidence: str = "high") -> Json:
    return {"key": key, "name": name, "doc": doc, "evidence": list(evidence), "confidence": confidence}


def _legacy(kind: str, target: str, name: str, evidence: Sequence[str]) -> Json:
    """The row `naming.judge` reads."""
    return {"kind": kind, "target": target, "name": name, "doc": "auto", "evidence": list(evidence)}


def key_of(ctx: Any, name: str) -> str:
    member = ctx.members.get(ctx.canon(name))
    if member is None:
        return ""
    where = slices._first(member["placements"], ctx.doc["names_from"])
    if where is None:
        return ""
    return (slices.fn_key if member["kind"] == "function" else slices.datum_key)(*where)


GENERIC = {"args", "data", "ptr", "value", "temp", "tmp", "self", "this", "param", "params", "unused", "dummy", "ptrs"}
GOOD = re.compile(r"[a-z][A-Za-z]{3,}")  # no digit: `unused0`, `arg2` say nothing


def local_rows(ctx: Any, config: Any) -> list[tuple[str, Json | None, Json]]:
    """A slot whose every `param CALLEE NAME` hint is the same NAME takes it, unless NAME is generic, a word the
    function already spells, or taken by another slot of it. The ledger judges the row (scope, declaration, duplicates)."""
    root = ctx.root
    index, source = slicepack.prototypes(config, root), slicepack.Source(root)
    out: list[tuple[str, Json | None, Json]] = []
    for name, m in sorted(ctx.members.items()):
        got = source.get(m.get("unit") or "", name) if m["kind"] == "function" else None
        if got is None or not (key := key_of(ctx, name)):
            continue
        text, masked, fn = got
        body = masked[fn.start : fn.end]
        spelled, chosen = set(locals_.WORD.findall(body)), set[str]()
        for slot in locals_.slots(text, fn):
            hints = [
                h
                for h in locals_.suggestions(body, slot.name, index.get(locals_.calls(body)))
                if h.startswith("param ")
            ]
            found = list(dict.fromkeys(h.split()[2] for h in hints))
            if len(found) != 1 or not GOOD.fullmatch(found[0]) or found[0] in GENERIC | spelled | chosen:
                continue
            chosen.add(found[0])
            doc = f"The {slot.kind} {slot.name} of {name} is passed on as {found[0]}."
            out.append(("locals", None, _row(slices.slot_key(key, slot.name), found[0], doc, [hints[0]], "low")))
    return out


def candidates(
    ctx: Any, config: Any, strings: slicepack.Strings, kinds: Sequence[str]
) -> list[tuple[str, Json | None, Json]]:
    """(kind of auto-namer, the legacy row for judge, the ledger row) for every candidate."""
    out: list[tuple[str, Json | None, Json]] = []
    first = ctx.doc["names_from"]
    del first
    for name, m in sorted(ctx.members.items()):
        if not style.neutral(name):
            continue
        key = key_of(ctx, name)
        if not key:
            continue
        if m["kind"] == "function" and "sdk" in kinds and (sdk := ctx.sdk_name(name)):
            ev = [f"sdk {sdk}"]
            out.append(("sdk", _legacy("function", name, sdk, ev), _row(key, sdk, f"The SDK's {sdk}.", ev)))
    if "catalog" in kinds and ctx.catalog is not None:
        for row in debug.rows(ctx.catalog, ctx.members.values()):
            if style.neutral(row["target"]) and (key := key_of(ctx, row["target"])):
                out.append(
                    (
                        "catalog",
                        {**row, "doc": row["doc"]},
                        _row(key, row["name"], row["doc"], row["evidence"]),
                    )
                )
    if "strings" in kinds:
        for name, m in sorted(ctx.members.items()):
            if m["kind"] == "function" or not style.neutral(name) or not (key := key_of(ctx, name)):
                continue
            held = strings.of(name)
            if len(held) != 1:
                continue
            readers = list(dict.fromkeys(ctx.callers.get(name, ())))
            scope = "global" if name in ctx.externs() or len({ctx.module_at(c) for c in readers}) >= 2 else "static"
            new = string_name(held[0], scope)
            fragment = words(held[0])[0] if new else ""
            if not new:
                continue
            ev = [f"string {fragment}"]
            kind = "global" if scope == "global" else "static"
            doc = f"The string {held[0][:40]!r}."
            out.append(("strings", _legacy(kind, name, new, ev), _row(key, new, doc, ev, "low")))
    if "locals" in kinds:
        out += local_rows(ctx, config)
    return out


def run(config: Any, ctx: Any, kinds: Sequence[str], out: Path | None, judge: Any, moves: Any) -> Json:
    """Judge every candidate in order (a name taken by an earlier row is taken for the later ones) and write the
    accepted rows to OUT (one JSON object per line)."""
    strings = slicepack.Strings(config, ctx)
    accepted: list[Json] = []
    refused: dict[str, dict[str, int]] = {}
    examples: dict[str, list[str]] = {}
    for kind, legacy, row in candidates(ctx, config, strings, kinds):
        rule, reason = judge(ctx, legacy) if legacy else ("", "")
        if rule:
            refused.setdefault(kind, {}).setdefault(rule, 0)
            refused[kind][rule] += 1
            examples.setdefault(f"{kind} {rule}", []).append(f"{legacy['target']} -> {legacy['name']}: {reason}"[:160])
            continue
        if legacy:
            moves(ctx, legacy)  # the name is taken for the rows after it
        accepted.append(row)
    if out is not None:
        host.write(out, "".join(json.dumps(r, sort_keys=True) + "\n" for r in accepted).encode())
    by_kind = {
        k: sum(1 for r in accepted if (r["evidence"] or [""])[0].split()[0] == f)
        for k, f in (("sdk", "sdk"), ("catalog", "debug"), ("strings", "string"), ("locals", "param"))
    }
    return {
        "action": "auto",
        "rows": len(accepted),
        "by_kind": by_kind,
        "refused": refused,
        "examples": {k: v[:3] for k, v in examples.items()},
        "path": str(out) if out else "",
        "next": f"recipe name submit --ledger auto {out}" if out and accepted else "recipe name next",
    }
