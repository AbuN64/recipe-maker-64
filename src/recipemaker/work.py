"""The verbs behind `recipe`: next, work, try, skip, look, status. Each returns the JSON object the CLI prints."""

import pickle
import re
import time
from collections import Counter
from itertools import takewhile
from pathlib import Path
from typing import Any, cast

from unbake import api
from unbake.contracts import Config, Finding, Json, Proof, Refusal, Snapshot, digest

from recipemaker import (
    brief,
    crack,
    declare,
    draft,
    errors,
    host,
    index,
    judge,
    ladder,
    ledger,
    look,
    regions,
    state,
    twin,
    typeitem,
    world,
)

_DEBT = 3  # declaration conflicts status names
_ALIKE = 8  # open functions of the same shape named after a landing
_PROBES = 30  # open items next compiles a draft of before it leases one it knows cannot compile
_TRACKED = ("src", "include", "config.toml", "versions")


class AfterLanding(Exception):
    """A step after a landing failed. The landing stands, and the command says so before it says anything else."""

    def __init__(self, landed: str, saved: str, cause: Exception) -> None:
        super().__init__(repr(cause))
        self.landed, self.saved, self.cause = landed, saved, cause


def _now() -> float:
    return time.time()


def _asm(snapshot: Snapshot, item: str) -> str:
    """The reference version's assembly of a function; a data item has none."""
    if world.of(snapshot).members[item].kind != "function":
        return ""
    return index.asm(snapshot, item)


def _deps(snapshot: Snapshot, item: str) -> str:
    asm = _asm(snapshot, item)
    names = [*index.CALL.findall(asm), *index.REFERENCE.findall(asm)]
    declared = index.get(snapshot).declarations
    return digest((twin.find(snapshot, item) is not None, sorted({n for n in names if n in declared})))


def _sdk(snapshot: Snapshot, member: str) -> bool:
    return bool(group := world.of(snapshot).members[member].group) and world.of(snapshot).groups[group].sdk


def items(config: Config) -> list[Json]:
    """The ordered work list, built once per tree."""
    snapshot = world.capture(config)
    return api.memo(("work.items", snapshot.digest), lambda: world.open_items(snapshot))


def _is_open(snapshot: Snapshot, item: str) -> bool:
    return item in {r["member"] for r in items(snapshot.config)}


def _alike(snapshot: Snapshot, item: str, landed: str) -> list[str]:
    """The open functions of the same shape, named only after a landing."""
    return twin.alike(snapshot, item)[:_ALIKE] if landed else []


def _best(config: Config, item: str) -> float:
    return max((a.score for a in state.attempts(config, item)), default=0.0)


def _probeable(config: Config, snapshot: Snapshot, row: Json, ready: frozenset[str]) -> bool:
    """A function with no landed twin, no candidate file and no earlier attempt starts from an m2c draft."""
    return (
        row["kind"] == "function"
        and row["member"] not in ready
        and not ladder.candidate_path(config, row["member"]).exists()
        and not state.attempts(config, row["member"])
    )


def _undraftable(job: tuple[Snapshot, str]) -> str:
    """Why the m2c draft of a member cannot compile ("" when it does), kept in the content cache; a pool job."""
    snapshot, member = job

    def probe() -> str:
        out = snapshot.config.project.root / ".unbake/recipe/probe" / f"{host.stem(member)}.c"
        try:
            return draft.create(snapshot, member, out)
        except Refusal as refusal:
            return refusal.findings[0].reason

    return index.persisted(snapshot, "recipe.draftable", probe, extra=member, sources=True)


class _Absent(Exception):
    """The probe is not in the cache."""


def _verdict(config: Config, snapshot: Snapshot, member: str) -> str | None:
    """The cached answer of a probe ("" compiles, else why not), None when the project never probed the member."""
    key = index.key(snapshot, "recipe.draftable", extra=member, sources=True)

    def absent() -> bytes:
        raise _Absent

    try:
        return cast("str | None", pickle.loads(api.cached(config, "recipe.draftable", key, absent)))
    except _Absent:
        return None


def _compilable(
    config: Config, snapshot: Snapshot, rows: list[dict[str, Any]], ready: frozenset[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The rows with the leading ones whose draft cannot compile moved behind the first that can, and their names.
    A probe the cache holds costs a read. The first batch of new probes is one draft (the usual answer), each batch
    after it four times wider up to the pool share, and a batch only follows a batch in which none compiled."""
    failed: list[dict[str, Any]] = []
    cap = config.host.slots or config.host.workers
    fresh, todo = 1, rows
    while todo:
        lead = list(takewhile(lambda r: _probeable(config, snapshot, r, ready), todo[: _PROBES - len(failed)]))
        known = {r["member"]: _verdict(config, snapshot, r["member"]) for r in lead}
        batch, new = [], 0
        for row in lead:
            new += known[row["member"]] is None
            if new > fresh:
                break
            batch.append(row)
        if not batch:
            break
        asked = [r["member"] for r in batch if known[r["member"]] is None]
        answers = iter(
            api.pool_map(config, "recipe.probe", _undraftable, [(snapshot, m) for m in asked]) if asked else []
        )
        why = [known[r["member"]] if known[r["member"]] is not None else next(answers) for r in batch]
        good = next((n for n, reason in enumerate(why) if not reason), len(batch))
        failed += batch[:good]
        todo = todo[good:]
        if good < len(batch):
            break
        fresh = min(cap, fresh * 4)
    return [*todo, *failed], [r["member"] for r in failed]


def next_item(config: Config, params: Json) -> Json:
    now, rules = _now(), state.settings()
    snapshot, names_from = world.capture(config), config.project.names_from
    if params.get("kind") == "type":
        return typeitem.next_item(config, snapshot, now)
    if params.get("kind") == "declaration":
        return declare.next_item(config, snapshot, now, params.get("count") or 0, bool(params.get("dry")))
    wanted_kind, wanted_twin = params.get("kind"), params.get("twin")
    taken = {lease.item for lease in state.leases(config, now)}
    ready = twin.ready(snapshot)
    cap = params.get("max_size") or float("inf")
    sign = 1 if params.get("order") == "small" else -1
    reported = items(config)
    if lost := [r for r in reported if r["member"] not in world.of(snapshot).members]:
        raise Refusal(
            Finding("recipe.unknown-member", f"the layout lacks a reported member: {lost[0]}", action="recipe status")
        )
    rows: list[dict[str, Any]] = [
        dict(r)
        for r in reported
        if wanted_kind in (None, r["kind"]) and r["size"] <= cap and r["member"] not in taken
        if wanted_twin in (None, "yes" if r["member"] in ready else "no")
    ]
    deps = {item: _deps(snapshot, item) for item in state.skipped(config, now) if item in world.of(snapshot).members}
    again = state.unblocked(config, {name: found[2] for name, found in index.get(snapshot).declarations.items()})
    hidden = state.skipped(config, now, deps) - again
    rows = [row for row in rows if row["member"] not in hidden]
    rows.sort(
        key=lambda r: (
            r["member"] not in again,
            names_from not in world.of(snapshot).members[r["member"]].standing,  # only live in the target version
            _sdk(snapshot, r["member"]),
            r["member"] not in ready,
            r["size"] < 16,
            sign * r["size"],
            r["address"],
            r["member"],
        )
    )
    uncompilable: list[str] = []
    if params.get("probe", True):
        rows, uncompilable = _compilable(config, snapshot, rows, ready)
    if not rows:
        return {"item": "", "why": "no open item matches", "next": "recipe status"}
    for row in rows:  # another session may lease the top row while this call ranked: the first free row wins
        try:
            lease = state.take(config, row["member"], now)
            break
        except Refusal as refusal:
            if refusal.findings[0].key != "recipe.leased":
                raise
    else:
        return {"item": "", "why": "every open item is leased to another session", "next": "recipe status"}
    found = twin.find(snapshot, row["member"]) if row["member"] in ready else None
    why = (
        f"a landed twin exists: {found.counterpart} in {found.unit} ({found.other_version}); read it with recipe look"
        if found
        else "a header it was waiting on changed; its best candidate is kept"
        if row["member"] in again
        else "smallest open item"
        if sign == 1
        else "largest open item"
    )
    return {
        "item": row["member"],
        "kind": row["kind"],
        "size": row["size"],
        "regions": len(regions.split(_asm(snapshot, row["member"]))) if row["kind"] == "function" else 0,
        "why": why,
        "twin": f"{found.counterpart} in {found.unit} ({found.other_version})" if found else "none",
        "aims": sorted({p.version for p in world.of(snapshot).members[row["member"]].placements}),
        "draft_failed": uncompilable,
        "lease_until": state.stamp(lease.expires),
        "idle_minutes": rules.idle_minutes,
        "stall_tries": rules.stall_tries,
        "next": f"recipe work {row['member']}",
    }


def _blockers(snapshot: Snapshot, item: str) -> dict[str, str]:
    """The globals a candidate reaches through a neighbour or a placeholder, with their declarations as they stand."""
    path = ladder.candidate_path(snapshot.config, item)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    declared = index.get(snapshot).declarations
    symbols = snapshot.versions[index.reference(snapshot, item)].symbols
    found = {
        what.rsplit(" ", 1)[-1] for key, what, *_ in errors.held(snapshot, text, item) if key == "recipe.neighbour"
    }
    found |= {use.base.removeprefix("&") for use in ledger.uses(text)}
    found |= regions.misdeclared(_asm(snapshot, item), declared).keys()
    return {name: declared[name][2] if name in declared else "" for name in sorted(found & symbols.keys())}


def _skip_for(config: Config, item: str, reason: str, why: str, now: float) -> Json:
    snapshot = world.capture(config)
    deps = _deps(snapshot, item) if item in world.of(snapshot).members else ""
    blockers = _blockers(snapshot, item) if reason in typeitem.SKIPS else {}
    wait = typeitem.route(config, snapshot, item) if reason in typeitem.SKIPS else None
    if wait and item in index.get(snapshot).declarations:  # the type item changes this prototype: the item ranks again
        blockers[item] = index.get(snapshot).declarations[item][2]
    if reason == "other" and "toolchain" in why:  # the evidence rides in the note so the pins can be batched
        packet = crack.packet(snapshot, item) if item in world.of(snapshot).members else {"why": []}
        why = "; ".join([why, *(w for w in packet["why"] if "MIPS-I load delays" in w)])
    state.skip(config, item, reason, why, _best(config, item), now, deps, blockers)
    return {
        "item": item,
        "result": "skipped",
        "reason": reason,
        "why": why,
        "next": wait["next"] if wait else "recipe next",
    }


def work(config: Config, params: Json) -> Json:
    item, now = params["item"], _now()
    snapshot = world.capture(config)
    if params.get("kind") == "type" or typeitem.parse_item(item):
        return _typed(config, snapshot, item, params, now)
    if params.get("kind") == "declaration" or not _is_open(snapshot, item):
        if row := next((r for r in declare.conflicts(config, snapshot) if r["name"] == item), None):
            return declare.take(config, snapshot, row, now)
        if params.get("kind") == "declaration":
            raise Refusal(
                Finding(
                    "recipe.no-declaration",
                    f"{item} is no open declaration item",
                    action="recipe next --kind declaration",
                )
            )
        _landed(config, snapshot, item)
    before = state.lease(config, item)
    lease = state.take(config, item, now)
    new = before is None or before.session != lease.session or before.expires <= now
    try:
        return _work(config, params, lease, now)
    except BaseException:
        if new:  # a command that fails must not leave a lease it took
            state.release(config, item)
        raise


def _typed(config: Config, snapshot: Snapshot, item: str, params: Json, now: float) -> Json:
    """Plan and lease the type item of a function's `void *` parameter (the first one its code reaches by offsets)."""
    function, position = typeitem.parse_item(item) or (item, params.get("arg"))
    if position is None:
        found = typeitem.targets(snapshot, function)
        if not found:
            raise Refusal(Finding("recipe.no-type", f"{function} has no void * parameter its code reaches by offsets"))
        position = found[0]
    return typeitem.take(config, snapshot, function, int(position), now)


def _landed(config: Config, snapshot: Snapshot, item: str) -> None:
    """Refuse an item that is no longer open, naming the commit that added it to its source."""
    unit = world.unit_of(snapshot, item)
    if unit is None or item not in world.of(snapshot).members:
        return
    log = host.git(config, "log", "-1", "--format=%h", f"-S{item}", "--", unit.path).stdout.decode().strip()
    raise Refusal(Finding("recipe.landed", f"{item} is already landed (commit {log})", unit=item, action="recipe next"))


def _commits(drain: Json | None, item: str) -> tuple[str, str]:
    """The commit that landed `item` exact and the commit that saved it as nonmatching, each "" when there is none."""
    rows = ladder.commits(drain, item)
    return rows.get("publish", ""), rows.get("fuzzy", "")


def _work(config: Config, params: Json, lease: state.Lease, now: float) -> Json:
    item = lease.item
    try:
        outcome = ladder.run(config, item, submit=not params.get("no_submit"))
    except Refusal as refusal:
        found = refusal.findings[0]
        if found.key in ("member.split-delay-slot", "member.leading-pad"):
            return {**_skip_for(config, item, "split-symbol", found.reason, now), "next": found.action or "recipe next"}
        raise
    landed, saved = _commits(outcome.drain or {}, item)
    try:
        return _summary(config, params, lease, now, outcome, landed, saved)
    except Exception as exc:
        raise _late(config, item, landed, saved, exc) from exc


def _late(config: Config, item: str, landed: str, saved: str, cause: Exception) -> Exception:
    """A failure after a landing must not hide it: the landing stands and the lease is done."""
    if not (landed or saved):
        return cause
    if landed:
        state.release(config, item)
    return AfterLanding(landed, saved, cause)


def _summary(
    config: Config, params: Json, lease: state.Lease, now: float, outcome: ladder.Outcome, landed: str, saved: str
) -> Json:
    item, rules = lease.item, state.settings()
    snapshot = world.capture(config)
    follow = f"read the brief, edit the file toward a byte-exact match, then run: recipe try {item}"
    drain = outcome.drain or {}
    refused = [f for body in drain.get("refused", []) if body["member"] == item for f in body["findings"]]
    reason = f"{refused[0]['reason']} ({refused[0]['path']})" if refused else ""
    text = Path(outcome.candidate).read_text(encoding="utf-8") if Path(outcome.candidate).exists() else ""
    hits = errors.held(snapshot, text, item) if text else []
    if outcome.exact and not outcome.submitted and not reason and hits:
        lines = ", ".join(str(n) for k, _, _, n, _ in hits if k == hits[0][0])
        reason = f"{hits[0][1]} on line {lines}: {hits[0][2]}"
    brief_path = ""
    if (not outcome.exact or reason) and text:
        packet = crack.packet(snapshot, item)
        brief_path = str(brief.write(snapshot, item, packet, outcome.auto_fixes, outcome.left))
    seed = ""
    if any(f.startswith("resumed from") for f in outcome.auto_fixes) and hits:
        what = ", ".join(sorted({w.split(" on ")[0] for _, w, *_ in hits}))
        lines = ", ".join(str(n) for n in sorted({n for _, _, _, n, _ in hits}))
        seed = f"resumed seed holds {len({n for _, _, _, n, _ in hits})} refusable lines ({what}): lines {lines}"
    unresolved = list(dict.fromkeys(f"{u.base} at 0x{u.offset:X}" for u in outcome.left))
    classes = dict(Counter(u.why for u in outcome.left if u.why))  # the census: what keeps each placeholder
    form = outcome.exact and any(k in ("recipe.cast", "recipe.unused") for k, *_ in hits)
    after = (
        f"match budget spent: {_fallback(config, item)}"
        if lease.tries >= rules.stall_tries
        else f"the exact form needs refused text: recipe skip {item} --reason other --note refused-form"
        if form
        else f"recipe try {item}"
        if outcome.exact
        or (world.of(snapshot).members[item].kind == "function" and max(outcome.score.values(), default=0) >= 1)
        else f"{follow}; if no declared record has these members, stop with: recipe skip {item} --reason "
        f'type-unresolved --note "{", ".join(unresolved[:8])}"'
        if unresolved
        else follow
    )
    return {
        "item": item,
        "unresolved": unresolved,
        "unresolved_classes": classes,
        "rungs": [
            {"name": r.name, "result": r.result, "score": round(r.score, 4), "note": r.note} for r in outcome.rungs
        ],
        "result": "open"
        if world.of(snapshot).members[item].kind == "data"
        else "refused"
        if reason
        else "exact"
        if outcome.exact
        else "fuzzy"
        if any(outcome.score.values())
        else "open",
        "score": {v: round(s, 4) for v, s in outcome.score.items()},
        "landed": landed,
        "saved": saved,
        "alike": _alike(snapshot, item, landed),
        "refused": reason,
        "seed": seed,
        "submitted": outcome.submitted,
        "candidate": outcome.candidate,
        "brief": brief_path,
        "twin": f"{found.counterpart} in {found.unit} ({found.version})"
        if (found := twin.find(snapshot, item))
        else "",
        "auto_fixes": list(outcome.auto_fixes),
        "budget": {
            "stall_tries": rules.stall_tries,
            "stalled": lease.tries,
            "minutes_left": state.minutes_left(lease, now),
        },
        "next": after,
    }


def _fallback(config: Config, item: str) -> str:
    path = ladder.candidate_path(config, item)
    return (
        f"write the best body that behaves the same in {path} inside `#ifdef NON_MATCHING` ... `#else` INCLUDE_ASM "
        f"`#endif`, prove it: behave --function {item} --candidate {path}; it then owes a match (recipe work {item})"
    )


def _refuse(item: str, key: str, reason: str, action: str, why: str) -> Refusal:
    return Refusal(Finding(key, reason, unit=item, action=action, missing=(why,)))


def _prepare(config: Config, item: str, last: Json | None) -> list[str]:
    path = ladder.candidate_path(config, item)
    if not path.is_file():
        reason = f"{item} has no candidate file"
        raise _refuse(item, "recipe.no-candidate", reason, f"recipe work {item}", "run work first")
    snapshot = world.capture(config)
    text = path.read_text(encoding="utf-8")
    done: list[str] = []
    if last and last.get("autofix"):
        fixed, said = errors.autofix(
            snapshot, text, errors.Translated(last["error_class"], 0, "", "", last["autofix"], last.get("name", ""))
        )
        if said:
            text, done = fixed, [*done, said]
    hits = errors.held(snapshot, text, item)
    if hits:
        key, what, why = hits[0][:3]
        lines = ", ".join([str(n) for k, _, _, n, _ in hits if k == key][:5])
        action = f"recipe work {item}"
        extra = brief.neighbours(snapshot, key, what)
        if key == "recipe.cast":  # casts that reach a typed or typable object at offsets its record lacks: add them
            wanted, members = typeitem.casts(snapshot, item, [h[4] for h in hits if h[0] == key])
            if wanted:
                action, extra = f"recipe work {wanted} --kind type", f"; the record lacks {', '.join(members[:8])}"
        raise _refuse(item, key, f"the candidate holds {what} (lines {lines}){extra}", action, why)
    if done:
        host.write(path, text.encode())
    return done


def _finding(row: Json) -> Finding:
    return Finding(**{**row, "versions": tuple(row["versions"]), "missing": tuple(row["missing"]), "origin": None})


def _prove(config: Config, item: str, path: Path) -> Json:
    """unbake proves the candidate in every version and submits it when it is exact."""
    return judge.compare(config, draft.measured(path), item)


def _regression(item: str, text: str, result: Json, last: Json | None) -> str:
    """Why the candidate must give way to the previous content (empty when it may stay): it lost the function or a
    version scores lower than it did."""
    if not last or "text" not in last or result["exact"]:
        return ""
    if not re.search(rf"\b{re.escape(item)}\s*\(", text):
        return f"the candidate no longer contains {item}"
    worse = sorted(v for v, s in result["score"].items() if s < last["score"].get(v, 0.0) - 1e-9)
    if worse:
        scores = ", ".join(
            f"{v} {state.percent(result['score'][v])} was {state.percent(last['score'][v])}" for v in worse
        )
        return f"the candidate scores lower than the previous one ({scores})"
    return ""


def _tracked(config: Config) -> dict[str, str]:
    """Each tracked path git shows changed, with a digest of its content: other writers' work is the baseline."""
    changed = host.git(config, "status", "--porcelain", "--", *_TRACKED).stdout.decode(errors="replace")
    root = config.project.root
    paths = [line[3:].strip() for line in changed.splitlines() if line.strip()]
    return {p: digest((root / p).read_bytes()) if (root / p).is_file() else "" for p in paths}


def _wrote(config: Config, before: dict[str, str], candidate: Path) -> str:
    """The first tracked path this try changed besides its candidate, "" when there is none."""
    own = str(candidate.relative_to(config.project.root)) if candidate.is_absolute() else str(candidate)
    mine = (p for p, d in _tracked(config).items() if p != own and before.get(p) != d and not p.startswith("versions/"))
    return next(mine, "")  # versions/ is unbake sync's, never a try's


def try_(config: Config, params: Json) -> Json:
    now, rules = _now(), state.settings()
    item = params["item"]
    lease = state.lease(config, item)
    if lease is None and not declare.is_item(config, item):
        snapshot = world.capture(config)
        if not _is_open(snapshot, item):
            _landed(config, snapshot, item)
    if lease is None or lease.session != state.session():
        lease = state.held(config, item, now)  # a dead owner's lease is taken over; anything else is refused
    if lease.expires <= now:  # idle time alone never ends a lease this session holds: the verb renews it
        lease = state.take(config, item, now)
    if lease.tries >= rules.stall_tries:
        best = _best(config, item)
        state.skip(config, item, "budget", "budget spent", best, now)
        spent = f"budget spent ({lease.tries} tries without progress), skipped as budget"
        reason = f"{spent}. Best kept: {state.percent(best)}. Fallback: {_fallback(config, item)}"
        raise Refusal(Finding("recipe.budget", reason, unit=item, action="recipe next"))
    baseline, epoch = _tracked(config), api.tree_epoch(config)
    if declare.is_item(config, item):
        return declare.run(config, item, now, bool(params.get("dry")))
    tries = [r for r in host.rows(config, "recipe/tries") if r["item"] == item]
    path = ladder.candidate_path(config, item)
    fixes = _prepare(config, item, tries[-1] if tries else None)
    before = _best(config, item)
    result = _prove(config, item, path)
    # a writer's window (a ship: its epoch moved) rewrites tracked files under any try; only a try's own write counts
    wrote = "" if api.tree_epoch(config) != epoch else _wrote(config, baseline, path)
    if not any(_commits(result.get("drain") or {}, item)) and wrote:
        why = "a candidate may change only its own file; another writer's changes are never a reason to refuse"
        reason = f"this try changed a tracked project file other than the candidate: {config.project.root / wrote}"
        raise _refuse(item, "recipe.dirty", reason, f"git -C {config.project.root} diff -- {wrote}", why)
    landed, saved = _commits(result.get("drain") or {}, item)
    candidate = path.read_text(encoding="utf-8")
    last = next((r for r in reversed(tries) if not r.get("restored")), None)
    reason = _regression(item, candidate, result, last)
    kept = last["text"] if reason and last else candidate
    try:
        out = _report(config, item, path, result, fixes, tries, before, now, kept, bool(reason))
    except Exception as exc:
        raise _late(config, item, landed, saved, exc) from exc
    if reason:  # the lane's text stays where it wrote it: the previous candidate goes beside it, said loudly
        previous = path.with_name(path.name + ".prev")
        host.write(previous, kept.encode("utf-8"))
        out["restored"] = (
            f"{reason}; the file keeps your text (it is not the best candidate), the previous content is in {previous}"
        )
    return out


def _canonical(config: Config, snapshot: Snapshot, name: str) -> list[str]:
    """The landed definition's signature and every declaration that disagrees with it, file and line each."""
    row = next((r for r in declare.conflicts(config, snapshot) if r["name"] == name), None)
    if row is None:
        return []
    out = [f"canonical: {row['definition'][0]} defines `{row['definition'][2]}`"] if row["definition"] else []
    return [*out, *(f"declared: {p}:{n} {t}" for p, n, t in row["spellings"] if (p, n, t) != row["definition"])]


def _report(
    config: Config,
    item: str,
    path: Path,
    result: Json,
    fixes: list[str],
    tries: list[Json],
    before: float,
    now: float,
    text: str,
    restored: bool,
) -> dict[str, Any]:
    rules = state.settings()
    proofs = [Proof(**{**row, "missing": tuple(row["missing"])}) for row in result["proofs"]]
    snapshot = world.capture(config)
    crack.feedback(snapshot, item, "creative", proofs, "", path.read_text(encoding="utf-8"))
    drain = result.get("drain") or {}
    refused = [_finding(f) for body in drain.get("refused", []) for f in body["findings"]]
    exact = bool(result["exact"]) and not refused
    landed, saved = _commits(drain, item)
    packet: dict[str, Any] = {"why": [], "regions": []} if exact else dict(crack.packet(snapshot, item))
    areas = packet["regions"]
    matched = sum(r["kind"] == "match" for r in areas)
    last = next((r["regions"] for r in reversed(tries) if r.get("regions")), None)
    more = last is not None and bool(areas) and matched > last[0]
    missing = [m for p in proofs for m in p.missing]
    compiled = not any(errors.unbuilt(m) for m in missing)
    if landed or saved:
        lease = state.lease(config, item) or state.Lease(item, "", now, now, 0)
    elif (result["exact"] and refused) or not compiled:  # a compile error is no try at the match: it costs no budget
        lease = state.renew(config, item, now)
    else:
        lease = state.count_try(config, item, now, more or state.worst(result["score"]) > before)
    gaps = [_finding(g) for g in result["gaps"]]
    done = exact
    landing = brief.refusals(refused) if result["exact"] else []
    found = [*gaps, *refused] if compiled else []
    translated = None if done or saved else errors.translate(snapshot, path.read_text(encoding="utf-8"), missing, found)
    if translated and landing and translated.cls in ("not-exact", "unknown"):
        translated = errors.Translated(
            "landing-refused", 0, "the candidate matches, but unbake refused the landing", "fix what why: names"
        )
    score = {v: round(s, 4) for v, s in result["score"].items()}
    word = "exact" if exact else "refused" if landing else "failed" if not any(score.values()) else "fuzzy"
    state.record(
        config,
        item,
        now,
        word,
        score,
        error_class=translated.cls if translated else "",
        autofix=translated.autofix if translated else "",
        name=translated.name if translated else "",
        auto_fixes=fixes,
        regions=[matched, len(areas)] if areas else [],
        text=text,
        restored=restored,
    )
    if done:
        state.release(config, item)
    canon: list[str] = []
    if translated and translated.cls == "decl-conflict" and translated.name:
        canon = _canonical(config, snapshot, translated.name)
    packet["why"] = [*landing, *canon, *packet["why"]]
    out: dict[str, Any] = {
        "item": item,
        "result": word,
        "compiled": compiled,
        "score": score,
        "stalled": lease.tries,
        "stall_tries": rules.stall_tries,
        "minutes_left": state.minutes_left(lease, now),
        "auto_fixes": fixes,
        "landed": landed,
        "saved": saved,
        "alike": _alike(snapshot, item, landed),
        "regions": {"matched": matched, "total": len(areas), "gained": matched - last[0] if last else None},
        "file": str(path),
        "why": packet["why"],
        "compiler": list(dict.fromkeys(m.partition(": ")[2] for m in missing if "compile.error" in m))[:3],
        "brief": "" if exact else str(brief.write(snapshot, item, packet, fixes)),
        "next": "recipe next" if done else f"edit the file, then run: recipe try {item}",
    }
    if translated:
        out["error"] = {
            "class": translated.cls,
            "line": translated.line,
            "what": translated.what,
            "fix": translated.fix,
        }
        if translated.line:
            out["next"] = f"edit line {translated.line}, then run: recipe try {item}"
    if translated and translated.cls == "decl-conflict" and translated.name:
        name = translated.name
        out["next"] = (
            f"fix the declaration item {name} first: recipe work {name} --kind declaration (leases it; "
            f"you keep {item}), then recipe try {name}, then recipe try {item}"
        )
    elif not (translated and translated.line) and not exact and (start := brief.first_open(areas)):
        out["next"] = f"{start}; edit the file, then run: recipe try {item}"
    if not done and lease.tries >= rules.stall_tries:
        out["next"] = f"match budget spent: {_fallback(config, item)}"
    return out


def skip(config: Config, params: Json) -> Json:
    try:
        found = _skip_for(config, params["item"], params["reason"], params.get("note") or "", _now())
    finally:
        state.release(config, params["item"])  # a skip frees the lease even when recording it failed
    return {"item": found["item"], "reason": found["reason"], "next": found["next"]}


def look_(config: Config, params: Json) -> Json:
    mine = [x.item for x in state.leases(config, _now()) if x.session == state.session()]
    members = world.of(world.capture(config)).members
    found = dict(look.look(config, params["name"], next((i for i in mine if members[i].kind == "function"), "")))
    if mine:
        state.renew(config, mine[0], _now())
    found["next"] = f"use it in your file, then run: recipe try {mine[0]}" if mine else "recipe next"
    return found


def status(config: Config, params: Json) -> Json:
    snapshot = world.capture(config)
    body = dict(state.status(config, _now()))
    body["head"] = snapshot.commit[:9]
    body["open"] = len(items(config))
    body["twin_ready"] = len(twin.ready(snapshot))
    body["debt"] = [
        {"name": r["name"], "blocks": len(r["items"]), "spellings": len(r["spellings"])}
        for r in declare.conflicts(config, snapshot)[:_DEBT]
    ]
    body["next"] = "recipe next"
    return body
