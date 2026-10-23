"""Declaration items: the item and its brief, and a change set of edited copies proved and landed as one."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fakes import unit as make_unit
from unbake.contracts import Refusal

from recipemaker import cli, declare, host, state, typeitem, work

NOW = 1_000_000.0
ROW = {
    "name": "gX",
    "spellings": [("include/a.h", 9, "extern f32 gX;"), ("src/a.c", 3, "s32 gX;")],
    "items": ["func_b", "func_c"],
}


@pytest.fixture
def world(fakes, monkeypatch):
    fakes.with_units({"src/a.c": make_unit("src/a.c", ("func_a",))})
    fakes.snapshot = replace(
        fakes.snapshot,
        overlays={
            "include/a.h": b"extern f32 gX;\n",
            "src/a.c": b'#include "a.h"\ns32 gX;\nint func_a(void) { return 0; }\n',
        },
    )
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [ROW])
    monkeypatch.setattr(declare.state, "skipped", lambda config, now: frozenset())
    monkeypatch.setattr(work, "_now", lambda: NOW)
    return fakes


def test_next_hands_out_the_worst_conflict_with_copies_of_every_file_and_a_brief(world):
    got = declare.next_item(world.config, world.snapshot, NOW)
    where = declare.folder(world.config, "gX")
    assert (got["item"], got["kind"], got["brief"]) == ("gX", "declaration", str(where / "brief.md"))
    assert (where / "include/a.h").read_bytes() == b"extern f32 gX;\n" and (where / "src/a.c").is_file()
    text = (where / "brief.md").read_text()
    assert "- include/a.h:9  extern f32 gX;" in text and "- src/a.c:3  s32 gX;" in text
    assert "## Blocks 2 items\\n- func_b\\n- func_c" in text.replace("\\n", "\n").replace("\n", "\\n")
    assert [x.item for x in state.leases(world.config, NOW)] == ["gX"]
    assert declare.next_item(world.config, world.snapshot, NOW)["item"] == ""  # the only conflict is leased


def test_next_with_the_declaration_kind_goes_through_the_work_verb_and_reads_in_text(world, monkeypatch):
    got = work.next_item(world.config, {"kind": "declaration"})
    text = cli.render("next", got)
    assert "kind: declaration" in text and "brief: " in text and "next: read the brief, edit the copies" in text


def edited(world, header=b"extern u8 gX[4];\n", source=None):
    declare.next_item(world.config, world.snapshot, NOW)
    where = declare.folder(world.config, "gX")
    (where / "include/a.h").write_bytes(header)
    (where / "src/a.c").write_bytes(source or b'#include "a.h"\nint func_a(void) { return 0; }\n')


def land(world, monkeypatch, exact=True, problem="the ROMs differ"):
    """settle.land answering for every set: a commit when `exact`, else its problem; the screen answers the same."""
    seen = {}

    def settled(config, old, edits, message):
        seen.update(edits=edits, old=old, message=message)
        return {"commit": "abc1234"} if exact else {"problem": problem}

    monkeypatch.setattr(declare.settle, "land", settled)
    world.screens.extend([1.0 if exact else 0.4] * 4)
    return seen


def test_try_lands_the_edited_copies_as_one_commit_after_the_screen(world, monkeypatch):
    edited(world)
    seen = land(world, monkeypatch)
    got = work.try_(world.config, {"item": "gX"})
    assert sorted(seen["edits"]) == ["include/a.h", "src/a.c"] and world.calls["judge.score_many"] == 1
    assert seen["message"].startswith("Declare gX once in a.h from its definition")
    assert seen["old"]["src/a.c"] == world.snapshot.peek("src/a.c")  # what the tree held when the set was planned
    assert (got["result"], got["landed"], got["next"]) == ("exact", "abc1234", "recipe next")
    assert state.leases(world.config, NOW) == [] and cli.render("try", got).splitlines()[0] == "landed (abc1234)"


def test_only_the_files_that_changed_are_in_the_set(world, monkeypatch):
    edited(world, source=b'#include "a.h"\ns32 gX;\nint func_a(void) { return 0; }\n')
    seen = land(world, monkeypatch)
    work.try_(world.config, {"item": "gX"})
    assert sorted(seen["edits"]) == ["include/a.h"]  # a.c includes "a.h" and carries the screen


def test_try_refuses_copies_nobody_changed(world):
    declare.next_item(world.config, world.snapshot, NOW)
    with pytest.raises(Refusal) as caught:
        work.try_(world.config, {"item": "gX"})
    assert caught.value.findings[0].key == "recipe.no-change"


def test_try_refuses_a_cast_in_an_edited_source_before_any_proof(world, monkeypatch):
    edited(world, source=b"int func_a(void) { return (struct Cup *) gX; }\n")
    seen = land(world, monkeypatch)
    with pytest.raises(Refusal) as caught:
        work.try_(world.config, {"item": "gX"})
    assert caught.value.findings[0].key == "recipe.cast" and not seen


def test_a_set_that_does_not_screen_exact_lands_nothing_and_counts_a_try(world, monkeypatch):
    edited(world)
    seen = land(world, monkeypatch, exact=False)
    got = work.try_(world.config, {"item": "gX"})
    assert (got["result"], got["landed"], got["stalled"]) == (
        "fuzzy",
        "",
        0,
    ) and not seen  # the score rose from nothing
    assert got["next"] == "edit the copies, then run: recipe try gX" and state.lease(world.config, "gX").session


def test_roms_that_differ_after_the_set_was_written_are_reported_with_the_cause_and_commit_nothing(world, monkeypatch):
    edited(world)
    land(world, monkeypatch, exact=True)
    monkeypatch.setattr(
        declare.settle, "land", lambda config, old, edits, message: {"problem": "the ROMs differ: us: sha1 differs"}
    )
    got = work.try_(world.config, {"item": "gX"})
    assert got["landed"] == "" and got["result"] != "exact" and "the ROMs differ: us: sha1 differs" in got["why"][0]
    assert state.lease(world.config, "gX")


def test_a_cast_on_an_untouched_old_line_does_not_refuse_the_set_but_a_new_one_does(world, monkeypatch):
    old = b'#include "a.h"\nint func_a(void) { return (struct Cup *) gX; }\n'
    world.snapshot = replace(world.snapshot, overlays={**world.snapshot.overlays, "src/a.c": old})
    edited(world, source=old + b"int func_b(void) { return (u8 *) gX; }\n")
    seen = land(world, monkeypatch)
    with pytest.raises(Refusal) as caught:
        work.try_(world.config, {"item": "gX"})
    assert "line 3" in caught.value.findings[0].reason and not seen
    (declare.folder(world.config, "gX") / "src/a.c").write_bytes(old + b"int func_b(void) { return 1; }\n")
    assert work.try_(world.config, {"item": "gX"})["result"] == "exact" and seen


def test_the_brief_names_the_header_that_gets_the_declaration_and_the_item_has_no_twin_line(world):
    got = declare.next_item(world.config, world.snapshot, NOW)
    text = (declare.folder(world.config, "gX") / "brief.md").read_text()
    assert "## Headers\n- include/a.h" in text and "Put it in include/a.h." in text
    assert "twin" not in got and "twin:" not in cli.render("next", got)


def test_try_drops_file_local_externs_identical_to_the_header_and_keeps_one_that_disagrees(world, monkeypatch):
    source = b'#include "a.h"\nextern u8 gX[4];\nextern s32 gOther;\nextern f32 gY;\nint func_a(void) { return 0; }\n'
    edited(world, header=b"extern u8 gX[4];\nextern s32 gOther;\n", source=source)
    seen = land(world, monkeypatch)
    got = work.try_(world.config, {"item": "gX"})
    assert (
        seen["edits"]["src/a.c"]
        == b'#include "a.h"\nextern s32 gOther;\nextern f32 gY;\nint func_a(void) { return 0; }\n'
    )
    assert got["auto_fixes"] == ["dropped 1 repeated externs from src/a.c"]
    assert "auto-fixes: dropped 1 repeated externs from src/a.c" in cli.render("try", got)


def test_a_local_variable_line_that_reads_like_a_header_statement_is_never_dropped(fakes):
    fakes.snapshot = replace(fakes.snapshot, overlays={"include/a.h": b"extern s32 gX;\ns32 t;\n"})
    source = b'#include "a.h"\nint f(void) {\n    s32 t;\n    return t + gX;\n}\n'
    out, notes = declare._drop_repeats(fakes.snapshot, {"src/a.c": source}, "gX")
    assert out["src/a.c"] == source and notes == []


def test_an_edited_header_that_is_now_empty_is_read_as_empty_not_from_disk(fakes):
    fakes.snapshot = replace(fakes.snapshot, overlays={"include/a.h": b"extern s32 gX;\n"})
    edits = {"include/a.h": b"", "src/a.c": b'#include "a.h"\nextern s32 gX;\nint f(void) { return gX; }\n'}
    out, notes = declare._drop_repeats(fakes.snapshot, edits, "gX")
    assert out["src/a.c"] == edits["src/a.c"] and notes == []  # the empty header declares nothing to drop


FILES = {
    "include/f.h": b"#ifndef F_H\n#define F_H\nextern s32 other(void);\n#endif\n",
    "include/a.h": b"extern s32 func_f(void);\nextern s32 more(void);\n",
    "src/f.c": b'#include "f.h"\ns32 func_f(s32 a, s32 *b) { return a; }\n',
    "src/a.c": (
        b'#include "a.h"\n/* extern */\nextern u16 func_f(u16 a, s32 *b);\nint func_a(void) { return func_f(1, 0); }\n'
    ),
    "src/u.c": b'#include "a.h"\nint func_u(void) { return func_f(2, 0); }\n',
}
DEF = ("src/f.c", 2, "s32 func_f(s32 a, s32 *b)")
FROW = {
    "name": "func_f",
    "spellings": [("include/a.h", 1, "extern s32 func_f(void);"), ("src/a.c", 3, "extern u16 func_f(u16 a, s32 *b);")],
    "definition": DEF,
    "items": [],
    "consumers": 2,
}


@pytest.fixture
def tree(world, monkeypatch):
    world.snapshot = replace(world.snapshot, overlays=dict(FILES))
    monkeypatch.setattr(declare, "_users", lambda snapshot, name: ["src/a.c", "src/f.c", "src/u.c"])
    return world


def test_a_plan_puts_the_prototype_in_the_family_header_deletes_the_copies_and_adds_the_includes(tree):
    made = declare.plan(tree.snapshot, FROW)
    assert made.header == "include/f.h" and not made.skip
    assert sorted(made.edits) == ["include/a.h", "include/f.h", "src/a.c", "src/u.c"]
    assert made.edits["include/f.h"].decode().splitlines()[2:4] == [
        "extern s32 other(void);",
        "extern s32 func_f(s32 a, s32 *b);",
    ]
    assert made.edits["include/a.h"] == b"extern s32 more(void);\n"
    assert made.edits["src/a.c"].decode().splitlines() == [
        '#include "a.h"',
        '#include "f.h"',
        "int func_a(void) { return func_f(1, 0); }",
    ]
    assert made.edits["src/u.c"].startswith(b'#include "a.h"\n#include "f.h"\n')  # it lost sight of the name
    assert (made.removed, made.includes, made.casts, made.cast_needed) == (2, 2, 0, True)


def test_the_cast_variant_casts_the_call_sites_of_the_unit_whose_old_copy_took_other_scalar_types(tree):
    made = declare.plan(tree.snapshot, FROW, casts=True)
    assert "func_f((u16)1, 0)" in made.edits["src/a.c"].decode() and "(u16)func_f(" in made.edits["src/a.c"].decode()
    assert made.casts == 1 and made.blocked == ()


def test_a_name_without_a_landed_definition_or_a_header_to_declare_into_is_reported_not_guessed(tree):
    made = declare.plan(tree.snapshot, {**FROW, "definition": None})
    assert made.edits == {} and "no landed definition" in made.skip
    made = declare.plan(tree.snapshot, {**FROW, "definition": ("src/f.c", 2, "static s32 func_f(s32 a)")})
    assert "file-local" in made.skip


def test_a_definition_whose_unit_has_no_family_header_declares_into_the_header_of_its_folder_it_includes(tree):
    unit = b'#include "other/x.h"\n#include "m/near.h"\ns32 func_f(s32 a, s32 *b) { return a; }\n'
    files = {**FILES, "src/m/g.c": unit, "include/m/near.h": b"extern s32 q(void);\n", "include/other/x.h": b""}
    tree.snapshot = replace(tree.snapshot, overlays=files)
    made = declare.plan(tree.snapshot, {**FROW, "definition": ("src/m/g.c", 3, "s32 func_f(s32 a, s32 *b)")})
    assert made.header == "include/m/near.h" and b"func_f(s32 a, s32 *b)" in made.edits["include/m/near.h"]


def test_a_definition_whose_unit_reaches_no_header_is_reported_not_given_a_header_unbake_cannot_prove(tree):
    tree.snapshot = replace(tree.snapshot, overlays={**FILES, "src/g.c": b"s32 func_f(s32 a, s32 *b) { return a; }\n"})
    row = {**FROW, "spellings": FROW["spellings"][1:], "definition": ("src/g.c", 1, "s32 func_f(s32 a, s32 *b)")}
    made = declare.plan(tree.snapshot, row)
    assert made.edits == {} and "no header to declare into" in made.skip


def test_ranking_puts_names_the_open_functions_need_before_hygiene_and_width_mismatches_first(world, monkeypatch):
    rows = [
        {"name": "hygiene", "spellings": [("i.h", 1, "extern s32 hygiene(void);")], "items": [], "consumers": 90},
        {"name": "needed", "spellings": [("i.h", 2, "extern s32 needed(void);")], "items": [], "consumers": 1},
        {
            "name": "wide",
            "spellings": [("i.h", 3, "extern u8 wide[4];"), ("a.c", 4, "extern u32 wide[4];")],
            "items": [],
            "consumers": 0,
        },
    ]
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: rows)
    monkeypatch.setattr(
        declare.index,
        "get",
        lambda snapshot: SimpleNamespace(callers={"needed": ("func_a", "func_b")}, uses={}),
    )
    got = [r["name"] for r in declare.ranked(world.config, world.snapshot)]
    assert got == ["wide", "needed", "hygiene"]  # func_a, func_b are open in the fake layout


def test_next_leases_the_first_planned_name_and_writes_it_planned(tree, monkeypatch):
    second = {**FROW, "name": "func_g", "spellings": [("src/b.c", 1, "extern s32 func_g(void);")], "definition": None}
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [FROW, second])
    got = declare.next_item(tree.config, tree.snapshot, NOW)
    where = declare.folder(tree.config, "func_f")
    assert got["item"] == "func_f" and got["planned"]
    assert (where / "include/f.h").read_text().count("func_f(s32 a, s32 *b)") == 1
    assert json.loads((where / "plan.json").read_text()) == {"risky": ["src/a.c"]}
    assert "Planned" in (where / "brief.md").read_text()


def landing(monkeypatch, refuse=(), edit_check=None):
    """settle.land committing every set as c1, c2 ... unless its files hold a path in `refuse`."""
    seen = []

    def settled(config, old, edits, message):
        seen.append(sorted(edits))
        if edit_check is not None and not edit_check(edits):
            return {"problem": "the ROMs differ"}
        return {"problem": "the ROMs differ"} if set(edits) & set(refuse) else {"commit": f"c{len(seen)}"}

    monkeypatch.setattr(declare.settle, "land", settled)
    return seen


def test_a_name_that_fails_plain_is_retried_alone_with_the_casts_its_old_types_need(tree, monkeypatch):
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [FROW])
    declare.next_item(tree.config, tree.snapshot, NOW)
    tree.screens.extend([0.4, 1.0])  # the retry screens exact
    landing(monkeypatch, edit_check=lambda edits: b"(u16)func_f" in edits.get("src/a.c", b""))
    got = work.try_(tree.config, {"item": "func_f"})
    assert got["result"] == "exact" and "cast 1 call sites" in " ".join(got["auto_fixes"])


def test_the_family_header_is_the_one_the_folder_layout_gives_the_unit_even_when_the_unit_does_not_include_it(
    tree, monkeypatch
):
    files = {**FILES, "src/M/X_1.CPP": b"s32 func_f(s32 a, s32 *b) { return a; }\n", "include/M/X_1.H": b"/* x */\n"}
    tree.snapshot = replace(tree.snapshot, overlays=files)
    monkeypatch.setattr(declare.headers, "sources", lambda snapshot, place="include", suffix=".h": ["include/M/X_1.H"])
    row = {**FROW, "definition": ("src/M/X_1.CPP", 1, "s32 func_f(s32 a, s32 *b)")}
    assert declare.family(tree.snapshot, "src/M/X_1.CPP", []) == "include/M/X_1.H"
    assert declare.plan(tree.snapshot, row).header == "include/M/X_1.H"


def test_a_name_waits_when_its_header_would_stop_a_unit_that_includes_it_compiling(tree):
    files = {
        **FILES,
        "include/a.h": b"extern s32 func_f(void);\nextern s32 clash(s32 a);\n",
        "src/a.c": FILES["src/a.c"] + b"extern s32 clash(void);\n",
    }
    tree.snapshot = replace(tree.snapshot, overlays=files)
    made = declare.plan(tree.snapshot, {**FROW, "definition": ("src/f.c", 2, "s32 func_f(s32 a, s32 *b)")})
    assert made.waits == () and made.edits  # f.h declares no `clash`: nothing to wait for
    files["include/f.h"] = b"#ifndef F_H\n#define F_H\nextern s32 clash(s32 a);\n#endif\n"
    tree.snapshot = replace(tree.snapshot, overlays=files)
    made = declare.plan(tree.snapshot, {**FROW, "definition": ("src/f.c", 2, "s32 func_f(s32 a, s32 *b)")})
    assert made.waits == ("clash",) and made.edits == {} and "waits for clash" in made.skip


def test_the_header_gets_an_include_of_the_header_that_declares_a_type_its_prototype_names(tree, monkeypatch):
    files = {**FILES, "include/t.h": b"typedef struct Widget Widget;\n"}
    tree.snapshot = replace(tree.snapshot, overlays=files)
    monkeypatch.setattr(
        declare.headers, "catalog", lambda snapshot: {"Widget": ("include/t.h", 1, "typedef struct Widget Widget;")}
    )
    made = declare.plan(tree.snapshot, {**FROW, "definition": ("src/f.c", 2, "Widget *func_f(s32 a, s32 *b)")})
    assert made.edits["include/f.h"].decode().splitlines()[2:4] == ['#include "t.h"', "extern s32 other(void);"]


def test_leasing_a_name_again_clears_the_copies_an_earlier_lease_left_in_its_folder(world):
    declare.next_item(world.config, world.snapshot, NOW)
    where = declare.folder(world.config, "gX")
    (where / "batch.json").write_text("[]")  # a file from an older tool version, read as an edit if it stayed
    (where / "include/old.h").write_text("stale")
    state.release(world.config, "gX")
    declare.next_item(world.config, world.snapshot, NOW)
    assert not (where / "batch.json").exists() and not (where / "include/old.h").exists()


def test_a_type_item_lands_under_its_own_message_and_leaves_each_edited_source_as_a_recorded_candidate(
    world, monkeypatch
):
    edited(world)
    where = declare.folder(world.config, "gX")
    (where / "plan.json").write_text(json.dumps({"function": "func_a", "record": "R", "message": "Type it"}))
    seen = land(world, monkeypatch)
    monkeypatch.setattr(typeitem, "done", lambda config, function: seen.update(done=function))
    work.try_(world.config, {"item": "gX", "dry": True})
    rows = [r for r in host.rows(world.config, "recipe/tries") if r["item"] == "gX"]
    assert [r["path"] for r in rows] == ["src/a.c"] and "func_a" in rows[0]["text"] and not seen.get("message")
    world.screens.extend([1.0] * 4)
    work.try_(world.config, {"item": "gX"})
    assert seen["message"] == "Type it" and seen["done"] == "func_a"


def test_a_cast_of_the_record_a_type_item_defines_is_no_banned_cast(world):
    edits = {"src/a.c": b"void g(void) { f((R *) buf); }\n"}
    assert declare._banned(world.snapshot, "gX", edits)
    assert declare._banned(world.snapshot, "gX", edits, "R") is None
