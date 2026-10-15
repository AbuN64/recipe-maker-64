"""Cracking ladder unit tests: no native tools, repositories, ROMs or network."""

import json
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import datetime
from unittest.mock import Mock

import fixture
import pytest
from unbake.contracts import (
    Config,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Recipe,
    Refusal,
    Snapshot,
    UnitSpec,
    Version,
)

from recipemaker import crack

MEMBER = "func_80000400"
SOURCE = b"int func_80000400(void) { return 0; }\n"
RECIPE = Recipe("gcc-test", ("-DTEST",), ("-O2",), (), "1" * 64)
DRAIN = {"running": False, "landed": [], "refused": []}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    project = Project(
        tmp_path,
        "fixture",
        "fixture",
        "Fixture",
        ("a", "b"),
        "a",
        "gcc-test",
        {k: [] for k in ("cppflags", "cflags", "asflags", "gnu_asflags")},
        {},
        {"a": (), "b": ()},
        {},
        {},
        "0" * 64,
    )
    host = Host(
        2,
        2,
        1 << 30,
        1 << 30,
        tmp_path / "cache",
        1 << 30,
        tmp_path / "tools",
        {},
        None,
        2,
        2,
        0.8,
        2,
        ("test", "test@example.invalid"),
        {},
        "0" * 64,
    )
    config = Config(project, host, "0" * 64)
    places = tuple(Placement(v, ".text", 0x1000, 0x1008, 0x80000400) for v in ("a", "b"))
    member = Member(MEMBER, "function", "asm", "main", places)
    group = fixture.group("main", "main", (MEMBER,))
    mapping = LayoutMap({"main": group}, {MEMBER: member}, {}, "0" * 64, (), {})
    versions = {v: Version(v, tmp_path / f"{v}.z64", "0" * 64, "", "", {}, ()) for v in ("a", "b")}
    snapshot = Snapshot(config, "0" * 40, mapping, versions, {}, "0" * 64)
    rows = []
    monkeypatch.setattr(crack.effort, "stage", Mock(side_effect=lambda name: nullcontext()))
    monkeypatch.setattr(crack.twin, "find", lambda snap, member: None)
    monkeypatch.setattr(crack.store, "rows", Mock(side_effect=lambda cfg, stream: list(rows)))
    monkeypatch.setattr(crack.store, "append", Mock(side_effect=lambda cfg, stream, row: rows.append(row)))
    monkeypatch.setattr(crack.draft.report, "split_slot", Mock(return_value=None))
    monkeypatch.setattr(crack.process, "run", Mock(side_effect=AssertionError("unmocked native invocation")))
    monkeypatch.setattr(crack.process, "git", Mock(side_effect=AssertionError("git forbidden")))
    return snapshot, rows


def _proof(score, version="a", symptoms=None, member=MEMBER):
    return replace(
        fixture.proof(
            f".unbake/recipe/work/{MEMBER}.c",
            member,
            version,
            score == 1,
            missing=() if score == 1 else ("bytes",),
            score=score,
            symptoms=symptoms,
        ),
        recipe=RECIPE.digest,
    )


def _prior(rows, **fields):
    rows.append(asdict(fixture.attempt(**{"member": MEMBER, "step": "draft", **fields})))


def _candidate(snapshot):
    path = snapshot.config.project.root / f".unbake/recipe/work/{MEMBER}.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SOURCE)
    return path


def _measurement(snapshot, monkeypatch, proofs=None):
    unit = UnitSpec(f".unbake/recipe/work/{MEMBER}.c", "c", "main", (MEMBER,), "gcc-test", {})
    monkeypatch.setattr(crack.compare, "bind", Mock(return_value=(unit, snapshot)))
    monkeypatch.setattr(crack.compare, "measure", Mock(return_value=proofs or (_proof(0.5), _proof(0.4, "b"))))
    monkeypatch.setattr(
        crack.compare,
        "measure_many",
        lambda snap, unit, variants: (crack.compare.measure(snap, unit, v, None) for v in variants),
    )  # one at a time, as consumed
    monkeypatch.setattr(
        crack.versions, "asm_path", lambda cfg, version, member: cfg.project.root / f"asm/{version}/{member}.s"
    )
    for version in ("a", "b"):
        asm = crack.versions.asm_path(snapshot.config, version, MEMBER)
        asm.parent.mkdir(parents=True)
        asm.write_text("jal callee\nlui $t0, %hi(global)\naddiu $t0, $t0, %lo(global)\njal callee\n")
    monkeypatch.setattr(crack.versions, "rom_bytes", Mock(return_value=b"target!!"))
    monkeypatch.setattr(crack.store, "cached", Mock(return_value=b"built!!!extra"))
    monkeypatch.setattr(crack.symptoms, "diff", Mock(return_value="measured diff"))
    monkeypatch.setattr(crack.signatures, "signature", Mock(return_value={MEMBER: "int f(void)"}))
    monkeypatch.setattr(
        crack.signatures,
        "declarations",
        Mock(return_value={"callee": "void callee(void);", "global": "extern int global;"}),
    )
    return unit


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


def test_feedback_records_attempt(lane):
    snapshot, rows = lane
    proofs = [_proof(0.8), _proof(0.4, "b")]
    attempt = crack.feedback(snapshot, MEMBER, "draft", proofs, "retain this technique", "text")
    assert rows == [json_roundtrip(asdict(attempt))]
    crack.store.append.assert_called_once_with(snapshot.config, f"recipe-attempts/{MEMBER}", rows[0])
    assert attempt.note == "retain this technique"
    assert datetime.fromisoformat(attempt.time).utcoffset().total_seconds() == 0
    assert crack.state.attempts(snapshot.config, MEMBER) == [attempt]


def json_roundtrip(body):

    return json.loads(json.dumps(body))


def test_packet_names_the_target_score_diff_and_types(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    _measurement(snapshot, monkeypatch)
    packet = crack.packet(snapshot, MEMBER)
    assert packet["target"]["a"] == {"asm": f"asm/a/{MEMBER}.s", "size": 8}
    assert packet["best"]["score"] == {"a": 0.5, "b": 0.4} and packet["best"]["errors"] == []
    assert packet["diff"] == "measured diff" and packet["why"] == ["all versions: bytes"]
    crack.symptoms.diff.assert_called_once_with(b"built!!!extra", b"target!!", 0x80000400)
    crack.versions.rom_bytes.assert_called_once_with(snapshot.versions["b"], 0x1000, 0x1008)
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
    bind = Mock(side_effect=AssertionError("candidate must be checked before binding"))
    monkeypatch.setattr(crack.compare, "bind", bind)
    with pytest.raises(Refusal) as caught:
        crack.packet(snapshot, MEMBER)
    (finding,) = caught.value.findings
    assert finding.key == "land.request"
    assert finding.reason == f"{MEMBER} has no candidate"
    assert finding.action == f"recipe work {MEMBER}"
    bind.assert_not_called()


def test_packet_of_a_candidate_that_did_not_link_has_no_diff(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    unlinked = replace(_proof(0.0), built_sha256="")
    _measurement(snapshot, monkeypatch, proofs=(unlinked,))
    crack.store.cached.side_effect = AssertionError("an unlinked candidate has no built bytes to read")
    assert crack.packet(snapshot, MEMBER)["diff"] == ""
    crack.symptoms.diff.assert_not_called()


def test_packet_reads_assembly_only_from_a_version_holding_the_member(lane, monkeypatch):
    snapshot, _ = lane
    member = replace(snapshot.layout.members[MEMBER], placements=snapshot.layout.members[MEMBER].placements[1:])
    snapshot = replace(snapshot, layout=replace(snapshot.layout, members={MEMBER: member}))
    _candidate(snapshot)
    _measurement(snapshot, monkeypatch, proofs=(_proof(0.4, "b"),))
    asm = crack.versions.asm_path(snapshot.config, "b", MEMBER)
    asm.write_text("jal callee\n")
    crack.versions.asm_path(snapshot.config, "a", MEMBER).write_text("jal wrong\n")
    assert list(crack.packet(snapshot, MEMBER)["target"]) == ["b"]
    assert "callee" in crack.signatures.declarations.call_args.args[1]


SOURCE_TEXT = '#include "a.h"\nint other(void) { return 1; }\nint func_80000400(void) {\n    return 0;\n}\n'
EXPANDED = "typedef int s32;\nint other(void) { return 1; }\nint func_80000400(void) {\n    return 0;\n}\n"


def permuter(lane, monkeypatch, tmp_path, run):
    """Everything crack.permute touches, faked; `run(argv, work)` plays the permuter."""
    snapshot, _ = lane
    unit = UnitSpec("src/f.c", "c", "main", (MEMBER,), "gcc-test", {})
    snapshot = replace(snapshot, overlays={"src/f.c": SOURCE_TEXT.encode()})
    work = tmp_path / "scratch"
    work.mkdir()
    seen = {}

    def view_get(snap, spec, version, recipe, *, lines=True):
        seen["lines"] = lines
        return Mock(text=EXPANDED)

    def process_run(name, argv, cwd, **kwargs):
        seen["runs"] = seen.get("runs", 0) + 1
        seen["base"] = (work / "base.c").read_text()
        return run(argv, work)

    monkeypatch.setattr(crack.view, "get", view_get)
    monkeypatch.setattr(crack.store, "work", lambda cfg: nullcontext(work))
    assembler = Mock()
    assembler.return_value.assemble.side_effect = lambda asm, recipe, include, out: out.write_bytes(seen["object"])
    seen["object"] = b"object"
    seen["script"] = "#!/bin/sh\n"
    seen["memo"] = {}

    def cached(cfg, kind, key, produce):
        assert kind == "recipe.permute"
        if key not in seen["memo"]:
            seen["memo"][key] = produce()
        return seen["memo"][key]

    monkeypatch.setattr(crack.store, "cached", cached)
    monkeypatch.setattr(crack.adapters, "assembler", assembler)
    monkeypatch.setattr(crack.adapters, "script", lambda cfg, recipe, version: seen["script"])
    monkeypatch.setattr(crack.versions, "asm_path", lambda cfg, v, m: work / "t.s")
    monkeypatch.setattr(crack.state, "tool", lambda name: work / "permuter")
    monkeypatch.setattr(crack.process, "run", process_run)
    return snapshot, unit, seen


def test_the_permuter_gets_the_unit_preprocessed_not_the_raw_source(lane, monkeypatch, tmp_path):
    def run(argv, work):
        return fixture.native_result(argv, None)  # killed at the deadline, having found nothing

    snapshot, unit, seen = permuter(lane, monkeypatch, tmp_path, run)
    assert crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0) is None
    assert "func_80000400" in seen["base"] and "other" not in seen["base"] and seen["lines"] is False
    assert "#include" not in seen["base"]


def test_only_the_function_comes_back_spliced_into_the_unit_as_it_was_written(lane, monkeypatch, tmp_path):
    def run(argv, work):
        out = work / "output-12-1"
        out.mkdir()
        (out / "source.c").write_text(EXPANDED.replace("return 0;", "return 2 - 2;"))
        return fixture.native_result(argv, None)

    snapshot, unit, _ = permuter(lane, monkeypatch, tmp_path, run)
    got = crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0).decode()
    assert got == SOURCE_TEXT.replace("return 0;", "return 2 - 2;")  # the include line stays, nothing is expanded


def test_a_permuter_that_exits_with_an_error_is_refused_with_its_last_line(lane, monkeypatch, tmp_path):
    def run(argv, work):
        return fixture.native_result(
            argv, 1, b"Loading...\n", b"Traceback (most recent call last):\nCalledProcessError: cpp\n"
        )

    snapshot, unit, _ = permuter(lane, monkeypatch, tmp_path, run)
    with pytest.raises(Refusal) as caught:
        crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    assert caught.value.findings[0].reason == "CalledProcessError: cpp"


def test_a_permuter_that_exits_zero_having_found_nothing_is_no_gain_not_a_failure(lane, monkeypatch, tmp_path):
    snapshot, unit, _ = permuter(lane, monkeypatch, tmp_path, lambda argv, work: fixture.native_result(argv, 0))
    assert crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0) is None


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
    monkeypatch.setattr(crack.symptoms, "merge", lambda _: {})
    assert crack.packet(snapshot, MEMBER)["why"] == [
        "all versions: size 64 != 48",
        "a: 3 bytes differ",
        "b: 5 bytes differ",
    ]


def test_a_matching_text_section_with_a_differing_rodata_is_named_not_pointed_at_a_missing_hunk(lane, monkeypatch):
    snapshot, _ = lane
    _candidate(snapshot)
    proof = replace(_proof(0.9996), missing=("bytes differ in .rodata at +0x10: 3f800000 != 40000000",), symptoms={})
    _measurement(snapshot, monkeypatch, proofs=(proof,))
    monkeypatch.setattr(crack.symptoms, "diff", lambda *a: "")
    got = crack.packet(snapshot, MEMBER)
    assert got["diff"] == "" and got["why"] == ["all versions: bytes differ in .rodata at +0x10: 3f800000 != 40000000"]


def test_the_packet_names_the_twin_and_what_differs_between_its_version_and_this_one(lane, monkeypatch):
    from types import SimpleNamespace

    snapshot, _ = lane
    _candidate(snapshot)
    _measurement(snapshot, monkeypatch)
    found = SimpleNamespace(counterpart=MEMBER, unit="src/b.c", version="a", other_version="b")
    monkeypatch.setattr(crack.twin, "find", lambda snap, member: found)
    monkeypatch.setattr(crack.layout, "unit_of", lambda snap, member: UnitSpec("src/b.c", "c", "main", (), "gcc-b", {}))
    rows = {"gcc-test": ("-O2",), "gcc-b": ("-O1",)}
    monkeypatch.setattr(
        crack.recipes,
        "resolve",
        lambda cfg, unit, o: SimpleNamespace(
            toolchain=unit.toolchain, cppflags=(), cflags=rows[unit.toolchain], asflags=()
        ),
    )
    monkeypatch.setattr(crack.symptoms, "diff", Mock(return_value="-lw a\n+lw b"))
    macros = {"a": ("-DVERSION_A",), "b": ("-DVERSION_B",)}
    project = replace(snapshot.config.project, version_macros=macros)
    snapshot = replace(snapshot, config=replace(snapshot.config, project=project))
    got = crack.packet(snapshot, MEMBER)["twin"]
    assert (got["function"], got["file"], got["version"]) == (MEMBER, "src/b.c", "b") and got["diff"] == "-lw a\n+lw b"
    assert got["flags"]["cflags"] == [("-O2",), ("-O1",)] and got["flags"]["toolchain"] == ["gcc-test", "gcc-b"]


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
    asm = crack.versions.asm_path(snapshot.config, "a", MEMBER)
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


def test_the_permuter_gets_its_j_from_the_held_share_of_the_host_budget(lane, monkeypatch, tmp_path):
    held = []

    @contextmanager
    def cores(host, work=0):
        held.append(host)
        yield 3

    def run(argv, work):
        held.append(argv[argv.index("-j") + 1])
        return fixture.native_result(argv, None)

    snapshot, unit, _ = permuter(lane, monkeypatch, tmp_path, run)
    monkeypatch.setattr(crack.pool, "cores", cores)
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    assert held == [snapshot.config.host, "3"]


def test_the_same_candidate_permuted_twice_runs_the_permuter_once(lane, monkeypatch, tmp_path):
    snapshot, unit, seen = permuter(lane, monkeypatch, tmp_path, lambda argv, work: fixture.native_result(argv, None))
    for _ in range(2):
        assert crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0) is None
    assert seen["runs"] == 1


def test_a_found_variant_is_stored_and_returned_without_a_second_run(lane, monkeypatch, tmp_path):
    def run(argv, work):
        out = work / "output-12-1"
        out.mkdir()
        (out / "source.c").write_text(EXPANDED.replace("return 0;", "return 2 - 2;"))
        return fixture.native_result(argv, None)

    snapshot, unit, seen = permuter(lane, monkeypatch, tmp_path, run)
    first = crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    for entry in (tmp_path / "scratch").glob("output-*"):  # the work directory of the second call starts empty
        for child in entry.iterdir():
            child.unlink()
        entry.rmdir()
    assert crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0) == first and seen["runs"] == 1


def test_a_changed_script_object_or_budget_runs_the_permuter_again(lane, monkeypatch, tmp_path):
    snapshot, unit, seen = permuter(lane, monkeypatch, tmp_path, lambda argv, work: fixture.native_result(argv, None))
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    seen["script"] = "#!/bin/sh\n# other flags\n"
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    seen["object"] = b"other object"
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 6.0)
    assert seen["runs"] == 4


def test_a_changed_base_runs_the_permuter_again(lane, monkeypatch, tmp_path):
    snapshot, unit, seen = permuter(lane, monkeypatch, tmp_path, lambda argv, work: fixture.native_result(argv, None))
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    monkeypatch.setattr(crack.view, "get", lambda *a, **k: Mock(text=EXPANDED.replace("return 0;", "return 1;")))
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    assert seen["runs"] == 2


def test_a_permuter_that_failed_is_not_remembered(lane, monkeypatch, tmp_path):
    snapshot, unit, seen = permuter(lane, monkeypatch, tmp_path, lambda argv, work: fixture.native_result(argv, 1))
    for _ in range(2):
        with pytest.raises(Refusal):
            crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    assert seen["runs"] == 2


def test_a_candidate_whose_diff_is_not_an_order_or_register_miss_is_refused_with_the_reason(lane, monkeypatch):
    snapshot, _ = lane
    packet = {"permutable": False, "regions": [{"kind": "match"}, {"kind": "missing call"}]}
    monkeypatch.setattr(crack, "packet", lambda snap, member: packet)
    assert crack.permutable(snapshot, MEMBER).startswith("diff class missing call; only order or register")
    packet["permutable"] = True
    assert crack.permutable(snapshot, MEMBER) == ""


def test_the_permuter_holds_one_slot_per_worker_it_runs_never_the_whole_share(lane, monkeypatch, tmp_path):
    asked = []

    @contextmanager
    def cores(host, work=0):
        asked.append(work)
        yield min(work or 11, 11)

    snapshot, unit, _ = permuter(lane, monkeypatch, tmp_path, lambda argv, work: fixture.native_result(argv, None))
    monkeypatch.setattr(crack.pool, "cores", cores)
    crack.permute(snapshot.config, snapshot, unit, MEMBER, RECIPE, 5.0)
    assert asked == [crack.PERMUTERS] and crack.PERMUTERS < 11
