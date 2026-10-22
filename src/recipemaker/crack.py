"""Measured attempts and packets for the recipe ladder."""

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from importlib import metadata

from unbake.contracts import Finding, Json, Proof, Refusal, Snapshot, digest

from recipemaker import advice, draft, errors, host, index, judge, regions, signatures, state, twin, world
from recipemaker.state import Attempt


def stamp(snapshot: Snapshot, member: str) -> str:
    """What an attempt at `member` is made from: this recipe release, the member's reference assembly and its
    prototype (an attempt made against a `void *` is stale once a type item typed it)."""
    entry = world.of(snapshot).members.get(member)
    try:
        asm = index.asm(snapshot, member, entry.reference(snapshot.config.project.names_from)) if entry else ""
    except (Refusal, OSError):  # a member without assembly (data) is made from the release alone
        asm = ""
    return digest((metadata.version("recipemaker"), asm, index.get(snapshot).declarations.get(member, ("", 0, ""))[2]))


def feedback(
    snapshot: Snapshot, member: str, step: str, proofs: Sequence[Proof], note: str, text: str, *, record: bool = True
) -> Attempt:
    """The attempt a measurement makes; recorded in the member's attempt stream unless it is a screen."""
    with host.stage("crack.feedback"):
        mine = [p for p in proofs if p.member == member]
        score = min((p.score for p in mine), default=0.0)
        prior = state.attempts(snapshot.config, member)
        best_before = max((a.score for a in prior), default=0.0)
        failed = any(p.symptoms.get(k) for p in mine for k in ("compile_failed", "no_measurement", "unresolved"))
        if mine and all(p.exact for p in mine):
            outcome = "exact"
        elif score == 0 and failed:
            outcome = "failed"
        else:
            outcome = "better" if score > best_before else "same" if score == best_before else "worse"
        facts = dict(min(mine, key=lambda p: p.score).symptoms) if mine else {}
        now = datetime.now(UTC).isoformat()
        attempt = Attempt(
            member,
            step,
            score,
            best_before,
            outcome,
            facts,
            note,
            now,
            text,
            stamp(snapshot, member),
        )
        if not record:
            return attempt
        stored = json.loads(json.dumps(asdict(attempt), sort_keys=True))
        host.append(snapshot.config, f"recipe-attempts/{host.stem(member)}", stored)
        return attempt


def collapse(rows: Sequence[tuple[str, str]], total: int) -> list[str]:
    """Each distinct text once, led by the versions that have it or by "all versions" when every version does."""
    seen: dict[str, list[str]] = {}
    for version, text in rows:
        seen.setdefault(text, []).append(version)
    return [f"{'all versions' if len(v) == total else ', '.join(v)}: {t}" for t, v in seen.items()]


def _why(proofs: Sequence[Proof]) -> list[str]:
    """Per version that is not exact, what differs; each distinct text once, led by the versions that have it."""
    rows: list[tuple[str, str]] = []
    for p in proofs:
        size, frame = p.symptoms.get("size_delta_bytes", 0), p.symptoms.get("frame_delta_bytes", 0)
        if size:
            rows.append(
                (p.version, f"built is {abs(size)} bytes {'longer' if size > 0 else 'shorter'} than the target")
            )
        if frame:
            rows.append(
                (
                    p.version,
                    f"stack frame is {abs(frame)} bytes {'larger' if frame < 0 else 'smaller'} than the target's",
                )
            )
        rows += [(p.version, m.removeprefix(f"version {p.version}: ")) for m in p.missing]
    return collapse([(v, m) for v, m in rows if not errors.unbuilt(m)], len(proofs))


def delay_slot(diff: str, start: int) -> str:
    """Where the target keeps a nop that the build fills with an instruction, or the reverse; "" when neither."""
    rows = {sign: [x[1:].split(": ", 1) for x in diff.splitlines() if x[:1] == sign and ": " in x] for sign in "-+"}
    for kept, filled, said in (
        ("-", "+", "the target keeps a nop that the build fills"),
        ("+", "-", "the build leaves a nop the target fills"),
    ):
        nops = [t for _, t in rows[kept] if t == "nop"]
        other = [(a, t) for a, t in rows[filled] if t != "nop"]
        if nops and other and not any(t == "nop" for _, t in rows[filled]):
            address = int((rows["-"][0] if kept == "-" else other[0])[0], 16) - start
            return (
                f"{said} in a delay slot at +0x{address:X} with `{other[0][1]}`: move that work in or out of the branch"
            )
    return ""


_LOAD = re.compile(r"l(?:[bhwd]u?|wc1|dc1|w[lr])\s")


def load_delay(diff: str) -> str:
    """Evidence that the target or the build puts a nop after a load and the other does not, "" when neither does."""
    previous = {"-": "", "+": ""}
    found: dict[str, str] = {}
    for line in diff.splitlines():
        sign, _, text = line[:1], *line[1:].partition(": ")[1:]
        if sign not in " -+" or not text or line.startswith(("---", "+++")):
            continue
        for side in "-+":
            if sign in (" ", side):
                if text == "nop" and _LOAD.match(previous[side]):
                    found.setdefault(side, previous[side])
                previous[side] = text
    if len(found) != 1:
        return ""
    side, load = next(iter(found.items()))
    return advice("toolchain").format(f"{'target' if side == '-' else 'build'} has a nop after `{load}`")


def same_code(diff: str, start: int) -> str:
    """What the brief says when the differing code is the target's own reordered or re-registered, else none."""
    target, built = ([x[1:].split(": ", 1) for x in diff.splitlines() if x[0] == sign and ": " in x] for sign in "-+")
    how = regions.kind([t for _, t in target], [b for _, b in built])
    if how not in ("order", "register", "compare"):
        return ""
    return advice(how).format(f"+0x{int(target[0][0], 16) - start:X}")


def _twin(found: twin.Twin) -> Json:
    """The landed function of the same shape the candidate was cloned from."""
    return {"function": found.counterpart, "file": found.unit, "version": found.other_version}


def packet(snapshot: Snapshot, member: str) -> Json:
    """What the brief shows about the candidate."""
    with host.stage("crack.packet"):
        config, root = snapshot.config, snapshot.config.project.root
        source = f".unbake/recipe/work/{host.stem(member)}.c"
        if not (root / source).exists():
            raise Refusal(Finding("land.request", f"{member} has no candidate", action=f"recipe work {member}"))
        verdict = judge.score(config, draft.measured(root / source), member)
        proofs = [p for p in verdict.proofs if p.member == member]
        item = world.of(snapshot).members[member]
        target: dict[str, Json] = {}
        for version in item.holders():
            place = next(p for p in item.placements if p.version == version)
            asm = item.kind == "function" and world.asm_path(config, version, member).relative_to(root).as_posix()
            target[version] = {"asm": asm or "", "size": place.size}
        diff, same, toolchain, worst = "", "", "", ""
        measured = [p for p in proofs if p.built_sha256]  # a candidate that did not link has no built bytes
        if item.kind == "function" and measured:
            proof = min(measured, key=lambda p: p.score)
            worst = proof.version
            place = next(p for p in item.placements if p.version == proof.version and p.section == ".text")
            if proof.version != (proofs[0].version if proofs else ""):  # the diff of the worst version
                verdict = judge.score(config, draft.measured(root / source), member, version=proof.version)
            diff = "\n".join(verdict.diff)
            same = same_code(diff, place.vram)
            toolchain = load_delay(diff).replace("{item}", member)
            asm = index.asm(snapshot, member, proof.version)
            found_regions = regions.classify(regions.split(asm), diff, place.vram)
        elif item.kind == "function":  # nothing was built: the target alone gives the regions, none of them built
            asm = index.asm(snapshot, member)
            found_regions = [replace(r, kind="not built") for r in regions.split(asm)]
        else:
            found_regions = []
        areas = [{**asdict(r), "anchors": r.anchors()} for r in found_regions]
        found = twin.find(snapshot, member)
        names = []
        if item.kind == "function":
            names = list(
                dict.fromkeys(
                    a or b
                    for a, b in re.findall(
                        r"\bjal\s+([\w.$]+)|%(?:hi|lo)\(\s*([\w.$]+)\s*\)", index.asm(snapshot, member)
                    )
                )
            )
        return {
            "member": member,
            "kind": item.kind,
            "target": target,
            "best": {
                "source": source,
                "score": {p.version: p.score for p in proofs},
                "errors": [m for p in proofs for m in p.missing if errors.unbuilt(m)],
            },
            "diff": diff,
            "worst": worst,
            "regions": areas,
            "why": [
                *([toolchain] if toolchain else []),
                *_why(proofs),
                *regions.missing_calls(found_regions),
                *([same] if same else []),
                *filter(None, [delay_slot(diff, place.vram)]),
            ],
            "toolchain": bool(toolchain),
            "permutable": bool(same) and not same.startswith("operand"),
            "twin": _twin(found) if found else None,
            "types": {
                "signature": signatures.signature(snapshot, [member, *names]).get(member, ""),
                "declarations": list(signatures.declarations(snapshot, names).values()),
            },
        }
