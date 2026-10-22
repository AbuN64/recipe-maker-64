"""Cracking ladder unit tests: no native tools, repositories, ROMs or network."""

import json
from dataclasses import asdict, replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import fixture
import pytest
from unbake.contracts import Config, Host, Project, Refusal, Version

from recipemaker import crack, judge
from recipemaker.world import Group, Layout, Member, Placement

MEMBER = "func_80000400"
SOURCE = b"int func_80000400(void) { return 0; }\n"
DRAIN = {"running": False, "landed": [], "refused": []}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    project = Project(
        tmp_path, "fixture", "Fixture", "gcc-test", "a", (), (), (), (), None, None, ("a", "b"), {}, {}, "0" * 64
    )
    host = Host(
        2, 2, 1 << 30, 1 << 30, tmp_path / "cache", 1 << 30, tmp_path / "tools", {}, None, 2, 2, 0.8, 2,
        ("test", "test@example.invalid"), {}, "0" * 64,
    )  # fmt: skip
    config = Config(project, host, "0" * 64)
    places = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in ("a", "b"))
    layout = Layout(
        {"src/main.c": Group("src/main.c", "main", (MEMBER,), False, "src/main.c")},
        {MEMBER: Member(MEMBER, "function", "src/main.c", places, False)},
        {},
        {},
    )
    versions = {v: Version(v, tmp_path / f"{v}.z64", "0" * 64, "", "", {}, ()) for v in ("a", "b")}
    snapshot = SimpleNamespace(config=config, commit="0" * 40, versions=versions, overlays={}, digest="0" * 64)
    snapshot.layout = layout
    rows = []
    monkeypatch.setattr(crack.twin, "find", lambda snap, member: None)
    monkeypatch.setattr(crack.host, "rows", Mock(side_effect=lambda cfg, stream: list(rows)))
    monkeypatch.setattr(crack.host, "append", Mock(side_effect=lambda cfg, stream, row: rows.append(row)))
    monkeypatch.setattr(crack.host, "run", Mock(side_effect=AssertionError("unmocked native invocation")))
    monkeypatch.setattr(
        crack.world, "asm_path", lambda cfg, version, member: cfg.project.root / f"asm/{version}/{member}.s"
    )
    return snapshot, rows


def _proof(score, version="a", symptoms=None, member=MEMBER):
    return fixture.proof(
        f".unbake/recipe/work/{MEMBER}.c",
        member,
        version,
        score == 1,
        missing=() if score == 1 else ("bytes",),
        score=score,
        symptoms=symptoms,
    )


def _prior(rows, **fields):
    rows.append(asdict(fixture.attempt(**{"member": MEMBER, "step": "draft", **fields})))


def _candidate(snapshot):
    path = snapshot.config.project.root / f".unbake/recipe/work/{MEMBER}.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SOURCE)
    return path


def _measurement(snapshot, monkeypatch, proofs=None, diff=("measured diff",)):
    found = tuple(proofs or (_proof(0.5), _proof(0.4, "b")))
    score = Mock(return_value=judge.Verdict(found, all(p.exact for p in found), diff))
    monkeypatch.setattr(crack.judge, "score", score)
    for version in ("a", "b"):
        asm = crack.world.asm_path(snapshot.config, version, MEMBER)
        asm.parent.mkdir(parents=True, exist_ok=True)
        asm.write_text("jal callee\nlui $t0, %hi(global)\naddiu $t0, $t0, %lo(global)\njal callee\n")
    monkeypatch.setattr(crack.signatures, "signature", Mock(return_value={MEMBER: "int f(void)"}))
    monkeypatch.setattr(
        crack.signatures,
        "declarations",
        Mock(return_value={"callee": "void callee(void);", "global": "extern int global;"}),
    )
    return score


@pytest.mark.parametrize(
    "scores,facts,before,outcome",
    [
        ((1, 1), {}, 0.8, "exact"),
        ((1, 0.6), {}, 0.5, "better"),
        ((0.6, 0.4), {"bytes_differ": True}, 0.4, "same"),
        ((0.6, 0.3), {"bytes_differ": True}, 0.4, "worse"),
        ((0.2, 0), {"compile_failed": True}, 0.4, "failed"),
        ((0.2, 0), {"no_measurement": True}, 0.0, "failed"),
        ((0.2, 0), {"unresolved": True}, 0.0, "failed"),
        ((0.2, 0), {}, 0.0, "same"),
        ((), {}, 0.0, "same"),
    ],
)
def test_feedback_outcomes(lane, scores, facts, before, outcome):
    snapshot, rows = lane
    _prior(rows, score=before, outcome="same")
    _prior(rows, score=before, outcome="worse")
    proofs = [_proof(score, v, {} if score == 1 else facts) for score, v in zip(scores, ("a", "b"), strict=False)]
    proofs.append(_proof(0, member="unrelated", symptoms={"compile_failed": True}))
    attempt = crack.feedback(snapshot, MEMBER, "draft", proofs, "note", "text")
    assert attempt.outcome == outcome
    assert attempt.score == min(scores, default=0)
    assert attempt.best_before == before
    assert attempt.symptoms == facts
    if len(proofs) > 1:
        assert proofs[0].symptoms == ({} if scores[0] == 1 else facts)


def test_feedback_records_attempt_and_a_screen_records_none(lane):
    snapshot, rows = lane
    proofs = [_proof(0.8), _proof(0.4, "b")]
    crack.feedback(snapshot, MEMBER, "screen", proofs, "", "text", record=False)
    assert rows == []
    attempt = crack.feedback(snapshot, MEMBER, "draft", proofs, "retain this technique", "text")
    assert rows == [json.loads(json.dumps(asdict(attempt)))]
    crack.host.append.assert_called_once_with(snapshot.config, f"recipe-attempts/{MEMBER}", rows[0])
    assert attempt.note == "retain this technique"
    assert datetime.fromisoformat(attempt.time).utcoffset().total_seconds() == 0
    assert crack.state.attempts(snapshot.config, MEMBER) == [attempt]


def test_packet_names_the_target_score_the_worst_versions_diff_and_types(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    score = _measurement(snapshot, monkeypatch)
    packet = crack.packet(snapshot, MEMBER)
    assert packet["target"]["a"] == {"asm": f"asm/a/{MEMBER}.s", "size": 8}
    assert packet["best"]["score"] == {"a": 0.5, "b": 0.4} and packet["best"]["errors"] == []
    assert packet["diff"] == "measured diff" and packet["worst"] == "b" and packet["why"] == ["all versions: bytes"]
    assert score.call_count == 2 and score.call_args.kwargs == {"version": "b"}  # the worst version's diff
    crack.signatures.declarations.assert_called_once_with(snapshot, ["callee", "global"])
    assert packet["types"] == {"signature": "int f(void)", "declarations": ["void callee(void);", "extern int global;"]}


def test_packet_names_the_compiler_errors_of_a_candidate_that_does_not_compile(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    broken = replace(_proof(0.0), built_sha256="", missing=("compile.error: 'x' undeclared", "bytes"))
    _measurement(snapshot, monkeypatch, proofs=(broken,))
    assert crack.packet(snapshot, MEMBER)["best"]["errors"] == ["compile.error: 'x' undeclared"]


def test_packet_requires_candidate(lane, monkeypatch):
    snapshot, _ = lane
    score = Mock(side_effect=AssertionError("candidate must be checked before measuring"))
    monkeypatch.setattr(crack.judge, "score", score)
    with pytest.raises(Refusal) as caught:
        crack.packet(snapshot, MEMBER)
    (finding,) = caught.value.findings
    assert finding.key == "land.request"
    assert finding.reason == f"{MEMBER} has no candidate"
    assert finding.action == f"recipe work {MEMBER}"
    score.assert_not_called()


def test_packet_of_a_candidate_that_did_not_link_has_no_diff(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    unlinked = replace(_proof(0.0), built_sha256="")
    _measurement(snapshot, monkeypatch, proofs=(unlinked,))
    assert crack.packet(snapshot, MEMBER)["diff"] == ""


def test_packet_reads_assembly_only_from_a_version_holding_the_member(lane, monkeypatch):
    snapshot, _ = lane
    member = replace(snapshot.layout.members[MEMBER], placements=snapshot.layout.members[MEMBER].placements[1:])
    snapshot.layout = replace(snapshot.layout, members={MEMBER: member})
    _candidate(snapshot)
    _measurement(snapshot, monkeypatch, proofs=(_proof(0.4, "b"),))
    crack.world.asm_path(snapshot.config, "b", MEMBER).write_text("jal callee\n")
    crack.world.asm_path(snapshot.config, "a", MEMBER).write_text("jal wrong\n")
    assert list(crack.packet(snapshot, MEMBER)["target"]) == ["b"]
    assert "callee" in crack.signatures.declarations.call_args.args[1]


def test_packet_says_in_words_how_much_longer_the_build_is_and_how_the_frame_differs(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    facts = {"size_delta_bytes": 16, "frame_delta_bytes": -8}
    _measurement(snapshot, monkeypatch, proofs=(_proof(1.0), _proof(0.4, "b", facts)))
    assert crack.packet(snapshot, MEMBER)["why"] == [
        "b: built is 16 bytes longer than the target",
        "b: stack frame is 8 bytes larger than the target's",
        "b: bytes",
    ]


def test_a_difference_every_version_shares_prints_once_and_one_only_some_have_names_them(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    proofs = (
        replace(_proof(0.5), version="a", missing=("size 64 != 48", "3 bytes differ")),
        replace(_proof(0.4), version="b", missing=("size 64 != 48", "5 bytes differ")),
    )
    _measurement(snapshot, monkeypatch, proofs=proofs)
    assert crack.packet(snapshot, MEMBER)["why"] == [
        "all versions: size 64 != 48",
        "a: 3 bytes differ",
        "b: 5 bytes differ",
    ]


def test_a_matching_text_section_with_a_differing_rodata_is_named_not_pointed_at_a_missing_hunk(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    proof = replace(_proof(0.9996), missing=("bytes differ in .rodata at +0x10: 3f800000 != 40000000",), symptoms={})
    _measurement(snapshot, monkeypatch, proofs=(proof,), diff=())
    got = crack.packet(snapshot, MEMBER)
    assert got["diff"] == "" and got["why"] == ["all versions: bytes differ in .rodata at +0x10: 3f800000 != 40000000"]


def _diff(*pairs):
    return "".join(f"-{a:X}: {t}\n+{a:X}: {b}\n" for a, t, b in pairs)


def test_instructions_that_only_changed_places_are_called_a_reorder_with_their_offset():
    diff = _diff(
        (0x80000414, "andi $a0, $v0, 0xFF", "sll $v1, $v0, 2"), (0x80000418, "sll $v1, $v0, 2", "andi $a0, $v0, 0xFF")
    )
    assert crack.same_code(diff, 0x80000400).startswith("same instructions, different order around +0x14: try a")


def test_consistently_permuted_registers_are_called_register_allocation_not_a_shape_error():
    diff = _diff(
        (0x80000404, "addu $s0, $s1, $a0", "addu $s1, $s0, $a0"), (0x80000408, "lw $s2, 0x0($s0)", "lw $s2, 0x0($s1)")
    )
    said = crack.same_code(diff, 0x80000400)
    assert said.startswith("same code, different register allocation around +0x4") and "can score lower" in said


def test_a_real_difference_is_not_called_the_same_code():
    assert crack.same_code(_diff((0x80000404, "addu $s0, $s1, $a0", "subu $s0, $s1, $a0")), 0x80000400) == ""
    assert (
        crack.same_code(
            _diff(
                (0x80000404, "lw $s0, 0x0($a0)", "lw $s1, 0x0($a0)"),
                (0x80000408, "lw $s1, 0x4($a0)", "lw $s1, 0x4($a1)"),
            ),
            0x80000400,
        )
        == ""
    )
    assert crack.same_code("", 0x80000400) == ""


def test_a_candidate_that_does_not_compile_still_gets_the_regions_of_the_target_none_of_them_built(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    broken = replace(_proof(0.0), built_sha256="", missing=("compile.error: 'x' undeclared",))
    _measurement(snapshot, monkeypatch, proofs=(broken,))
    asm = crack.world.asm_path(snapshot.config, "a", MEMBER)
    asm.write_text(
        "/* 0 80000400 27BDFFE8 */  addiu $sp, $sp, -0x18\n/* 4 80000404 10800002 */  beqz $a0, .L1\n"
        "/* 8 80000408 00000000 */  nop\n/* C 8000040C 0C000100 */  jal callee\n/* 10 80000410 00000000 */  nop\n"
    )
    areas = crack.packet(snapshot, MEMBER)["regions"]
    assert len(areas) == 2 and {r["kind"] for r in areas} == {"not built"}
    assert areas[1]["anchors"] == "calls callee"


def test_a_compare_with_its_operands_the_other_way_round_says_to_swap_the_comparison():
    diff = _diff((0x80000404, "slt $v0, $a0, $a1", "slt $v0, $a1, $a0"))
    assert crack.same_code(diff, 0x80000400) == (
        "operand order differs around +0x4: swap the comparison (a > b against b < a)"
    )


def test_a_nop_the_target_keeps_in_a_delay_slot_is_named_and_so_is_the_reverse():
    kept = "-80000010: nop\n+80000010: addiu $a0, $a0, 1\n"
    assert crack.delay_slot(kept, 0x80000000) == (
        "the target keeps a nop that the build fills in a delay slot at +0x10 with `addiu $a0, $a0, 1`: "
        "move that work in or out of the branch"
    )
    reverse = "-80000010: addiu $a0, $a0, 1\n+80000010: nop\n"
    assert crack.delay_slot(reverse, 0x80000000).startswith("the build leaves a nop the target fills")
    assert crack.delay_slot("-80000010: lw $a0, 0($a1)\n+80000010: lw $a0, 4($a1)\n", 0x80000000) == ""


def test_a_load_delay_nop_only_the_target_has_is_a_toolchain_signature_and_a_matching_pair_is_not():
    diff = " 80000000: lw $v1, 0($a0)\n-80000004: nop\n+80000004: addiu $a1, $a1, 1\n 80000008: jr $ra\n"
    got = crack.load_delay(diff)
    assert "target has a nop after `lw $v1, 0($a0)`" in got and "layout" in got and "--note toolchain" in got
    both = " 80000000: lw $v1, 0($a0)\n 80000004: nop\n-80000008: or $a0, $v1, $zero\n+80000008: or $a0, $v1, $v1\n"
    assert crack.load_delay(both) == ""
    reverse = " 80000000: lw $v1, 0($a0)\n+80000004: nop\n-80000004: addiu $a1, $a1, 1\n"
    assert "build has a nop" in crack.load_delay(reverse)


def test_an_attempt_made_against_one_prototype_is_stale_once_a_type_item_changed_it(lane, monkeypatch):
    snapshot, _ = lane
    declared = {MEMBER: ("h.h", 1, "extern void func_80000400(void *);")}
    monkeypatch.setattr(crack.index, "get", lambda snapshot: SimpleNamespace(declarations=declared))
    monkeypatch.setattr(crack.index, "asm", lambda *a, **k: "asm")
    before = crack.stamp(snapshot, MEMBER)
    declared[MEMBER] = ("h.h", 1, "extern void func_80000400(Obj *);")
    assert crack.stamp(snapshot, MEMBER) != before


def test_crack_runs_the_permuter_and_offers_the_better_candidate_to_recipe_try(lane, monkeypatch):
    config = lane[0].config
    work = config.project.root / ".unbake/recipe/work"
    work.mkdir(parents=True)
    (work / f"{MEMBER}.c").write_text(SOURCE.decode())
    asked = []
    monkeypatch.setattr(crack.state, "tool", lambda name: "/bin/permuter")
    monkeypatch.setattr(crack.state, "settings", lambda: SimpleNamespace(permute_seconds=60))

    def call(cfg, command, *args):
        asked.append(args)
        return {"permute": {"base_score": 90, "best_score": 40, "candidate": str(work / f"{MEMBER}.cracked.c")}}

    scores = iter([(0.80, ["-a"]), (0.95, ["-x", "+y"])])

    def score(cfg, file, member):
        value, diff = next(scores)
        return SimpleNamespace(proofs=(SimpleNamespace(score=value),), diff=diff)

    monkeypatch.setattr(crack.judge, "call", call)
    monkeypatch.setattr(crack.judge, "score", score)
    got = crack.run(config, {"item": MEMBER})
    assert got["score"] == [0.80, 0.95] and got["permuter"] == [90, 40] and got["diff"] == "-x\n+y"
    assert got["next"].startswith("cp ") and got["next"].endswith(f"recipe try {MEMBER}")
    assert "--permute" in asked[0] and "60" in asked[0] and "/bin/permuter" in asked[0]
    with pytest.raises(Refusal):
        crack.run(config, {"item": "func_missing"})
