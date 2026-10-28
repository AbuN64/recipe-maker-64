"""The name ledger: `recipe name submit --ledger LANE FILE` validates rows in seconds against the tree as it is
plus the rows already queued, writes nothing to any tree, and appends the accepted rows to one shared ledger. A batch
applier (the train's `names` input) later applies every queued row at once through the same `submit` rules; a row it
cannot apply comes back to its lane in `returned/LANE.jsonl`.

Files under the names state directory (`$NAMES_STATE`, default ~/rig-state/names):
  ledger.jsonl      one accepted row per line {seq, ts, lane, key, name, doc, evidence, confidence, row}, append-only
  state.json        {applied_seq, next_batch}: rows with seq above applied_seq are queued
  submits.jsonl     one line per submit {ts, lane, rows, accepted, refused, suggested, by_rule}: the WATCH counts
  events.jsonl      what became of rows: made (batch K), landed, requeued, returned
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
from copy import deepcopy
from pathlib import Path
from typing import Any

from unbake.contracts import Config, Json

from recipemaker import keys, state

DEFAULT_TREE = "~/code/wt/POOL-SRC"  # the shared warm tree at main that a ledger submit reads (it writes nothing)


@contextmanager
def namespace(directory: str | Path | None) -> Iterator[None]:
    """Bind the explicit CLI directory to the existing store boundary for this command only."""
    previous = os.environ.get("NAMES_STATE")
    if directory:
        os.environ["NAMES_STATE"] = str(Path(directory).expanduser().resolve())
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("NAMES_STATE", None)
        else:
            os.environ["NAMES_STATE"] = previous


def store() -> Path:
    return Path(os.environ.get("NAMES_STATE") or "~/rig-state/names").expanduser()


def default_tree() -> Path:
    """The warm read-only input tree when the caller supplies no project."""
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
    with locked():
        _event(doc)


def _event(doc: Json) -> None:
    """Append under the caller's ledger lock, including an atomic selection decision."""
    if "batch" in doc:
        batch_identity(doc["batch"])
    with open(store() / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **doc}, sort_keys=True) + "\n")


def events() -> list[Json]:
    rows = _lines(store() / "events.jsonl")
    for row in rows:
        if "batch" in row:
            batch_identity(row["batch"])
    return rows


def batch_identity(value: Any) -> tuple[int, Any]:
    """Order distinct integer and named batch identities without coercion or aliases."""
    if type(value) is int and value >= 0:
        return (0, value)
    if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        return (1, value)
    raise ValueError(f"invalid batch identity: {value!r}")


def next_batch(rows: Sequence[Json]) -> int:
    identities = [batch_identity(r["batch"]) for r in rows if "batch" in r]
    candidate = 1 + max((v for kind, v in identities if kind == 0), default=0)
    named = {v for kind, v in identities if kind == 1}
    while str(candidate) in named:
        candidate += 1
    return int(candidate)


def selector(row: Json) -> Json:
    """Immutable ledger ownership and content, suitable for an exact make request."""
    return deepcopy(row)


def selection(rows: Sequence[Json]) -> Json:
    return {"ledger": str(store().resolve()), "rows": [selector(row) for row in rows]}


def _identity(key: str, name: str, row: Json) -> tuple[str, str, str]:
    """Keyed ownership or the exact unkeyed proposal; never equate different scopes by name."""
    return key, name, "" if key else json.dumps(row, sort_keys=True, separators=(",", ":"))


def select(request: Json) -> list[Json]:
    """Validate the entire reviewed selection before any batch side effect."""
    return _select(request, ledger(), events(), {"queued"})


def _select(request: Json, rows: list[Json], history: list[Json], states: set[str]) -> list[Json]:
    """One immutable selection boundary for application and reviewed returns."""
    if not isinstance(request, dict):
        raise ValueError("selection requires an object")
    if set(request) != {"ledger", "rows"} or request["ledger"] != str(store().resolve()):
        raise ValueError("selection belongs to another ledger or has invalid fields")
    wanted = request["rows"]
    if not isinstance(wanted, list) or not wanted:
        raise ValueError("selection requires a nonempty rows array")
    now = _status(rows, history)
    by_seq = {r["seq"]: r for r in rows}
    if len(by_seq) != len(rows):
        raise ValueError("ledger has ambiguous sequence identities")
    counts = Counter(_identity(r["key"], r["name"], r["row"]) for r in rows if now[r["seq"]] in {"queued", "inflight"})
    seen: set[int] = set()
    out = []
    for item in wanted:
        if not isinstance(item, dict) or type(item.get("seq")) is not int:
            raise ValueError("selection row requires an integer seq")
        seq = item["seq"]
        if seq in seen:
            raise ValueError(f"duplicate selection seq {seq}")
        seen.add(seq)
        row = by_seq.get(seq)
        if row is None:
            raise ValueError(f"unknown selection seq {seq}")
        if selector(row) != item:
            raise ValueError(f"stale or foreign selection seq {seq}")
        if now[seq] not in states:
            raise ValueError(f"selection seq {seq} is {now[seq]}")
        if now[seq] == "queued" and counts[_identity(row["key"], row["name"], row["row"])] != 1:
            raise ValueError(f"ambiguous selection seq {seq}")
        out.append(row)
    return out


def return_selection(request: Json, reason: str | None) -> Json:
    """Return an exact reviewed queued selection, preserving its rows and existing event history.

    The batch lock excludes make/rebuild/settle; the ledger lock covers validation and the one append.
    A repeat succeeds only for the same reviewed return still owning every selected disposition.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a reviewed return requires a reason")
    store().mkdir(parents=True, exist_ok=True)
    with open(store() / "batch.lock", "a") as batch:
        try:
            fcntl.flock(batch, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("a naming batch is active; return no rows until it completes") from error
        with locked():
            rows, history = ledger(), events()
            selected = _select(request, rows, history, {"queued", "returned"})
            seqs = sorted(row["seq"] for row in selected)
            entry = {
                "event": "returned",
                "seqs": seqs,
                "rule": "review",
                "reason": reason,
                "selection": selection(sorted(selected, key=lambda row: row["seq"])),
            }
            now = _status(rows, history)
            repeated = any(now[seq] == "returned" for seq in seqs)
            if repeated:
                wanted = set(seqs)
                latest = {seq: event for event in history for seq in event.get("seqs", ()) if seq in wanted}
                if any(any(latest.get(seq, {}).get(key) != value for key, value in entry.items()) for seq in seqs):
                    raise ValueError("selection has a different return disposition or review reason")
            else:
                _event(entry)
    return {"action": "return", "rows": selected, "returned": len(selected), "repeated": repeated, "reason": reason}


def status() -> dict[int, str]:
    """seq -> queued | inflight | landed | returned, by replaying the events over the ledger."""
    return _status(ledger(), events())


def _status(rows: Sequence[Json], history: Sequence[Json]) -> dict[int, str]:
    out = {r["seq"]: "queued" for r in rows}
    for e in history:
        for seq in e.get("seqs", ()):
            if seq in out:
                out[seq] = {"made": "inflight", "landed": "landed", "returned": "returned", "requeued": "queued"}.get(
                    e["event"], out[seq]
                )
    return out


def dispositions(first: int = 0, last: int = 0, lane: str = "") -> list[Json]:
    """What became of each ledger row, seq by seq: its state (see `status`) and the reason the last event gave for it
    (a requeue or return), else the rule and reason its lane was sent back. FIRST..LAST bound the seqs (0: none)."""
    now = status()
    why: dict[int, str] = {}
    for e in events():
        for seq in e.get("seqs", ()):
            if e.get("reason"):
                why[seq] = str(e["reason"])
    sent: dict[tuple[str, str, str], str] = {}
    for path in sorted((store() / "returned").glob("*.*")):
        for back in _lines(path):
            sent[
                (path.stem, str(back["row"].get("_key") or back["row"].get("key") or ""), str(back["row"].get("name")))
            ] = f"{back['rule']}: {back['reason']}"
    out: list[Json] = []
    for r in ledger():
        if (first and r["seq"] < first) or (last and r["seq"] > last) or (lane and r["lane"] != lane):
            continue
        state_ = now[r["seq"]]
        reason = why.get(r["seq"], "") if state_ in ("returned", "queued") else ""
        if state_ == "returned" and not reason:
            reason = sent.get((r["lane"], r["key"] or "", r["name"]), "")
        out.append(
            {
                **selector(r),
                "state": state_,
                "reason": reason,
                "proposal": r["row"],
            }
        )
    return out


def queued(inflight: bool = True) -> list[Json]:
    """The accepted rows not in main yet, oldest first; with `inflight` False only those no batch holds."""
    held = {"queued", "inflight"} if inflight else {"queued"}
    rows = ledger()
    now = _status(rows, events())
    return [r for r in rows if now.get(r["seq"]) in held]


def append(lane: str, rows: Sequence[Json]) -> list[Json]:
    """Add accepted rows to the ledger under the lock; returns the ledger lines written."""
    if not rows:
        return []
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


def refused(lane: str, items: Sequence[Json]) -> None:
    """Remember the keys a submit refused (`refused.jsonl`, one line per refusal): `name next --step S` defers a key
    refused twice, so a pack moves on to targets it has not offered instead of repeating the same cards."""
    keyed = [(str(i["row"].get("_key") or i["row"].get("key") or ""), i) for i in items]
    if not any(key for key, _ in keyed):
        return
    store().mkdir(parents=True, exist_ok=True)
    with locked(), open(store() / "refused.jsonl", "a", encoding="utf-8") as handle:
        for key, item in keyed:
            if key:
                line = {"key": key, "lane": lane, "rule": item["rule"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
                handle.write(json.dumps(line, sort_keys=True) + "\n")


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
            f"{raw!r} is no row key (fn:VER:VRAM8, datum:VER:VRAM8, member:TAG+OFFSET, type:TAG, slot:VER:VRAM8:OLD)",
        )
    name = str(row.get("name"))
    if parsed.kind == "type":
        return {**rest, "kind": "type", "target": parsed.tag, "_key": parsed.text()}, "", ""
    if parsed.kind == "member":
        old, why = fields.unknown_at(parsed.tag, parsed.offset)
        if not old:
            held = fields.types.member.get(fields.types.key.get(parsed.tag, ""), {}) if fields.types else {}
            if name in held:
                return {**rest, "_key": parsed.text()}, "applied", ""
            if row.get("suggest") and held:
                # a correction of a member that already has a name goes to the record's owner as advice: the record
                # exists, the owner judges the name (P3-BLOG3 member:CGameStatus_t+0x18 scoreLimit was refused here)
                return {**rest, "_key": parsed.text()}, "", ""
            return row, "target", why
        return {**rest, "kind": "field", "struct": parsed.tag, "target": old, "_key": parsed.text()}, "", ""
    function = ctx.canon(
        keys.neutral(keys.Key("fn" if parsed.kind == "slot" else parsed.kind, parsed.version, parsed.vram), first)
    )
    member = ctx.members.get(function)
    want = "data" if parsed.kind == "datum" else "function"
    if member is None and parsed.kind == "datum":
        # data no module owns (bss, a symbol with no ROM): the version's own table still names the address, and the
        # legacy `global` row (target = that spelling) already renames such a symbol
        spelled = ctx.table_name(parsed.version, parsed.vram, "data")
        if spelled and spelled not in ctx.members:
            if spelled == name:
                return {**rest, "_key": parsed.text()}, "applied", ""
            if row.get("_fix") and row.get("target") != spelled:
                return row, "stale", "reviewed correction target no longer names this identity"
            kind = "static" if name.startswith("s_") else "global"
            return {**rest, "kind": kind, "target": spelled, "_key": parsed.text()}, "", ""
    if member is None or (member["kind"] == "function") != (want == "function"):
        return row, "target", f"no {want} is placed at {parsed.version} {parsed.vram:08X}"
    if parsed.kind == "slot":
        # a NON_MATCHING body (unit None, fuzzy = its file) still spells its locals: the name is source text only
        unit = str(member.get("unit") or member.get("fuzzy") or "")
        line = keys.declared_line(ctx.root, unit, function, parsed.slot, parsed.version)
        if not line:
            if keys.declared_line(ctx.root, unit, function, name, parsed.version):
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
    if row.get("_fix") and row.get("target") != function:
        return row, "stale", "reviewed correction target no longer names this identity"
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
    unfilled = [r for r in rows if isinstance(r, dict) and r.get("key") and r.get("name") == ""]
    rows = [r for r in rows if not (isinstance(r, dict) and r.get("key") and r.get("name") == "")]  # pack skeleton rows
    sent_back = take_returns(lane)
    snapshot = ledger()
    now = _status(snapshot, events())
    waiting = [r for r in snapshot if now[r["seq"]] in {"queued", "inflight"}]
    waiting_by_identity: dict[tuple[str, str, str], list[Json]] = {}
    for existing in waiting:
        identity = _identity(existing["key"], existing["name"], existing["row"])
        waiting_by_identity.setdefault(identity, []).append(existing)
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
        same = waiting_by_identity.get(_identity(key, row.get("name", ""), row), [])
        if len(same) > 1:
            return "ambiguous", "multiple queued or inflight owners hold this proposal"
        if same:
            return "applied", "already queued"
        if key and key in taken_keys | seen:
            return "pending", f"{key} is already queued under another name"
        seen.add(key)
        return None

    landed = {r["key"]: r["name"] for r in snapshot if r["lane"] == lane and r["key"] and now[r["seq"]] == "landed"}
    judged = naming.judge_rows(config, params, rows, [r["row"] for r in waiting], gate, landed)
    stored = [r for r, _ in judged.accepted if not r.get("suggest")]
    advice = [r for r, _ in judged.accepted if r.get("suggest")]
    written = append(lane, stored)
    registered = list(written)
    registrations: list[Json] = [{"state": "registered", **selector(r), "proposal": r["row"]} for r in written]
    for row in judged.already:
        key = row.get("_key") or row.get("key") or ""
        matches = waiting_by_identity.get(_identity(key, row.get("name", ""), row), [])
        if len(matches) == 1:
            existing = matches[0]
            registrations.append({"state": now[existing["seq"]], **selector(existing), "proposal": row})
            if now[existing["seq"]] == "queued":
                registered.append(existing)
        else:
            registrations.append({"state": "ambiguous" if matches else "already", "proposal": row})
    registrations.extend({"state": "refused", **item} for item in judged.refused)
    registrations.extend({"state": "suggested", "proposal": row} for row in advice)
    suggest(lane, advice, lambda r: "")
    refused(lane, judged.refused)
    by_rule = Counter(r["rule"] for r in judged.refused)
    if rows:  # an empty submit (the warm-up call after main moves) is no lane activity: WATCH counts only real ones
        with (
            locked(),
            open(store() / "submits.jsonl", "a", encoding="utf-8") as handle,
        ):  # WATCH counts accepted/refused per lane
            log = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "lane": lane, "rows": len(rows), "accepted": len(stored)}
            log |= {"refused": len(judged.refused), "suggested": len(advice), "by_rule": dict(by_rule)}
            handle.write(json.dumps(log, sort_keys=True) + "\n")
    return {
        "action": "submit",
        "ledger": lane,
        "rows": len(rows),
        "accepted": len(stored),
        "suggested": len(advice),
        "refused": len(judged.refused),
        "already": len(judged.already),
        "unfilled": len(unfilled),
        "queued": len(waiting) + len(written),
        "seqs": [w["seq"] for w in written],
        "selection": selection(registered),
        "registrations": registrations,
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
        if r.get("kind") in ("function", "global", "static", "field", "type", "local")
    )
