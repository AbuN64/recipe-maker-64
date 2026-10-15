"""Measured attempts, packets and the permuter step for the recipe ladder (moved from unbake)."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from unbake import adapters, compare, effort, layout, pool, process, recipes, store, symptoms, versions, view
from unbake.contracts import Config, Finding, Json, Proof, Recipe, Refusal, Snapshot, UnitSpec, digest

from recipemaker import advice, cparse, draft, errors, index, regions, signatures, state, twin
from recipemaker.state import Attempt


def stamp(snapshot: Snapshot, member: str) -> str:
    """What an attempt at `member` is made from: this recipe release and the member's reference assembly."""
    entry = snapshot.layout.members.get(member)
    try:
        asm = index.asm(snapshot, member, entry.reference(snapshot.config.project.names_from)) if entry else ""
    except Refusal:  # a member without assembly (data) is made from the release alone
        asm = ""
    return digest((metadata.version("recipemaker"), asm))


def feedback(snapshot: Snapshot, member: str, step: str, proofs: Sequence[Proof], note: str, text: str) -> Attempt:
    with effort.stage("crack.feedback"):
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
        stored = json.loads(json.dumps(asdict(attempt), sort_keys=True))
        store.append(snapshot.config, f"recipe-attempts/{store.stem(member)}", stored)
        return attempt


def _missing() -> bytes:
    raise Refusal(Finding("store.corrupt", "measured candidate bytes are missing from the cache"))


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


def _twin(snapshot: Snapshot, member: str, found: twin.Twin, unit: UnitSpec) -> Json:
    """The twin, and what differs between its version and this one: macros, unit flags and the target bytes."""
    config, members = snapshot.config, snapshot.layout.members

    def code(name: str, version: str) -> bytes:
        place = next(p for p in members[name].placements if p.version == version and p.section == ".text")
        return versions.rom_bytes(snapshot.versions[version], place.rom_start, place.rom_end)

    other = layout.unit_of(snapshot, found.counterpart)
    if other is None:
        raise Refusal(Finding("land.request", f"{found.counterpart} has no unit", unit=found.counterpart))
    ours, theirs = (recipes.resolve(config, u, {}) for u in (unit, other))
    macros = config.project.version_macros
    vram = next(p.vram for p in members[member].placements if p.version == found.version and p.section == ".text")
    return {
        "function": found.counterpart,
        "file": found.unit,
        "version": found.other_version,
        "macros": [macros[found.version], macros[found.other_version]],
        "flags": {k: [getattr(ours, k), getattr(theirs, k)] for k in ("toolchain", "cppflags", "cflags", "asflags")},
        "diff": symptoms.diff(code(found.counterpart, found.other_version), code(member, found.version), vram),
    }


def built_diff(snapshot: Snapshot, member: str, proof: Proof) -> str:
    """The target bytes of `member` in the proof's version against what the proof built, as a unified diff."""
    place = next(
        p for p in snapshot.layout.members[member].placements if p.version == proof.version and p.section == ".text"
    )
    built = store.cached(snapshot.config, "bytes", proof.built_sha256, _missing)
    target = versions.rom_bytes(snapshot.versions[proof.version], place.rom_start, place.rom_end)
    return symptoms.diff(built, target, place.vram)


def packet(snapshot: Snapshot, member: str) -> Json:
    """What the brief shows about the candidate."""
    with effort.stage("crack.packet"):
        config, root = snapshot.config, snapshot.config.project.root
        source = f".unbake/recipe/work/{store.stem(member)}.c"
        if not (root / source).exists():
            raise Refusal(Finding("land.request", f"{member} has no candidate", action=f"recipe work {member}"))
        unit, bound = draft.bind(snapshot, root / source, member)
        proofs = [p for p in compare.measure(bound, unit, {}, None) if p.member == member]
        item = snapshot.layout.members[member]
        target: dict[str, Json] = {}
        for version in item.holders():
            place = next(p for p in item.placements if p.version == version)
            asm = item.kind == "function" and versions.asm_path(config, version, member).relative_to(root).as_posix()
            target[version] = {"asm": asm or "", "size": place.size}
        diff, same, toolchain, worst = "", "", "", ""
        measured = [p for p in proofs if p.built_sha256]  # a candidate that did not link has no built bytes
        if item.kind == "function" and measured:
            proof = min(measured, key=lambda p: p.score)
            worst = proof.version
            place = next(p for p in item.placements if p.version == proof.version and p.section == ".text")
            diff = built_diff(snapshot, member, proof)
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
            "twin": _twin(snapshot, member, found, unit) if found else None,
            "types": {
                "signature": signatures.signature(snapshot, [member, *names]).get(member, ""),
                "declarations": list(signatures.declarations(snapshot, names).values()),
            },
        }


def permutable(snapshot: Snapshot, member: str) -> str:
    """ "" when the permuter may run on the candidate, else why not: its diff is not an order or register near miss."""
    packet_ = packet(snapshot, member)
    if packet_["permutable"]:
        return ""
    classes = ", ".join(sorted({r["kind"] for r in packet_["regions"] if r["kind"] != "match"})) or "other"
    return f"diff class {classes}; only order or register near misses permute; add PERM_ marks to force it"


def permute_file(config: Config, path: Path, member: str, seconds: float) -> bytes | None:
    """The permuter on the candidate file as it is now: its best output as the whole file, or None."""
    snapshot = layout.capture(config)
    unit, bound = draft.bind(snapshot, path, member)
    return permute(config, bound, unit, member, recipes.resolve(config, unit, {}), seconds)


def permute(
    config: Config, snap: Snapshot, unit: UnitSpec, member: str, recipe: Recipe, seconds: float
) -> bytes | None:
    version = snap.layout.members[member].reference(config.project.names_from)
    source = snap.read(unit.path).decode(errors="replace")
    with store.work(config) as work:
        # the permuter runs plain `cpp` on base.c: give it the unit preprocessed by unbake, not the raw source
        unit_text = view.get(snap, unit, version, recipe, lines=False).text
        (work / "base.c").write_text(cparse.reach(unit_text, member), encoding="utf-8")
        include = [config.project.root / "asm" / version / "include", config.project.root / "include"]
        adapters.assembler(config, "gnu").assemble(
            versions.asm_path(config, version, member), recipe, include, work / "target.o"
        )
        script = work / "compile.sh"
        script.write_text(adapters.script(config, recipe, version))
        script.chmod(0o755)
        tool = state.tool("permuter")
        identity = tool.read_bytes() if tool.is_file() else str(tool).encode()
        key = digest(
            [
                (work / "base.c").read_bytes(),
                (work / "target.o").read_bytes(),
                script.read_text(),
                identity,
                seconds,
            ]
        )
        # a run is a pure function of its inputs and budget: the same base is never searched twice for the same time
        found = store.cached(config, "recipe.permute", key, lambda: _search(config, work, tool, seconds, member))
        if not found.startswith(b"Y"):
            return None
        best, old = found[1:].decode(), twin.function(source, member)
        new = twin.function(best, member)  # only the function comes back, spliced into the unit as it was written
        return source.replace(old, new).encode() if old and new else None


PERMUTERS = 4  # the permuter workers one try runs: a slot is held for each, never the whole host share


def _search(config: Config, work: Path, tool: Path, seconds: float, member: str) -> bytes:
    """Run the permuter in `work`: b"Y" and its best source, or b"N" when it found nothing."""
    with pool.cores(config.host, work=PERMUTERS) as jobs:  # one slot per permuter worker it runs, no more
        ran = process.run(
            "permuter",
            [str(tool), str(work), "--stop-on-zero", "-j", str(jobs)],
            cwd=work,
            timeout=seconds,
            tmp=process.scratch(config.project.root),
        )
    if ran.exit not in (None, 0):  # None is the timeout kill, which is how a run that found nothing ends
        said = [x for x in (ran.stderr or ran.stdout).decode(errors="replace").splitlines() if x.strip()]
        raise Refusal(Finding("recipe.permuter", said[-1] if said else f"permuter exited {ran.exit}", unit=member))
    outputs = [
        (int(match[1]), entry.name, entry / "source.c")
        for entry in work.iterdir()
        if (match := re.fullmatch(r"output-(\d+)-\d+", entry.name)) and (entry / "source.c").is_file()
    ]
    return b"Y" + min(outputs)[2].read_bytes() if outputs else b"N"
