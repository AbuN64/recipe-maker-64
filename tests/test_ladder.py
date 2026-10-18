"""The ladder, counted in work: measures, pool calls, rungs run and submissions. Never time."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import fixture
import pytest
from fakes import unit as make_unit
from unbake.contracts import Finding, Refusal

from recipemaker import cparse, crack, host, index, ladder, ledger

pytestmark = pytest.mark.usefixtures("toolchains")


def run(fakes, member="func_a", **kwargs):
    kwargs.setdefault("submit", True)
    return ladder.run(fakes.config, member, **kwargs)


def names(outcome):
    return [r.name for r in outcome.rungs]


def stage_jobs(fakes, stage):
    return [n for name, n in fakes.pool_jobs if name == stage]


def edited(fakes):
    path = ladder.candidate_path(fakes.config, "func_a")
    path.parent.mkdir(parents=True)
    path.write_text("int func_a(void) { return 7; }\n")


def test_a_function_without_a_clone_runs_clone_then_draft(fakes):
    out = run(fakes)
    assert names(out) == ["clone", "draft"]
    assert out.rungs[0].result == "none" and not out.exact and out.score == {"a": 0.5, "b": 0.5}


def test_each_rung_costs_what_the_design_says(fakes):
    run(fakes)
    assert fakes.calls["judge.score"] == 1  # the draft, measured once
    assert fakes.calls["host.run"] == 2  # m2c both ways
    assert stage_jobs(fakes, "draft.m2c") == [2] and fakes.calls["judge.score_many"] == 1


def test_a_data_item_runs_the_data_rung_alone_with_one_measure_for_the_data(fakes):
    out = run(fakes, "data_d")
    assert names(out) == ["data"]
    assert fakes.calls["judge.score"] == 1 and fakes.calls["host.run"] == 0


def test_an_exact_twin_stops_the_ladder_after_one_measure(fakes, monkeypatch):
    unit = make_unit("src/b.c", ("func_b",))
    fakes.with_units({"src/b.c": unit})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": b"int func_b(void) {\n    return 0;\n}\n"})
    fakes.shapes = {"s": ("func_a", "func_b")}
    fakes.scores.append(1.0)
    out = run(fakes)
    assert names(out) == ["clone"] and out.exact and out.submitted == "id-func_a"
    assert fakes.calls["judge.score"] == 1
    assert stage_jobs(fakes, "draft.m2c") == [] and fakes.calls["judge.score_many"] == 0
    assert out.drain["landed"][0]["id"] == "id-func_a"
    assert "func_a(" in (fakes.root / ".unbake/recipe/work/func_a.c").read_text()


def test_an_exact_draft_meets_no_proposal(fakes):
    fakes.scores.append(1.0)
    out = run(fakes)
    assert names(out) == ["clone", "draft"] and out.exact
    assert fakes.calls["judge.score_many"] == 1  # the draft's compile screen, no proposal batch


def test_no_submit_lands_nothing_and_drains_nothing(fakes):
    fakes.scores.append(1.0)
    out = run(fakes, submit=False)
    assert out.exact and out.submitted is None
    assert fakes.calls["judge.submit"] == 0 and fakes.calls["judge.submit"] == 0


def test_a_gain_below_the_fuzzy_minimum_is_not_submitted_and_a_real_gain_is(fakes):
    fakes.default_score = 0.005
    assert run(fakes).submitted is None and fakes.calls["judge.submit"] == 0
    fakes.default_score = 0.6
    assert run(fakes, "func_b").submitted == "id-func_b"


@pytest.mark.parametrize("member", ["unknown", "func_a"])
def test_the_guard_refuses_an_unknown_member_and_one_whose_source_exists_before_any_work(fakes, member):
    unit = make_unit("src/a.c", ("func_a",))
    fakes.with_units({"src/a.c": unit})
    with pytest.raises(Refusal) as caught:
        run(fakes, member)
    assert caught.value.findings[0].key == "land.request"
    assert fakes.calls["judge.score"] == 0 and fakes.calls["host.run"] == 0


def test_a_fragment_is_refused_before_any_work(fakes):
    fakes.split["func_a"] = "func_b"
    with pytest.raises(Refusal) as caught:
        run(fakes)
    assert caught.value.findings[0].key == "member.split-delay-slot"
    assert fakes.calls["judge.score"] == 0 and stage_jobs(fakes, "draft.m2c") == []


def test_an_edited_candidate_is_measured_and_never_overwritten(fakes):
    path = ladder.candidate_path(fakes.config, "func_a")
    path.parent.mkdir(parents=True)
    path.write_text("int func_a(void) { return 7; }\n")
    out = run(fakes)
    assert path.read_text() == "int func_a(void) { return 7; }\n"
    assert fakes.calls["host.run"] == 0 and fakes.calls["judge.score_many"] == 0
    assert names(out) == ["existing"] and fakes.calls["judge.score"] == 1


def test_the_ledger_pass_resolves_what_the_declared_type_has_and_says_how_many(fakes, monkeypatch):
    fakes.m2c_text = (
        "void func_a(Actor *arg0) {{\n    x = M2C_FIELD(arg0, s32 *, 4);\n    y = M2C_FIELD(arg0, s32 *, 0x80);\n}}\n"
    )
    view = cparse.Layout("Actor", "include/actor.h", (cparse.Field("hp", 4, 4, 1, "i"),))
    found = ledger.identities([view])
    monkeypatch.setattr(
        index,
        "get",
        lambda snap: SimpleNamespace(
            declarations={}, identities=tuple(found), callers={}, shapes={}, extents={}, globals={}, addresses={}
        ),
    )
    out = run(fakes)
    text = (fakes.root / ".unbake/recipe/work/func_a.c").read_text()
    assert "arg0->hp" in text and text.count("M2C_FIELD(arg0") == 1  # the left use
    assert out.auto_fixes[0] == "resolved 1 of 2 M2C_FIELD" and out.auto_fixes[1].startswith("held: 1 refusable")
    assert [(u.offset, u.type) for u in out.left] == [(0x80, "Actor")]


def test_a_draft_that_does_not_compile_is_reported_as_failed(fakes):
    fakes.compile_error = "src/f.c:3: syntax error"
    out = run(fakes, submit=False)
    assert out.rungs[1].name == "draft" and out.rungs[1].result == "failed"


def test_one_item_is_measured_without_any_thread(fakes):
    run(fakes)
    assert fakes.peak == 1


def test_a_measure_that_is_refused_skips_that_rung_and_the_ladder_goes_on(fakes, monkeypatch):
    def measure(config, file, member, extras=None, *, version=""):
        raise Refusal(Finding("land.request", "boom"))

    monkeypatch.setattr(ladder.judge, "score", measure)
    out = run(fakes, submit=False)
    assert [r.result for r in out.rungs if r.name == "draft"] == ["skipped"]


@pytest.mark.parametrize("key", ["member.split-delay-slot", "recipe.budget"])
def test_a_refusal_about_the_whole_request_still_ends_the_ladder(fakes, monkeypatch, key):
    from unbake.contracts import Finding

    def refuse(*args):
        raise Refusal(Finding(key, "no"))

    monkeypatch.setattr(ladder.draft, "drafted", refuse)
    with pytest.raises(Refusal):
        run(fakes, submit=False)


def test_a_type_fix_that_stops_the_draft_compiling_is_dropped_and_the_plain_draft_is_kept(fakes, monkeypatch):
    from dataclasses import replace as swap

    fakes.m2c_text = "void func_a(Actor *arg0) {{\n    x = M2C_FIELD(arg0, s32 *, 4);\n}}\n"
    view = cparse.Layout("Actor", "include/actor.h", (cparse.Field("hp", 4, 4, 1, "i"),))
    monkeypatch.setattr(
        index,
        "get",
        lambda s: SimpleNamespace(
            declarations={},
            identities=tuple(ledger.identities([view])),
            callers={},
            shapes={},
            extents={},
            globals={},
            addresses={},
        ),
    )
    real = fakes.score

    def measure(config, file, member, extras=None, *, version=""):
        text = (fakes.root / ".unbake/recipe/work/func_a.c").read_text()
        fakes.compile_error = "arg0->hp is not a member" if "arg0->hp" in text else ""
        return real(config, file, member, extras)

    monkeypatch.setattr(ladder.judge, "score", measure)
    monkeypatch.setattr(ladder.rungs, "RUNGS", ())  # the fakes bind no proposal file: only the draft step is shown
    out = run(fakes, submit=False)
    text = (fakes.root / ".unbake/recipe/work/func_a.c").read_text()
    assert "M2C_FIELD(arg0, s32 *, 4)" in text and "arg0->hp" not in text
    assert out.auto_fixes == (
        "the 1 type fixes were dropped: with them the draft did not compile "
        "(compile.error: arg0->hp is not a member); fixed lines: x = arg0->hp;",
    )
    assert [r.result for r in out.rungs if r.name == "draft"] == ["compiles"] and swap


def test_the_m2c_macro_header_takes_the_arguments_m2c_gives_its_no_argument_macros():
    from recipemaker import resource

    header = resource("tables/m2c_macros.h").decode()
    assert "#define M2C_BREAK(...)" in header and "#define M2C_SYNC(...)" in header


def test_a_twin_candidate_carries_on_when_m2c_cannot_draft_the_function(fakes):
    unit = make_unit("src/b.c", ("func_b",))
    fakes.with_units({"src/b.c": unit})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": b"int func_b(void) {\n    return 0;\n}\n"})
    fakes.shapes = {"s": ("func_a", "func_b")}
    fakes.m2c_fail = "/*\nDecompilation failure in function f:\n\nCannot find branch target\n*/"
    out = run(fakes, submit=False)
    assert [(r.name, r.result) for r in out.rungs][:3] == [
        ("clone", "better"),
        ("draft", "skipped"),
    ]
    assert "branch target" in out.rungs[1].note and out.score == {"a": 0.5, "b": 0.5}


def test_a_draft_without_m2c_text_is_written_and_landed_without_the_macro_header(fakes):
    fakes.scores.append(1.0)
    out = run(fakes)
    assert "M2C_" not in (fakes.root / ".unbake/recipe/work/func_a.c").read_text() and out.submitted == "id-func_a"


def test_a_draft_that_still_uses_m2c_text_has_no_macro_block_is_measured_with_one_and_is_not_submitted(fakes):
    fakes.m2c_text = "void {name}(void) {{\n    M2C_ERROR(1);\n}}\n"
    fakes.scores.append(1.0)
    out = run(fakes)
    text = (fakes.root / ".unbake/recipe/work/func_a.c").read_text()
    assert "M2C_ERROR" in text and "#define" not in text and "M2C_UNK" not in text
    assert "#define M2C_ERROR" in (fakes.root / ".unbake/recipe/work/func_a/measured.c").read_text()
    assert out.exact and out.submitted is None and fakes.landed == []


def test_a_twin_that_compiles_stays_the_candidate_when_the_draft_does_not_beat_it(fakes, monkeypatch):
    fakes.with_units({"src/b.c": make_unit("src/b.c", ("func_b",))})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": b"int func_b(void) {\n    return 0;\n}\n"})
    fakes.shapes = {"s": ("func_a", "func_b")}
    fakes.scores.extend([0.6, 0.3])
    out = run(fakes, submit=False)
    assert "return 0;" in (fakes.root / ".unbake/recipe/work/func_a.c").read_text()
    assert out.score == {"a": 0.6, "b": 0.6}


def test_a_fuzzy_landing_is_resumed_instead_of_redrafted_and_the_brief_says_so(fakes):
    fakes.layout = replace(fakes.layout, fuzzy={"func_a": "src/fuzzy/func_a.c"})
    fakes.fuzzy = {"func_a": {"a": 0.967, "b": 0.967}}
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/fuzzy/func_a.c": b"int func_a(void) { return 5; }\n"})
    fakes.scores.append(0.967)
    out = run(fakes, submit=False)
    assert names(out) == ["resume"] and out.auto_fixes == ("resumed from src/fuzzy/func_a.c at 96.7%",)
    assert (fakes.root / ".unbake/recipe/work/func_a.c").read_text() == "int func_a(void) { return 5; }\n"
    assert fakes.calls["host.run"] == 0 and fakes.calls["judge.score_many"] == 0


def test_a_two_file_change_set_is_submitted_once_with_the_header_as_an_extra_and_lands_together(fakes):
    unit = make_unit("src/a.c", ("func_a",))
    edits = {"src/a.c": b"int func_a(void) { return 0; }\n", "include/a.h": b"extern int gX;\n"}
    fakes.scores.append(1.0)
    proofs, drain = ladder.land_set(fakes.config, fakes.snapshot, unit, edits)
    assert all(p.exact for p in proofs) and fakes.calls["judge.submit"] == 1
    assert fakes.extras == {"include/a.h": b"extern int gX;\n"}  # the unit's own file travels as the source
    assert [row["member"] for row in drain["landed"]] == ["func_a"] and drain["refused"] == []


def test_a_refusal_of_the_change_set_refuses_both_files_and_a_set_that_is_not_exact_is_never_submitted(
    fakes, monkeypatch
):
    unit = make_unit("src/a.c", ("func_a",))
    edits = {"src/a.c": b"int func_a(void) { return 0; }\n", "include/a.h": b"extern int gX;\n"}
    refused = {"id": "id-func_a", "member": "func_a", "findings": [{"reason": "does not build", "missing": []}]}
    monkeypatch.setattr(fakes, "drain", lambda config: {"running": False, "landed": [], "refused": [refused]})
    fakes.scores.append(1.0)
    _, drain = ladder.land_set(fakes.config, fakes.snapshot, unit, edits)
    assert drain["landed"] == [] and drain["refused"] == [refused] and fakes.calls["judge.submit"] == 1
    fakes.scores.append(0.5)
    proofs, drain = ladder.land_set(fakes.config, fakes.snapshot, unit, edits)
    assert not all(p.exact for p in proofs) and drain == {} and fakes.calls["judge.submit"] == 1


def attempted(fakes, score, text, step="permuter", stale=False):
    stamp = "old code" if stale else crack.stamp(fakes.snapshot, "func_a")
    row = asdict(fixture.attempt(member="func_a", step=step, score=score, text=text, stamp=stamp))
    host.append(fakes.config, "recipe-attempts/func_a", row)


def fuzzy_of(fakes, score, text):
    fakes.layout = replace(fakes.layout, fuzzy={"func_a": "src/fuzzy/func_a.c"})
    fakes.fuzzy = {"func_a": {"a": score, "b": score}}
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/fuzzy/func_a.c": text.encode()})


def work_file(fakes):
    return (fakes.root / ".unbake/recipe/work/func_a.c").read_text()


def test_resume_takes_the_best_measured_attempt_over_src_fuzzy_when_it_scored_higher(fakes):
    fuzzy_of(fakes, 0.774, "int func_a(void) { return 1; }\n")
    attempted(fakes, 0.5, "int func_a(void) { return 2; }\n")
    attempted(fakes, 1.0, "int func_a(void) { return 3; }\n")
    fakes.scores.append(1.0)
    out = run(fakes, submit=False)
    assert work_file(fakes) == "int func_a(void) { return 3; }\n"
    assert out.auto_fixes == ("resumed from the permuter attempt at 100.0%",)


def test_a_resumed_attempt_is_cleaned_as_drafts_are_now_no_m2c_unknown_type_left(fakes):
    attempted(fakes, 1.0, "M2C_UNK g();\nint func_a(void) { g(); return 3; }\n")
    fakes.scores.append(1.0)
    run(fakes, submit=False)
    assert work_file(fakes) == "s32 g();\nint func_a(void) { g(); return 3; }\n"


def test_an_existing_candidate_is_measured_without_m2c_unknown_types(fakes):
    path = fakes.root / ".unbake/recipe/work/func_a.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("M2C_UNK g();\nint func_a(void) { g(); return 3; }\n")
    fakes.scores.append(1.0)
    out = run(fakes, submit=False)
    assert work_file(fakes) == "s32 g();\nint func_a(void) { g(); return 3; }\n"
    assert "spelled m2c's unknown types and per-function callee records as project types" in out.auto_fixes


def test_a_fuzzy_candidate_that_does_not_land_says_what_holds_it(fakes):
    path = fakes.root / ".unbake/recipe/work/func_a.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("int func_a(void *a) { return M2C_FIELD(a, s32 *, 4); }\n")
    fakes.scores.append(0.5)
    out = run(fakes)
    assert out.submitted is None and any(
        f.startswith("held: 1 refusable lines (M2C_ text on line 1)") for f in out.auto_fixes
    )


def test_resume_ignores_an_attempt_made_by_other_recipe_code(fakes):
    fuzzy_of(fakes, 0.774, "int func_a(void) { return 1; }\n")
    attempted(fakes, 1.0, "int func_a(void) { return 3; }\n", stale=True)
    fakes.scores.append(0.774)
    out = run(fakes, submit=False)
    assert out.auto_fixes == ("resumed from src/fuzzy/func_a.c at 77.4%",)


def test_resume_takes_src_fuzzy_when_no_attempt_scored_higher(fakes):
    fuzzy_of(fakes, 0.774, "int func_a(void) { return 1; }\n")
    attempted(fakes, 0.774, "int func_a(void) { return 2; }\n")
    fakes.scores.append(0.774)
    out = run(fakes, submit=False)
    assert work_file(fakes) == "int func_a(void) { return 1; }\n"
    assert out.auto_fixes == ("resumed from src/fuzzy/func_a.c at 77.4%",)


def test_an_existing_work_file_is_replaced_by_a_better_attempt_and_kept_against_a_worse_one(fakes):
    edited(fakes)
    attempted(fakes, 0.9, "int func_a(void) { return 9; }\n")
    fakes.scores.extend([0.5, 0.9])
    run(fakes, submit=False)
    assert work_file(fakes) == "int func_a(void) { return 9; }\n"


def test_a_later_worse_rung_never_overwrites_the_best_candidate_file(fakes):
    edited(fakes)
    attempted(fakes, 0.95, "int func_a(void) { return 9; }\n")
    fakes.scores.extend([0.7, 0.3])  # the work file measures 0.7, the resumed attempt measures worse than it claimed
    out = run(fakes, submit=False)
    assert work_file(fakes) == "int func_a(void) { return 7; }\n" and out.score == {"a": 0.7, "b": 0.7}


def test_every_rungs_proposals_are_screened_in_one_call(fakes, monkeypatch):
    from recipemaker import rungs

    found = [
        rungs.Proposal("first", "void func_a(void) {\n}\n", {"include/h.h": "int h;\n"}, ("first: typed h",)),
        rungs.Proposal("second", "void func_a(void) {\n    int x;\n}\n"),
    ]
    monkeypatch.setattr(
        rungs, "RUNGS", (lambda snapshot, member, text: found[:1], lambda snapshot, member, text: found[1:])
    )
    out = run(fakes, submit=False)
    assert fakes.calls["judge.score_many"] == 2  # the draft's compile screen, then both proposals in one call
    assert {"first", "second"} <= set(names(out))


def test_the_commit_of_a_landed_row_is_its_commit_or_else_its_id_and_a_member_picks_its_own_rows():
    rows = [
        {"member": "a", "operation": "publish", "id": "id-a", "commit": "c1"},
        {"member": "b", "operation": "fuzzy", "id": "id-b"},
    ]
    assert ladder.commits({"landed": rows}) == {"publish": "c1", "fuzzy": "id-b"}
    assert ladder.commits({"landed": rows}, "b") == {"fuzzy": "id-b"}
    assert ladder.commits(None) == {}


def test_resume_never_takes_an_attempt_that_failed_to_compile_so_the_draft_runs_again(fakes):
    attempted(fakes, 0.0, "int func_a(void) { return 3; }\n")
    assert ladder._known(fakes.snapshot, ladder._Item("func_a", ladder.candidate_path(fakes.config, "func_a"))) is None
    attempted(fakes, 0.2, "int func_a(void) { return 4; }\n")
    found = ladder._known(fakes.snapshot, ladder._Item("func_a", ladder.candidate_path(fakes.config, "func_a")))
    assert found is not None and found[1] == 0.2


def test_a_draft_m2c_printed_a_project_struct_for_compiles_with_the_fold_and_lands_the_header_before_the_fuzzy(
    fakes, monkeypatch
):
    header = "struct Thing;\ntypedef struct Thing Thing;\nstruct Thing {\n    char pad[8];\n    s32 named;\n};\n"
    (fakes.root / "include").mkdir()
    (fakes.root / "include/a.h").write_text(header)
    (fakes.root / "src").mkdir(exist_ok=True)
    (fakes.root / "src/b.c").write_text('#include "a.h"\nint func_b(void) { return 0; }\n')
    fakes.with_units({"src/b.c": make_unit("src/b.c", ("func_b",))})
    printed = "typedef struct Thing {{\n    s32 unk0;\n    s32 unk4;\n    s32 named;\n}} Thing;\n\n"
    fakes.m2c_text = printed + "void {name}(Thing *a) {{\n    a->unk4 = 1;\n}}\n"
    monkeypatch.setattr(ladder.draft.headers, "sources", lambda snapshot, *a: ["include/a.h"])
    compiled = []
    real = ladder.judge.score_many
    monkeypatch.setattr(
        ladder.judge, "score_many", lambda c, rows: compiled.append(rows[0][2].get("include/a.h")) or real(c, rows)
    )
    fakes.scores.extend([0.5, 1.0])  # the draft is fuzzy, the header change set alone is exact
    out = run(fakes)
    folded = compiled[0].decode()
    assert "    s32 unk0;\n    s32 unk4;\n    s32 named;\n};" in folded and "pad[8]" not in folded
    assert "struct Thing {" not in work_file(fakes) and "a->unk4 = 1;" in work_file(fakes)
    assert fakes.landed == ["func_b", "func_a"]  # the header under the unit that includes it, then the fuzzy file
    assert fakes.extras == {} and out.submitted == "id-func_a"  # the fuzzy file travels alone
    assert any("landed the folded header fields first" in f for f in out.auto_fixes)


def test_a_fuzzy_file_whose_folded_header_does_not_prove_exact_is_held_and_says_why(fakes, monkeypatch):
    header = "struct Thing;\ntypedef struct Thing Thing;\nstruct Thing {\n    char pad[8];\n    s32 named;\n};\n"
    (fakes.root / "include").mkdir()
    (fakes.root / "include/a.h").write_text(header)
    (fakes.root / "src").mkdir(exist_ok=True)
    (fakes.root / "src/b.c").write_text('#include "a.h"\nint func_b(void) { return 0; }\n')
    fakes.with_units({"src/b.c": make_unit("src/b.c", ("func_b",))})
    printed = "typedef struct Thing {{\n    s32 unk0;\n    s32 unk4;\n    s32 named;\n}} Thing;\n\n"
    fakes.m2c_text = printed + "void {name}(Thing *a) {{\n    a->unk4 = 1;\n}}\n"
    monkeypatch.setattr(ladder.draft.headers, "sources", lambda snapshot, *a: ["include/a.h"])
    compiled = []
    real = ladder.judge.score_many
    monkeypatch.setattr(
        ladder.judge, "score_many", lambda c, rows: compiled.append(rows[0][2].get("include/a.h")) or real(c, rows)
    )
    fakes.scores.extend([0.5, 0.5])  # the header change set is not exact either
    out = run(fakes)
    assert fakes.landed == [] and out.submitted is None
    assert any(
        f.startswith("held the fuzzy file: the folded header fields did not land (src/b.c is not exact")
        for f in out.auto_fixes
    )


def test_a_fuzzy_draft_whose_header_fold_is_not_exact_lands_nothing(fakes, monkeypatch):
    (fakes.root / "include").mkdir()
    (fakes.root / "include/a.h").write_text("struct Thing {\n    char pad[8];\n};\n")
    (fakes.root / "src").mkdir(exist_ok=True)
    (fakes.root / "src/b.c").write_text('#include "a.h"\nint func_b(void) { return 0; }\n')
    fakes.with_units({"src/b.c": make_unit("src/b.c", ("func_b",))})
    fakes.m2c_text = "struct Thing {{\n    s32 unk0;\n    s32 unk4;\n}};\n\nvoid {name}(void) {{\n}}\n"
    monkeypatch.setattr(ladder.draft.headers, "sources", lambda snapshot, *a: ["include/a.h"])
    fakes.scores.extend([0.5, 0.5])
    out = run(fakes)
    assert out.submitted is None and fakes.calls["judge.submit"] == 0
    fakes.scores.extend([0.5, 1.0])

    def refuse(*a, **k):
        raise Refusal(Finding("source.fake-type", "no"))

    monkeypatch.setattr(ladder.judge, "submit", refuse)
    out = run(fakes)  # a refused header set leaves the fuzzy file unsubmitted, never a crash
    assert out.submitted is None and any("were refused: no" in f for f in out.auto_fixes)


def test_a_work_file_the_drafter_left_uncompiled_is_drafted_again_and_an_edited_one_is_kept(fakes):
    path = ladder.candidate_path(fakes.config, "func_a")
    path.parent.mkdir(parents=True)
    path.write_text("int func_a(void) { return broken; }\n")
    attempt = fixture.attempt(member="func_a", step="draft", score=0.0, text=path.read_text(), outcome="failed")
    host.append(fakes.config, "recipe-attempts/func_a", asdict(attempt))
    assert names(run(fakes, submit=False)) == ["clone", "draft"] and stage_jobs(fakes, "draft.m2c") == [2]
    path.write_text("int func_a(void) { return mine; }\n")
    assert names(run(fakes, submit=False))[0] == "existing"


def test_a_folded_draft_is_measured_with_its_header_edits_in_the_next_command_until_the_header_moves(
    fakes, monkeypatch
):
    (fakes.root / "include").mkdir()
    (fakes.root / "include/a.h").write_text("struct Thing {\n    char pad[8];\n};\n")
    fakes.m2c_text = "struct Thing {{\n    s32 unk0;\n    s32 unk4;\n}};\n\nvoid {name}(void) {{\n}}\n"
    monkeypatch.setattr(ladder.draft.headers, "sources", lambda snapshot, *a: ["include/a.h"])
    measured = []
    real = ladder.judge.score
    monkeypatch.setattr(
        ladder.judge,
        "score",
        lambda c, f, m, x=None, **k: measured.append((x or {}).get("include/a.h")) or real(c, f, m, x),
    )
    run(fakes, submit=False)
    folded = measured[-1]
    assert b"s32 unk4;" in folded and ladder.candidate_path(fakes.config, "func_a").with_suffix(".extras.json").exists()
    measured.clear()
    assert names(run(fakes, submit=False))[0] == "existing" and measured[0] == folded  # the next command carries it
    (fakes.root / "include/a.h").write_text("struct Thing {\n    char pad[4];\n    s32 named;\n};\n")
    assert names(run(fakes, submit=False))[:2] == ["clone", "draft"]  # the header moved: drafted again


def test_a_resumed_attempt_that_no_longer_compiles_is_followed_by_a_fresh_draft(fakes):
    attempted(fakes, 0.7, "int func_a(void) { return lost; }\n", step="draft")
    fakes.compile_error = "x.c:1: undeclared"
    fakes.scores.extend([0.0])
    out = run(fakes, submit=False)
    assert names(out)[:3] == ["resume", "clone", "draft"]


def test_a_candidate_an_earlier_tool_left_takes_the_members_the_resolver_names_at_an_equal_score(fakes, monkeypatch):
    view = cparse.Layout("Actor", "include/actor.h", (cparse.Field("hp", 4, 4, 1, "i"),))
    get = SimpleNamespace(
        declarations={}, identities=tuple(ledger.identities([view])), callers={}, shapes={}, extents={}
    )
    get.globals, get.addresses, get.uses = {}, {}, {}
    monkeypatch.setattr(index, "get", lambda snap: get)
    path = fakes.root / ".unbake/recipe/work/func_a.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("void func_a(Actor *arg0) {\n    x = M2C_FIELD(arg0, s32 *, 4);\n}\n")
    out = run(fakes, submit=False)
    assert "x = arg0->hp;" in path.read_text() and "resolved 1 M2C_FIELD of the candidate" in out.auto_fixes


@pytest.mark.parametrize(
    ("proposed", "replaced"),
    [
        ("int func_a(Obj *a) { return a->unk4; }\n", True),  # exact with no placeholder left: it lands
        ("int func_a(void *a) { return M2C_FIELD(a, s32 *, 4) + 0; }\n", False),  # as many placeholders
    ],
)
def test_an_exact_candidate_held_by_placeholders_meets_the_rungs_and_only_fewer_placeholders_replace_it(
    fakes, monkeypatch, proposed, replaced
):
    from recipemaker import rungs

    held = "int func_a(void *a) { return M2C_FIELD(a, s32 *, 4); }\n"
    path = fakes.root / ".unbake/recipe/work/func_a.c"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(held)
    fakes.scores.extend([1.0, 1.0])
    fakes.screens.append(1.0)
    found = [rungs.Proposal("group record a", proposed, {"include/h.h": "typedef struct Obj Obj;\n"})]
    monkeypatch.setattr(rungs, "RUNGS", (lambda snapshot, member, text: found,))
    out = run(fakes)
    assert "group record a" in names(out)
    assert path.read_text() == (proposed if replaced else held)
    assert (out.submitted == "id-func_a") is replaced
