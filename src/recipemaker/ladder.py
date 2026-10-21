"""The recipe ladder: guard, resume, clone, data literal, draft, proposals. The rungs explore; only a gain lands."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from unbake.contracts import Config, Finding, Json, Proof, Refusal, Snapshot, digest

from recipemaker import crack, draft, errors, headers, host, index, judge, ledger, repair, rungs, state, twin, world

MIN_GAIN = 0.01  # the least score a fuzzy candidate must gain over the landed fuzzy body before it is submitted


@dataclass(frozen=True)
class Rung:
    name: str
    result: str  # "none" | "compiles" | "failed" | "no gain" | "better" | "exact"
    score: float
    note: str = ""


@dataclass(frozen=True)
class Outcome:
    member: str
    rungs: tuple[Rung, ...]
    score: Json  # per version, of the candidate file
    exact: bool
    candidate: str  # absolute path of the best candidate file
    submitted: str | None  # submission id when something was sent to unbake
    drain: Json | None
    auto_fixes: tuple[str, ...] = ()
    left: tuple[ledger.Left, ...] = ()  # M2C_FIELD uses no known type could resolve


@dataclass
class _Item:
    member: str
    path: Path
    measured: bool = False  # a candidate was measured
    proofs: tuple[Proof, ...] = ()
    best: float = 0.0
    text: str = ""  # the best candidate file measured so far, which no later rung may leave worse
    exact: bool = False
    rungs: list[Rung] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    clean: bool = False  # the candidate is not an m2c draft
    left: tuple[ledger.Left, ...] = ()
    extras: dict[str, bytes] = field(default_factory=dict)  # files the winning proposal changes beside the candidate


def candidate_path(config: Config, member: str) -> Path:
    return config.project.root / ".unbake/recipe/work" / f"{host.stem(member)}.c"


def _guard(snapshot: Snapshot, member: str) -> None:
    if member not in world.of(snapshot).members:
        raise Refusal(Finding("land.request", f"{member} is unknown"))
    draft.refuse_fragment(snapshot, member)
    existing = world.unit_of(snapshot, member)
    if existing:
        reason = f"{member} already has source in {existing.path}"
        raise state.stop("land.request", reason, path=existing.path, action="recipe next")


def _measure(snapshot: Snapshot, item: _Item, step: str) -> str:
    """Measure the candidate file in every version, record the attempt, keep it when it is the best so far."""
    verdict = judge.score(snapshot.config, draft.measured(item.path), item.member, item.extras)
    text = item.path.read_text(encoding="utf-8")
    attempt = crack.feedback(snapshot, item.member, step, verdict.proofs, "", text)
    _keep(item, verdict.proofs, attempt.score, attempt.outcome)
    return attempt.outcome


def _keep(item: _Item, proofs: Sequence[Proof], score: float, outcome: str) -> None:
    """Keep the best measured candidate: a worse one is never left in the file."""
    if not item.measured or score > item.best or outcome == "exact":
        item.measured, item.proofs, item.best = True, tuple(proofs), score
        item.text = item.path.read_text(encoding="utf-8")
    elif item.best > 0 and score <= item.best:
        host.write(item.path, item.text.encode())
    item.exact = item.exact or outcome == "exact"


def _word(outcome: str, gained: bool) -> str:
    return "exact" if outcome == "exact" else "failed" if outcome == "failed" else "better" if gained else "no gain"


_WHOLE = ("member.split-delay-slot", "recipe.leased", "recipe.busy", "recipe.budget", "recipe.not-leased")


def _skippable[R](item: _Item, name: str, call: Callable[[], R]) -> R | None:
    """Run one rung."""
    try:
        return call()
    except Refusal as refusal:
        found = refusal.findings[0]
        if found.key in _WHOLE:
            raise
        item.rungs.append(Rung(name, "skipped", item.best, found.reason))
        return None


def _measured(snapshot: Snapshot, item: _Item, step: str) -> None:
    before = item.best
    item.rungs.append(Rung(step, _word(_measure(snapshot, item, step), item.best > before), item.best))


def _seed(snapshot: Snapshot, item: _Item, step: str, make: Callable[[Snapshot, str], str | None]) -> None:
    text = make(snapshot, item.member)
    if text is None:
        item.rungs.append(Rung(step, "none", item.best))
        return
    host.write(item.path, text.encode())
    _measured(snapshot, item, step)


def _known(snapshot: Snapshot, item: _Item) -> tuple[str, float, str] | None:
    fuzzy = world.of(snapshot).fuzzy.get(item.member)
    scores = world.scores(snapshot, item.member)
    options = [(fuzzy, min(scores.values()), _fuzzy_body(snapshot, fuzzy, item.member))] if fuzzy and scores else []
    options += [
        (f"the {a.step} attempt", a.score, a.text)
        for a in state.attempts(snapshot.config, item.member)
        if a.text and a.score > 0 and a.stamp == crack.stamp(snapshot, item.member)  # a failed draft is redrafted
    ]
    best = max(options, key=lambda o: o[1], default=None)
    return best if best and best[2] else None


def _fuzzy_body(snapshot: Snapshot, path: str, member: str) -> str:
    """The module's NON_MATCHING block of `member` (types, helpers, body) and its includes: a resume's seed."""
    source = index.source(snapshot, path)
    body = twin.block(source, member) or ""
    includes = "".join(line + "\n" for line in source.splitlines() if line.startswith("#include"))
    return includes + "\n" * bool(includes) + body


def _resume(snapshot: Snapshot, item: _Item, found: tuple[str, float, str]) -> None:
    label, _, text = found
    text = repair.unfake(repair.unknown(text), item.member)  # an earlier drafter's attempt, cleaned as drafts now are
    _seed(snapshot, item, "resume", lambda s, m: text)
    item.fixes.append(f"resumed from {label} at {state.percent(item.best)}")


def _draft(snapshot: Snapshot, item: _Item) -> None:
    report, extras = draft.drafted(snapshot, item.member, item.path)
    if extras:  # the struct fields m2c read ride with the candidate: every later rung measures with them
        item.extras = {**item.extras, **extras}
        sidecar = {p: {"base": digest(snapshot.peek(p) or b""), "text": t.decode()} for p, t in extras.items()}
        host.write(_sidecar(item.path), json.dumps(sidecar, sort_keys=True).encode())
        snapshot = world.overlay(snapshot, extras)
        item.fixes.append(f"folded the struct fields m2c read into {', '.join(sorted(extras))}")
    original = item.path.read_text(encoding="utf-8")
    result = index.resolve(snapshot, original)
    total = result.resolved + len(result.left)
    if result.resolved:
        host.write(item.path, result.text.encode())
    if total:
        item.fixes.append(f"resolved {result.resolved} of {total} M2C_FIELD")
    item.left = result.left
    best = item.best
    outcome = _measure(snapshot, item, "draft")
    if outcome == "failed" and result.resolved:  # a type fix must never turn a compiling draft into a failing one
        host.write(item.path, original.encode())
        error = next((m for p in item.proofs for m in p.missing if "compile.error" in m), "no compile error recorded")
        changed = [x.strip() for x in result.text.splitlines() if x not in original.splitlines()]
        item.fixes, item.left = (
            [
                f"the {result.resolved} type fixes were dropped: with them the draft did not compile ({error}); "
                f"fixed lines: {'; '.join(changed[:3])}"
            ],
            (),
        )
        outcome = _measure(snapshot, item, "draft")
    if not (best > 0 and item.best == best and outcome != "exact"):  # a draft that did not beat the twin leaves it
        item.clean = False
    word = "compiles" if outcome != "failed" and not report else "failed"
    item.rungs.append(Rung("draft", "exact" if outcome == "exact" else word, item.best))


def _measure_all(
    snapshot: Snapshot, item: _Item, proposals: Sequence[rungs.Proposal]
) -> list[tuple[rungs.Proposal, tuple[Proof, ...]]]:
    """Every proposal screened with its extras overlaid, in one unbake call."""
    if not proposals:
        return []
    rows = []
    for i, proposal in enumerate(proposals):
        path = item.path.with_suffix("") / f"proposal-{i}.c"
        host.write(path, proposal.text.encode())
        extras = {**item.extras, **{p: t.encode() for p, t in proposal.extras.items()}}
        rows.append((draft.measured(path), item.member, extras))
    verdicts = judge.score_many(snapshot.config, rows)
    return [(p, v.proofs) for p, v in zip(proposals, verdicts, strict=True)]


def _propose(snapshot: Snapshot, item: _Item) -> None:
    """Screen every rung's proposals together; one that beats the candidate replaces it and brings its extras, and is
    then measured in every version."""
    held = item.exact  # an exact candidate held by M2C_ text: only an exact one with fewer placeholders replaces it
    found = rungs.propose(snapshot, item.member, item.path.read_text(encoding="utf-8"))
    screened = min((p.score for p in item.proofs if p.version == snapshot.config.project.names_from), default=0.0)
    for proposal, proofs in _measure_all(snapshot, item, found):
        attempt = crack.feedback(snapshot, item.member, proposal.name, proofs, "", proposal.text, record=False)
        fewer = proposal.text.count("M2C_") < item.path.read_text(encoding="utf-8").count("M2C_")
        named = attempt.score >= screened and fewer  # as good, fewer held
        if held and not (attempt.outcome == "exact" and fewer):
            item.rungs.append(Rung(proposal.name, _word(attempt.outcome, False), item.best))
            continue
        if not (attempt.score > screened or attempt.outcome == "exact" or named):
            item.rungs.append(Rung(proposal.name, _word(attempt.outcome, False), item.best))
            continue
        before, kept = item.best, (item.text, item.proofs, item.extras)
        host.write(item.path, proposal.text.encode())
        item.extras = {**item.extras, **{p: t.encode() for p, t in proposal.extras.items()}}
        verdict = judge.score(snapshot.config, draft.measured(item.path), item.member, item.extras)  # every version
        full = crack.feedback(snapshot, item.member, proposal.name, verdict.proofs, "", proposal.text)
        if full.score > before or full.outcome == "exact" or (named and full.score >= before):
            item.measured, item.proofs, item.best, item.text = (
                True,
                verdict.proofs,
                max(before, full.score),
                proposal.text,
            )
            item.exact = item.exact or full.outcome == "exact"
            item.fixes.extend(proposal.evidence)
            screened = max(screened, attempt.score)
        else:  # worse in another version: the earlier candidate stays
            host.write(item.path, kept[0].encode())
            item.text, item.proofs, item.extras = kept
        item.rungs.append(Rung(proposal.name, _word(full.outcome, item.best > before), item.best))


def _land(config: Config, snapshot: Snapshot, item: _Item) -> tuple[str | None, Json | None]:
    gain = MIN_GAIN
    baseline = min(world.scores(snapshot, item.member).values(), default=0.0)
    held = errors.held(
        snapshot, item.path.read_text(encoding="utf-8"), item.member
    )  # no M2C_ text, casts or unused locals
    if not item.measured or held or not (item.exact or item.best - baseline >= gain):
        if item.measured and held:  # what keeps it from landing, named: never a silent fuzzy result
            item.fixes.append(f"held: {len(held)} refusable lines ({held[0][1]} on line {held[0][3]})")
        elif item.measured and item.best > 0:
            item.fixes.append(
                f"held: {state.percent(item.best)} gains less than {state.percent(gain)} on {baseline:.0%}"
            )
        return None, None
    extras = item.extras
    if extras and not item.exact and not _headers_landed(config, item):
        return None, None  # a fuzzy file lands alone: its header edits land first, as an exact change set, or neither
    if not item.exact:
        extras = {}
    result = judge.submit(config, draft.measured(item.path), item.member, extras)
    sent = _submitted(result, item.member)
    return (sent, result.get("drain")) if sent else (None, None)


def _submitted(result: Json, member: str) -> str | None:
    """The inbox id unbake gave the submission of `member`, None when it admitted none."""
    return next((r["id"] for r in result.get("submissions", ()) if r["member"] == member and r["id"]), None)


def primary(snapshot: Snapshot, edits: Mapping[str, bytes]) -> str | None:
    """The landed unit a change set is submitted under: an edited one, else one that includes an edited header."""
    units = world.of(snapshot).units
    edited = next((p for p in sorted(units) if p in edits), None)
    if edited:
        return edited
    reached = set(headers.consumers(snapshot, [p for p in edits if p.endswith(".h")]))  # through other headers too
    return next((p for p in sorted(units) if p in reached), None)


def _headers_landed(config: Config, item: _Item) -> bool:
    """Land the header edits of a fuzzy candidate as their own exact change set; True when they are on the tree."""
    snapshot = world.capture(config)
    if all(snapshot.peek(p) == t for p, t in item.extras.items()):
        return True
    path = primary(snapshot, item.extras)
    if path is None:
        item.fixes.append(f"held the fuzzy file: no landed unit includes {', '.join(sorted(item.extras))}")
        return False
    try:
        proofs, drain = land_set(config, snapshot, world.of(snapshot).units[path], dict(item.extras))
    except Refusal as refusal:  # the fields fail a landing rule: the fuzzy file cannot land without them
        item.fixes.append(f"the folded header fields were refused: {refusal.findings[0].reason}")
        return False
    landed = commits(drain).get("publish", "")
    gap = next((f"{p.version} {m}" for p in proofs for m in p.missing if not p.exact), "")
    refused = [f["reason"] for body in (drain or {}).get("refused", []) for f in body.get("findings", [])]
    why = f"{path} is not exact with them: {gap or 'no proof'}" if not drain else "; ".join(refused) or "none landed"
    item.fixes.append(
        f"landed the folded header fields first ({landed})"
        if landed
        else f"held the fuzzy file: the folded header fields did not land ({why[:200]})"
    )  # a fuzzy file never lands without them
    return bool(landed)


def _sidecar(path: Path) -> Path:
    return path.with_suffix(".extras.json")


def _carried(snapshot: Snapshot, item: _Item) -> dict[str, bytes] | None:
    """The header edits a draft of an earlier command was measured with, while the headers it edited are unchanged
    since; edits the tree already holds are none, and edits over a header that moved since are dropped with the
    candidate, which is then drafted again."""
    side = _sidecar(item.path)
    if not side.is_file():
        return {}
    rows = json.loads(side.read_text(encoding="utf-8"))
    now = {p: snapshot.peek(p) or b"" for p in rows}
    if all(now[p] == r["text"].encode() for p, r in rows.items()):
        side.unlink()
        return {}
    if all(digest(now[p]) == r["base"] for p, r in rows.items()):
        return {p: r["text"].encode() for p, r in rows.items()}
    side.unlink()
    item.path.unlink(missing_ok=True)
    return None  # a resume of that draft would miss the edits too


def _unbuilt_draft(snapshot: Snapshot, item: _Item) -> bool:
    """Whether the candidate file is the tool's own skeleton or a draft it measured as not compiling: a newer drafter
    may build it, and a file left like this was never edited."""
    if not item.path.is_file():
        return False
    text = item.path.read_text(encoding="utf-8")
    failed = {a.text for a in state.attempts(snapshot.config, item.member) if a.outcome == "failed" and a.text}
    return text in failed or text == draft.skeleton(snapshot, item.member)


def run(config: Config, member: str, *, submit: bool) -> Outcome:
    snapshot = world.capture(config)
    item = _Item(member, candidate_path(config, member))
    _guard(snapshot, member)
    kind = world.of(snapshot).members[member].kind
    carried = _carried(snapshot, item) if kind == "function" else {}
    if carried:
        item.extras = carried
        snapshot = world.overlay(snapshot, carried)
    known = _known(snapshot, item) if carried is not None else None
    if kind == "function" and _unbuilt_draft(snapshot, item):
        item.path.unlink()  # a draft or skeleton the tool left that never compiled is drafted again, never kept
    edited = item.path.exists()
    if edited and kind == "function":  # the same code without M2C_UNK or per-function callee records
        old = item.path.read_text(encoding="utf-8")
        if (new := repair.unfake(repair.unknown(old), member)) != old:
            host.write(item.path, new.encode())
            item.fixes.append("spelled m2c's unknown types and per-function callee records as project types")
    if edited:  # an edited candidate is measured, never overwritten by a worse one
        _skippable(item, "existing", lambda: _measured(snapshot, item, "existing"))
        if known and known[1] > item.best:
            _skippable(item, "resume", lambda: _resume(snapshot, item, known))
        item.clean = True
    elif known:
        _skippable(item, "resume", lambda: _resume(snapshot, item, known))
        item.clean = True
    elif kind == "data":
        _skippable(item, "data", lambda: _seed(snapshot, item, "data", draft.data_text))
    if not edited and kind == "function" and item.best == 0 and not item.exact:  # nothing yet, or a dead resume
        _skippable(item, "clone", lambda: _seed(snapshot, item, "clone", twin.candidate))
        item.clean = item.best > 0
        if kind == "function" and not item.exact:
            _skippable(item, "draft", lambda: _draft(snapshot, item))
            if not item.path.exists():  # m2c could not draft it: `try` starts from a skeleton
                host.write(item.path, draft.skeleton(snapshot, member).encode())
                item.fixes.append(
                    f"no draft ({item.rungs[-1].note}): the file is a skeleton, write the body from the target assembly"
                )
    if item.extras:  # a draft's folded header edits hold for every later rung
        snapshot = world.overlay(snapshot, item.extras)
    held = item.path.exists() and "M2C_" in item.path.read_text(encoding="utf-8")  # exact, but no source lands it
    if kind == "function" and (not item.exact or held) and rungs.RUNGS:
        _skippable(item, "proposals", partial(_propose, snapshot, item))
    if kind == "function" and item.path.exists() and "M2C_FIELD(" in (text := item.path.read_text(encoding="utf-8")):
        left = index.resolve(snapshot, text).left  # what the best candidate still leaves, each with why
        item.left = rungs.classify(snapshot, member, text, left)
    return _finish(config, snapshot, item, submit, kind == "data")


def carrier(snapshot: Snapshot, unit: world.Unit, edits: Mapping[str, bytes]) -> tuple[str, str]:
    """The landed function of `unit` (and its body, from the edited source when the set edits it) that carries a change
    set through the compiler."""
    layout = world.of(snapshot)
    source = (edits.get(unit.path) or snapshot.read(unit.path)).decode(errors="replace")
    landed = [n for n in unit.members if n in layout.members and layout.members[n].kind == "function"]
    found = next(((n, body) for n in landed if (body := twin.function(source, n))), None)
    if found is None:
        raise Refusal(
            Finding("land.request", f"{unit.path} defines no function to carry the change set", path=unit.path)
        )
    return found


def land_set(
    config: Config, snapshot: Snapshot, unit: world.Unit, edits: dict[str, bytes]
) -> tuple[tuple[Proof, ...], Json]:
    """Prove a landed function of the unit with the edited files overlaid; when it is exact, submit the edits as one
    change set under it and land it. Returns the proofs and the drain ({} when nothing was submitted)."""
    name, body = carrier(snapshot, unit, edits)
    with host.work(config) as work:
        file = work / f"{host.stem(name)}.c"
        host.write(file, body.encode())
        others = dict(edits)  # the unit's own file too: the candidate is spliced into its edited text
        verdict = judge.score(config, file, name, others)
        if not verdict.exact:
            return verdict.proofs, {}
        result = judge.submit(config, file, name, others)
    return verdict.proofs, result.get("drain") or {}


SCREEN_UNITS = 4  # the most edited sources one change set is compiled through (a row costs about eight CPU s)

Screened = tuple[world.Unit, Mapping[str, bytes], Sequence[str]]  # primary unit, the set, the units to compile first


def _carriers(snapshot: Snapshot, one: Screened) -> list[tuple[str, str]]:
    """The functions that carry a change set through the compiler: one of the primary unit, and one of up to
    SCREEN_UNITS - 1 other landed units the set edits (the `risky` ones first), so those sources are compiled as the
    build will compile them."""
    unit, edits, risky = one
    units = world.of(snapshot).units
    found = [carrier(snapshot, unit, edits)]
    others = [p for p in dict.fromkeys([*risky, *sorted(edits)]) if p in edits and p in units and p != unit.path]
    for path in others[: SCREEN_UNITS - 1]:
        with suppress(Refusal):
            found.append(carrier(snapshot, units[path], edits))
    return found


def screen(config: Config, snapshot: Snapshot, sets: Sequence[Screened]) -> list[judge.Verdict]:
    """The verdict of each change set, some of the sources it edits compiled with the whole set laid over the tree: all
    in one unbake process on every core and in the reference version only, so a set that fails here never reaches the
    build. A set unbake cannot measure at all (it refuses the whole call) is split out: the others are measured
    again."""
    if not sets:
        return []
    with host.work(config) as work:
        rows, owner = [], []
        for n, one in enumerate(sets):
            for k, (name, body) in enumerate(_carriers(snapshot, one)):
                file = work / f"{n}-{k}-{host.stem(name)}.c"
                host.write(file, body.encode())
                rows.append((file, name, dict(one[1])))
                owner.append(n)
        try:
            verdicts = judge.score_many(config, rows)
        except Refusal as refusal:
            if len(sets) == 1:
                reason = "; ".join(f.reason for f in refusal.findings)[-600:]
                return [judge.Verdict((judge.proof_failed(rows[0][1], reason),), False, ())]
            verdicts = []
    if not verdicts:
        half = len(sets) // 2
        return [*screen(config, snapshot, sets[:half]), *screen(config, snapshot, sets[half:])]
    out = []
    for n in range(len(sets)):
        mine = [v for v, o in zip(verdicts, owner, strict=True) if o == n]
        out.append(judge.Verdict(tuple(p for v in mine for p in v.proofs), all(v.exact for v in mine), mine[0].diff))
    return out


def commits(drain: Json | None, member: str | None = None) -> dict[str, str]:
    """The commit of each landed drain row by operation, for one member or all; a row without one is named by its id."""
    rows = (drain or {}).get("landed", [])
    return {r["operation"]: r.get("commit") or r["id"] for r in rows if member in (None, r["member"])}


def _finish(config: Config, snapshot: Snapshot, item: _Item, submit: bool, data: bool) -> Outcome:
    sent, drain = _land(config, snapshot, item) if submit and not data else (None, None)
    proofs = {p.version: p.score for p in item.proofs if p.member == item.member}
    return Outcome(
        item.member,
        tuple(item.rungs),
        proofs,
        item.exact and not data,
        str(item.path),
        sent,
        drain,
        tuple(item.fixes),
        item.left,
    )
