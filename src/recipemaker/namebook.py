"""The name ledger: `recipe name submit --ledger LANE FILE` validates rows in seconds against the tree as it is
plus the rows already queued, writes nothing to any tree, and appends the accepted rows to one shared ledger. A batch
applier (the train's `names` input) later applies every queued row at once through the same `submit` rules; a row it
cannot apply comes back to its lane in `returned/LANE.jsonl`.

Files under the names state directory (`$NAMES_STATE`, default ~/rig-state/names):
  ledger.jsonl      one accepted row per line {seq, ts, lane, key, name, doc, evidence, confidence, row}, append-only
  state.json        {applied_seq, next_batch}: rows with seq above applied_seq are queued
  suggest.jsonl     rows a lane marked `suggest`: never applied by themselves
  returned/LANE.jsonl   rows the applier refused, with the rule and reason
  slices/LANE.json  what the lane owns (written by `recipe name slices`); no file, no ownership check"""

import fcntl
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from unbake.contracts import Config, Json

from recipemaker import keys, state

DEFAULT_TREE = "~/code/wt/POOL-SRC"  # the shared warm tree at main that a ledger submit reads (it writes nothing)


def store() -> Path:
    return Path(os.environ.get("NAMES_STATE") or "~/rig-state/names").expanduser()


def default_tree() -> Path:
    return Path(os.environ.get("NAMES_TREE") or DEFAULT_TREE).expanduser()


@contextmanager
def locked() -> Iterator[None]:
    store().mkdir(parents=True, exist_ok=True)
    with open(store() / "ledger.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _lines(path: Path) -> list[Json]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def ledger() -> list[Json]:
    return _lines(store() / "ledger.jsonl")


def event(doc: Json) -> None:
    """Record what became of rows: made (in batch K), landed, dropped (back in the queue, or returned to its lane)."""
    with locked(), open(store() / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **doc}, sort_keys=True) + "\n")


def events() -> list[Json]:
    return _lines(store() / "events.jsonl")


def status() -> dict[int, str]:
    """seq -> queued | inflight | landed | returned, by replaying the events over the ledger."""
    out = {r["seq"]: "queued" for r in ledger()}
    for e in events():
        for seq in e.get("seqs", ()):
            if seq in out:
                out[seq] = {"made": "inflight", "landed": "landed", "returned": "returned", "requeued": "queued"}.get(
                    e["event"], out[seq]
                )
    return out


def queued(inflight: bool = True) -> list[Json]:
    """The accepted rows not in main yet, oldest first; with `inflight` False only those no batch holds."""
    held = {"queued", "inflight"} if inflight else {"queued"}
    now = status()
    return [r for r in ledger() if now.get(r["seq"]) in held]


def append(lane: str, rows: Sequence[Json]) -> list[Json]:
    """Add accepted rows to the ledger under the lock; returns the ledger lines written."""
    out: list[Json] = []
    with locked():
        have = ledger()
        seq = max((r["seq"] for r in have), default=0)
        with open(store() / "ledger.jsonl", "a", encoding="utf-8") as handle:
            for row in rows:
                seq += 1
                line = {
                    "seq": seq,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "lane": lane,
                    "key": row.get("_key", ""),
                    "name": row["name"],
                    "doc": row.get("doc", ""),
                    "evidence": row.get("evidence", []),
                    "confidence": row.get("confidence", "high"),
                    "row": row,
                }
                handle.write(json.dumps(line, sort_keys=True) + "\n")
                out.append(line)
    return out


def suggest(lane: str, rows: Sequence[Json], owner: Callable[[Json], str]) -> None:
    store().mkdir(parents=True, exist_ok=True)
    with locked(), open(store() / "suggest.jsonl", "a", encoding="utf-8") as handle:
        for row in rows:
            line = {
                **row,
                "key": row.get("_key") or row.get("key", ""),
                "lane": lane,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "owner": owner(row),
            }
            handle.write(json.dumps(line, sort_keys=True) + "\n")


def take_returns(lane: str) -> list[Json]:
    """The rows the applier sent back to LANE; they are handed over once."""
    path = store() / "returned" / f"{lane}.jsonl"
    with locked():
        rows = _lines(path)
        if rows:
            path.replace(path.with_suffix(".seen"))
    return rows


def send_back(lane: str, row: Json, rule: str, reason: str) -> None:
    path = store() / "returned" / f"{lane}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(), open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"row": row, "rule": rule, "reason": reason}, sort_keys=True) + "\n")


def slice_of(lane: str) -> Json | None:
    path = store() / "slices" / f"{lane}.json"
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def owner_gate(lane: str) -> Callable[[str], str]:
    """A function from a row's key to "" (owned, or no slice file) or the reason it is not the lane's."""
    sl = slice_of(lane)
    if sl is None:
        return lambda key: ""
    fns, data, records = (set(sl.get(k, ())) for k in ("fns", "data", "records"))

    def norm(k: str, first: str) -> str:
        parsed = keys.parse(k, first)
        return parsed.text() if parsed else k

    def check(key: str, first: str = "us-rev1") -> str:
        parsed = keys.parse(key, first)
        if parsed is None:
            return f"{key} is no key, so no slice owns it"
        if parsed.kind in ("fn", "slot"):
            own = keys.Key("fn", parsed.version, parsed.vram).text()
            ok = own in {norm(k, first) for k in fns}
        elif parsed.kind == "datum":
            ok = parsed.text() in {norm(k, first) for k in data}
        else:
            ok = parsed.tag in records
        return "" if ok else f"{key} is outside slice {sl.get('slice', '?')} of {lane}: send it as a suggest row"

    return check


def resolve(ctx: Any, fields: Any, repairs: Any, row: Json) -> tuple[Json, str, str]:
    """(row, rule, reason): a row with a `key` turned into the row shape the rules judge (kind, target and the details
    the kind needs, with `_key` the canonical key); a legacy row passes through with the key its target gives, if any.
    rule "applied" = the tree already holds the name."""
    first = str(ctx.doc["names_from"])
    raw = row.get("key")
    rest = dict(row)
    if raw is None:
        kind = str(row.get("kind"))
        key = keys.of_target(kind, str(row.get("target")), first, str(row.get("struct") or ""))
        return ({**row, "_key": key} if key else row), "", ""
    parsed = keys.parse(str(raw), first)
    if parsed is None:
        return (
            row,
            "shape",
            f"{raw!r} is no row key (fn:VER:VRAM8, datum:VER:VRAM8, member:TAG+OFFSET, slot:VER:VRAM8:OLD)",
        )
    name = str(row.get("name"))
    if parsed.kind == "member":
        old, why = fields.unknown_at(parsed.tag, parsed.offset)
        if not old:
            held = fields.types.member.get(fields.types.key.get(parsed.tag, ""), {}) if fields.types else {}
            if name in held:
                return {**rest, "_key": parsed.text()}, "applied", ""
            return row, "target", why
        return {**rest, "kind": "field", "struct": parsed.tag, "target": old, "_key": parsed.text()}, "", ""
    function = ctx.canon(
        keys.neutral(keys.Key("fn" if parsed.kind == "slot" else parsed.kind, parsed.version, parsed.vram), first)
    )
    member = ctx.members.get(function)
    want = "data" if parsed.kind == "datum" else "function"
    if member is None or (member["kind"] == "function") != (want == "function"):
        return row, "target", f"no {want} is placed at {parsed.version} {parsed.vram:08X}"
    if parsed.kind == "slot":
        unit = str(member.get("unit") or "")
        line = keys.declared_line(ctx.root, unit, function, parsed.slot)
        if not line:
            if keys.declared_line(ctx.root, unit, function, name):
                return {**rest, "_key": parsed.text()}, "applied", ""
            return row, "target", f"{function} has no local or parameter {parsed.slot}"
        return (
            {
                **rest,
                "kind": "local",
                "target": parsed.slot,
                "function": function,
                "path": unit,
                "line": line,
                "_key": parsed.text(),
            },
            "",
            "",
        )
    if function == name:
        return {**rest, "_key": parsed.text()}, "applied", ""
    kind = ("static" if name.startswith("s_") else "global") if parsed.kind == "datum" else "function"
    return {**rest, "kind": kind, "target": function, "_key": parsed.text()}, "", ""


def _where(row: Json) -> str:
    return str(row.get("_key") or row.get("key") or row.get("target"))


def submit(config: Config, params: Json, rows: list[Json]) -> Json:
    """`recipe name submit --ledger LANE FILE`: judge the rows with the naming rules against the tree and the queue,
    store the accepted ones; nothing in any tree changes."""
    from recipemaker import naming

    lane = str(params["ledger"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", lane):
        raise state.stop("recipe.name", f"{lane!r} is no lane name", action="recipe name submit --ledger LANE FILE")
    sent_back = take_returns(lane)
    waiting = queued()
    taken_keys = {r["key"] for r in waiting if r["key"]}
    owner = None
    seen: set[str] = set()

    def gate(row: Json) -> tuple[str, str] | None:
        nonlocal owner
        key = str(row.get("_key") or "")
        if row.get("suggest"):
            return None
        if owner is None:
            owner = owner_gate(lane)
        if key and (why := owner(key)):
            return "owner", why
        if key and key in taken_keys | seen:
            same = next((r for r in waiting if r["key"] == key and r["name"] == row.get("name")), None)
            return ("applied", "already queued") if same else ("pending", f"{key} is already queued under another name")
        seen.add(key)
        return None

    judged = naming.judge_rows(config, params, rows, [r["row"] for r in waiting], gate)
    stored = [r for r, _ in judged.accepted if not r.get("suggest")]
    advice = [r for r, _ in judged.accepted if r.get("suggest")]
    written = append(lane, stored)
    suggest(lane, advice, lambda r: "")
    by_rule = Counter(r["rule"] for r in judged.refused)
    return {
        "action": "submit",
        "ledger": lane,
        "rows": len(rows),
        "accepted": len(stored),
        "suggested": len(advice),
        "refused": len(judged.refused),
        "already": len(judged.already),
        "queued": len(waiting) + len(written),
        "seqs": [w["seq"] for w in written],
        "by_rule": dict(by_rule),
        "returned": [
            f"returned {r['rule']} {_where(r['row'])} -> {r['row'].get('name')}: {r['reason']}" for r in sent_back
        ],
        "refusals": naming.refusal_lines(judged.refused),
        "findings": [
            {"key": "recipe.name", "blocking": True, "message": line} for line in naming.refusal_lines(judged.refused)
        ],
        "exit": 1 if judged.refused else 0,
        "next": "recipe name next",
    }


def renames(rows: Sequence[Json]) -> str:
    """The old -> new map of a batch as TSV lines (old, new, kind, key) for the port to consume."""
    return "".join(
        f"{r['target']}\t{r['name']}\t{r['kind']}\t{r.get('_key', '')}\n"
        for r in rows
        if r.get("kind") in ("function", "global", "static", "field", "local")
    )
