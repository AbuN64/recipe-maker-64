"""`recipe flags`: one flag set per original object. The lattice is the toolchain's supported options (each alone, and
pairs inside one option family); `unbake score --variants` scores every candidate under every set in one process.

- Fuzzy functions are scored under default and each single option, then under the pairs that hold an option that
  helped. A set that makes one exact nominates its module.
- A nominated set, and the set of every unit that carries options today, is tried on the landed neighbours across
  the edges of its functions. A neighbour that loses bytes under it is another object: at a proven join that is a
  conflict (one object cannot have two sets, so this C is not the original), at an open edge the flags decide a cut.
- A set every landed member of the module keeps exact under becomes an `options` move; otherwise the function is
  split into a module of its own with the set, where its edges allow it.

Nothing is applied: the proposal and its moves go to `.unbake/recipe/flags/`."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from itertools import combinations
from pathlib import Path

from unbake.contracts import Config, Json, Refusal

from recipemaker import compiler, host, structure

Flags = tuple[str, ...]


def family(option: str) -> str:
    """The option family a lattice pair stays inside: -O levels exclude each other, -f and -m pair within."""
    return option[:2]


def lattice(supported: Iterable[str], extra: Iterable[str] = ()) -> list[Flags]:
    """Default, every option alone, and every pair of options of one -f or -m family."""
    options = sorted(set(supported) | set(extra))
    pairs = [(a, b) for a, b in combinations(options, 2) if family(a) == family(b) != "-O"]
    return [(), *((o,) for o in options), *pairs]


def _supported(config: Config, toolchain: str) -> list[str]:
    try:
        return list(compiler(toolchain).get("supported_options", ()))
    except Refusal:  # a toolchain without a compiler card has no lattice
        return []


def _project_lattice(root: Path) -> list[str]:
    """Extra lattice options the project config names under [flags] lattice (project facts, never the tool's)."""
    import tomllib

    try:
        doc = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(doc.get("flags", {}).get("lattice", ()))


def _order(doc: Json) -> dict[str, list[str]]:
    """Per version its functions in ROM order."""
    rows: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for m in doc["members"]:
        if m["kind"] == "function":
            for p in m["placements"]:
                if p["section"] == ".text":
                    rows[p["version"]].append((p["rom_start"], m["name"]))
    return {v: [n for _, n in sorted(r)] for v, r in rows.items()}


def neighbours(doc: Json, names: Sequence[str], reach: int = 2) -> list[tuple[str, str, str]]:
    """(neighbour, the function beside it, side) for up to `reach` landed C functions on each side of a run of
    functions in the names_from version (else the first holder), stopping at a proven cut."""
    order, verdicts = _order(doc), structure.verdicts(doc) or {}
    by = {m["name"]: m for m in doc["members"]}
    c = {u["path"] for u in doc["units"] if u.get("kind", "c") == "c"}
    ref = (
        doc["names_from"]
        if names[0] in order.get(doc["names_from"], ())
        else next(v for v, row in order.items() if names[0] in row)
    )
    row = order[ref]
    at = sorted(row.index(n) for n in names if n in row)
    out: list[tuple[str, str, str]] = []
    for side, step, start in (("left", -1, at[0]), ("right", 1, at[-1])):
        i, found = start, 0
        while found < reach and 0 <= i + step < len(row):
            left, right = (row[i + step], row[i]) if step < 0 else (row[i], row[i + step])
            if verdicts.get((left, right)) == "cut":
                break
            i += step
            other = row[i]
            if by[other]["unit"] in c and not by[other]["fuzzy"]:
                out.append((other, row[i - step], side))
                found += 1
    return out


def _score(config: Config, unbake_host: Path | None, rows: list[Json], clock: list[Json], tag: str) -> list[Json]:
    """Every row (file, function, flags) in one `unbake score --variants` call; the cores it used go to `clock`."""
    if not rows:
        return []
    path = config.project.root / ".unbake/recipe/flags" / f"variants-{tag}.json"
    host.write(path, json.dumps(rows).encode())
    began = time.monotonic()
    document, stderr = structure._unbake(config, unbake_host, "score", "--variants", str(path))
    cores = [float(x) for x in re.findall(r"^score\s+\S+s\s+([\d.]+) cores", stderr, re.M)]
    clock.append(
        {
            "verb": f"score --variants ({tag})",
            "rows": len(rows),
            "wall_s": round(time.monotonic() - began, 3),
            "cores": cores[-1] if cores else None,
        }
    )
    if not document.get("ok"):
        raise structure._refusal(document)
    return list(document["result"]["variants"])


def _value(result: Json) -> float:
    """1.0 exact, else the score; a refused score is -1 (worse than any build)."""
    if not result.get("ok"):
        return -1.0
    r = result["result"]
    return 1.0 if r.get("exact") else float(r.get("score") or 0.0)


def _row(path: str, function: str | None, flags: Flags) -> Json:
    return {"file": path, "flags": list(flags), **({"function": function} if function else {})}


def search(
    config: Config, host: Path | None, candidates: Sequence[tuple[str, str]], sets: Sequence[Flags], clock: list[Json]
) -> dict[str, dict[Flags, float]]:
    """Per fuzzy function (name, fuzzy path) its value under default and each single option, then under the pairs
    that hold an option that helped it. Two `score --variants` calls in all."""
    singles = [s for s in sets if len(s) <= 1]
    pairs = [s for s in sets if len(s) == 2]
    rows = [(n, s) for n, _ in candidates for s in singles]
    paths = dict(candidates)
    got = _score(config, host, [_row(paths[n], None, s) for n, s in rows], clock, "singles")
    values: dict[str, dict[Flags, float]] = defaultdict(dict)
    for (n, s), r in zip(rows, got, strict=True):
        values[n][s] = _value(r)
    more = []
    for n, by in values.items():
        helped = {s[0] for s in singles if s and by.get(s, -1) > by.get((), -1)}
        more += [(n, p) for p in pairs if set(p) & helped]
    got = _score(config, host, [_row(paths[n], None, s) for n, s in more], clock, "pairs")
    for (n, s), r in zip(more, got, strict=True):
        values[n][s] = _value(r)
    return values


def best(values: dict[Flags, float]) -> Flags | None:
    """The smallest set that makes the function exact; default first; None when no set does."""
    exact = [s for s, v in values.items() if v >= 1.0]
    return min(exact, key=lambda s: (len(s), s)) if exact else None


def judge(doc: Json, found: dict[str, Flags], neighbour_values: dict[tuple[str, Flags], float]) -> Json:
    """From each function's set (`found`: fuzzy finds and today's option units) and the values of its neighbours under
    that set: conflicts, cuts the flags decide, `options` moves and splits."""
    verdicts = structure.verdicts(doc) or {}
    module = {m["name"]: m["module"] for m in doc["members"]}
    members = {m["name"]: m["members"] for m in doc["modules"]}
    out: Json = {"options": [], "splits": [], "conflicts": [], "decided_cuts": []}
    for name, flags in sorted(found.items()):
        if not flags:
            continue
        drops, joins = [], []
        for other, beside, side in neighbours(doc, [name]):
            if neighbour_values.get((other, flags), 1.0) >= 1.0:
                continue
            edge = (other, beside) if side == "left" else (beside, other)
            kind = verdicts.get(edge, "open")
            drops.append({"neighbour": other, "edge": list(edge), "verdict": kind})
            if kind == "join":
                joins.append(other)
            elif kind == "open":
                out["decided_cuts"].append({"left": edge[0], "right": edge[1], "by": list(flags), "start": "flags"})
        landed = [n for n in members.get(module[name], []) if n != name]
        if joins:
            out["conflicts"].append({"function": name, "flags": list(flags), "drops": drops})
        elif not drops and all(neighbour_values.get((n, flags), 1.0) >= 1.0 for n in landed):
            out["options"].append({"module": module[name], "flags": list(flags), "for": name})
        else:
            out["splits"].append({"function": name, "module": f"obj_{name}", "flags": list(flags), "drops": drops})
    return out


def moves(judged: Json) -> list[Json]:
    """`unbake move -` lines: options for whole modules, member + options for the splits flags decide."""
    out: list[Json] = []
    for row in judged["options"]:
        target = json.dumps({"add": row["flags"], "omit": []})
        out.append({"op": "options", "subject": row["module"], "target": target})
    for row in judged["splits"]:
        out.append({"op": "member", "subject": row["function"], "target": row["module"]})
        out.append({"op": "options", "subject": row["module"], "target": json.dumps({"add": row["flags"], "omit": []})})
    return out


def run(config: Config, params: Json) -> Json:
    """`recipe flags`: score the lattice, judge and write proposal.json and moves.jsonl under .unbake/recipe/flags."""
    began = time.monotonic()
    clock: list[Json] = []
    doc = structure.evidence(config, params["host"])
    wanted = set((params.get("segment") or "").split(",")) - {""}
    by = {m["name"]: m for m in doc["members"]}
    fuzzy = [
        (m["name"], m["fuzzy"])
        for m in doc["members"]
        if m["kind"] == "function" and m["fuzzy"] and (not wanted or m["segment"] in wanted)
    ]
    tools = {u["toolchain"] for u in doc["units"]}
    extra = _project_lattice(config.project.root)
    sets = sorted({s for t in tools for s in lattice(_supported(config, t), extra)}, key=lambda s: (len(s), s))
    values = search(config, params["host"], fuzzy, sets, clock)
    found = {n: s for n, v in values.items() if (s := best(v))}
    for u in doc["units"]:  # today's option units: judged as finds of their own set
        flags = tuple((u.get("options") or {}).get("add", ()))
        for n in u["members"]:
            if flags and n in by and by[n]["kind"] == "function" and (not wanted or by[n]["segment"] in wanted):
                found[n] = flags
    pending = {
        (other, flags): by[other]["unit"] for n, flags in found.items() if flags for other, _, _ in neighbours(doc, [n])
    }
    for n, flags in found.items():  # every landed member of its module too: an options move needs all of them
        module = by[n]["module"]
        for m in doc["modules"]:
            if m["name"] == module and flags:
                for o in m["members"]:
                    if o != n and o in by and by[o]["kind"] == "function" and by[o]["unit"] and not by[o]["fuzzy"]:
                        pending[o, flags] = by[o]["unit"]
    keys = sorted(pending)
    got = _score(config, params["host"], [_row(pending[k], k[0], k[1]) for k in keys], clock, "neighbours")
    neighbour_values = {k: _value(r) for k, r in zip(keys, got, strict=True)}
    judged = judge(doc, found, neighbour_values)
    stream = moves(judged)
    root = config.project.root / ".unbake/recipe/flags"
    proposal = {
        "lattice": len(sets),
        "fuzzy": len(fuzzy),
        "found": {n: list(s) for n, s in sorted(found.items()) if n in values},
        **judged,
        "submit": [{"file": by[n]["fuzzy"], "flags": list(s)} for n, s in sorted(found.items()) if s and n in values],
        "unbake_calls": clock,
        "wall_s": round(time.monotonic() - began, 3),
    }
    host.write(root / "proposal.json", json.dumps(proposal, indent=1).encode())
    host.write(root / "moves.jsonl", "".join(json.dumps(m, sort_keys=True) + "\n" for m in stream).encode())
    after = "unbake move --partial - < .unbake/recipe/flags/moves.jsonl" if stream else "recipe status"
    return {
        **{k: v for k, v in proposal.items() if k != "found"},
        "moves": len(stream),
        "path": str(root),
        "next": after,
    }
