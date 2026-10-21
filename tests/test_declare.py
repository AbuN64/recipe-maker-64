"""Declaration items: the item and its brief, and a change set of edited copies proved and landed as one."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fakes import proof
from fakes import unit as make_unit
from unbake.contracts import Refusal

from recipemaker import cli, declare, state, work

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


def land(world, monkeypatch, exact=True, refused=()):
    seen = {}

    def land_set(config, snapshot, unit, edits):
        seen.update(unit=unit.path, edits=edits)
        found = [proof("func_a", v, 1.0 if exact else 0.4) for v in ("a", "b")]
        landed = [{"id": "x", "member": "func_a", "operation": "publish", "commit": "abc1234"}] if exact else []
        return found, {"landed": landed, "refused": list(refused)}

    monkeypatch.setattr(declare.ladder, "land_set", land_set)
    monkeypatch.setattr(
        declare.ladder, "submit_set", lambda config, snapshot, unit, edits: land_set(config, snapshot, unit, edits)[1]
    )
    return seen


def test_try_lands_the_edited_copies_as_one_change_set_under_the_edited_unit(world, monkeypatch):
    edited(world)
    seen = land(world, monkeypatch)
    got = work.try_(world.config, {"item": "gX"})
    assert seen["unit"] == "src/a.c" and sorted(seen["edits"]) == ["include/a.h", "src/a.c"]
    assert (got["result"], got["landed"], got["next"]) == ("exact", "abc1234", "recipe next")
    assert state.leases(world.config, NOW) == [] and cli.render("try", got).splitlines()[0] == "landed (abc1234)"


def test_only_the_files_that_changed_are_in_the_set_and_a_header_alone_goes_under_an_including_unit(world, monkeypatch):
    edited(world, source=b'#include "a.h"\ns32 gX;\nint func_a(void) { return 0; }\n')
    seen = land(world, monkeypatch)
    work.try_(world.config, {"item": "gX"})
    assert sorted(seen["edits"]) == ["include/a.h"] and seen["unit"] == "src/a.c"  # a.c includes "a.h"


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


def test_a_set_that_is_not_exact_lands_nothing_and_counts_a_try(world, monkeypatch):
    edited(world)
    land(world, monkeypatch, exact=False)
    got = work.try_(world.config, {"item": "gX"})
    assert (got["result"], got["landed"], got["stalled"]) == ("fuzzy", "", 0)  # the score rose from nothing
    assert got["next"] == "edit the copies, then run: recipe try gX" and state.lease(world.config, "gX").session


def test_unbake_refusing_the_set_by_unit_name_is_reported_and_costs_no_try(world, monkeypatch):
    edited(world)
    finding = {"reason": "src/b.c does not match in us", "missing": ["us: bytes differ"], "key": "land.not_exact"}
    land(world, monkeypatch, exact=False, refused=[{"member": "func_a", "findings": [finding]}])
    got = work.try_(world.config, {"item": "gX"})
    assert got["result"] == "refused" and "src/b.c does not match in us" in got["why"]
    assert state.lease(world.config, "gX").tries == 0


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


def plan_of(name, *files, critical=0):
    return declare.Plan(name, {f: b"x" for f in files}, critical=critical)


def test_groups_hold_names_with_disjoint_files_in_rank_order_and_leave_out_the_unplanned():
    plans = [
        plan_of("a", "h1", "a.c"),
        plan_of("b", "h1", "b.c"),
        plan_of("c", "c.c"),
        declare.Plan("d", {}, skip="no landed definition"),
        plan_of("e", "h1", "e.c"),
        plan_of("f", "f.c"),
    ]
    assert [[p.name for p in g] for g in declare.groups(plans)] == [["a", "c", "f"], ["b"], ["e"]]
    assert [[p.name for p in g] for g in declare.groups(plans, size=2)] == [["a", "c"], ["b", "f"], ["e"]]


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


def test_next_dry_shows_the_batches_and_change_sets_and_leases_and_writes_nothing(tree, monkeypatch):
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [FROW])
    got = declare.next_item(tree.config, tree.snapshot, NOW, 5, dry=True)
    assert got["dry"] and got["batches"] == [["func_f"]] and got["plans"][0]["header"] == "include/f.h"
    assert state.leases(tree.config, NOW) == [] and not declare.folder(tree.config, "func_f").exists()
    text = cli.render("next", got)
    assert "batch 1: func_f" in text and "2 copies deleted, 2 includes added, needs casts" in text


def test_next_leases_the_first_group_writes_each_name_planned_and_names_the_batch(tree, monkeypatch):
    second = {**FROW, "name": "func_g", "spellings": [("src/b.c", 1, "extern s32 func_g(void);")], "definition": None}
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [FROW, second])
    got = declare.next_item(tree.config, tree.snapshot, NOW)
    where = declare.folder(tree.config, "func_f")
    assert got["item"] == "func_f" and got["batch"] == ["func_f"] and got["planned"]
    assert (where / "include/f.h").read_text().count("func_f(s32 a, s32 *b)") == 1
    assert json.loads((where / "batch.json").read_text()) == ["func_f"]
    assert "Planned" in (where / "brief.md").read_text()


def two_names(tree, monkeypatch):
    """func_f (include/f.h, src/a.c) and func_h (include/h.h, src/b.c): independent change sets in one batch."""
    files = {
        **FILES,
        "include/h.h": b"#ifndef H_H\n#define H_H\n#endif\n",
        "src/h.c": b'#include "h.h"\ns32 func_h(s32 a) { return a; }\n',
        "src/b.c": b'#include "b.h"\nextern s32 func_h(void);\nint func_b(void) { return func_h(); }\n',
        "include/b.h": b"",
    }
    tree.snapshot = replace(tree.snapshot, overlays=files)
    tree.with_units({"src/a.c": make_unit("src/a.c", ("func_a",)), "src/b.c": make_unit("src/b.c", ("func_b",))})
    tree.snapshot = replace(tree.snapshot, overlays=files)
    row_h = {
        "name": "func_h",
        "spellings": [("src/b.c", 2, "extern s32 func_h(void);")],
        "definition": ("src/h.c", 2, "s32 func_h(s32 a)"),
        "items": [],
        "consumers": 1,
    }
    users = {"func_f": ["src/a.c", "src/f.c", "src/u.c"], "func_h": ["src/b.c", "src/h.c"]}
    monkeypatch.setattr(declare, "_users", lambda snapshot, name: users[name])
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [FROW, row_h])
    return row_h


def landing(world, monkeypatch, exact):
    """land_set that lands each name whose carrier unit is in `exact`, one commit each."""
    seen = []

    def land_set(config, snapshot, unit, edits):
        seen.append((unit.path, sorted(edits)))
        good = unit.path in exact
        found = [proof("func_a", v, 1.0 if good else 0.4) for v in ("a", "b")]
        landed = [{"id": "x", "member": "m", "operation": "publish", "commit": f"c{len(seen)}"}] if good else []
        return found, {"landed": landed, "refused": []}

    monkeypatch.setattr(declare.ladder, "land_set", land_set)
    monkeypatch.setattr(
        declare.ladder, "submit_set", lambda config, snapshot, unit, edits: land_set(config, snapshot, unit, edits)[1]
    )
    return seen


def test_a_batch_is_measured_once_and_each_exact_name_lands_as_its_own_commit(tree, monkeypatch):
    two_names(tree, monkeypatch)
    declare.next_item(tree.config, tree.snapshot, NOW)
    tree.screens.extend([1.0, 1.0])
    seen = landing(tree, monkeypatch, {"src/a.c", "src/b.c"})
    got = work.try_(tree.config, {"item": "func_f"})
    assert tree.calls["judge.score_many"] == 1 and [u for u, _ in seen] == ["src/a.c", "src/b.c"]
    assert [(b["item"], b["result"], b["landed"]) for b in got["batch"]] == [
        ("func_f", "exact", "c1"),
        ("func_h", "exact", "c2"),
    ]
    assert state.leases(tree.config, NOW) == []
    text = cli.render("try", got)
    assert "- func_h: exact (c2)" in text


def test_a_name_that_is_not_exact_in_the_screen_is_split_out_and_the_others_still_land(tree, monkeypatch):
    two_names(tree, monkeypatch)
    declare.next_item(tree.config, tree.snapshot, NOW)
    tree.screens.extend([0.4, 1.0])
    seen = landing(tree, monkeypatch, {"src/b.c"})
    got = work.try_(tree.config, {"item": "func_f"})
    assert got["result"] == "fuzzy" and got["landed"] == ""
    assert [(b["item"], b["result"]) for b in got["batch"]] == [("func_f", "fuzzy"), ("func_h", "exact")]
    assert [u for u, _ in seen] == ["src/b.c", "src/a.c"] and state.lease(tree.config, "func_f")  # a.c: the retry


def test_a_name_that_fails_plain_is_retried_alone_with_the_casts_its_old_types_need(tree, monkeypatch):
    two_names(tree, monkeypatch)
    declare.next_item(tree.config, tree.snapshot, NOW)
    tree.screens.extend([0.4, 1.0])
    sets = []

    def land_set(config, snapshot, unit, edits):
        sets.append(edits)
        good = b"(u16)func_f" in edits.get("src/a.c", b"") or unit.path == "src/b.c"
        found = [proof("func_a", v, 1.0 if good else 0.4) for v in ("a", "b")]
        landed = [{"id": "x", "member": "m", "operation": "publish", "commit": f"c{len(sets)}"}] if good else []
        return found, {"landed": landed, "refused": []}

    monkeypatch.setattr(declare.ladder, "land_set", land_set)
    monkeypatch.setattr(
        declare.ladder, "submit_set", lambda config, snapshot, unit, edits: land_set(config, snapshot, unit, edits)[1]
    )
    got = work.try_(tree.config, {"item": "func_f"})
    assert got["result"] == "exact" and "cast 1 call sites" in " ".join(got["auto_fixes"])
    assert [b["result"] for b in got["batch"]] == ["exact", "exact"]


def test_try_dry_measures_the_batch_and_lands_nothing(tree, monkeypatch):
    two_names(tree, monkeypatch)
    declare.next_item(tree.config, tree.snapshot, NOW)
    tree.screens.extend([1.0, 0.4])
    seen = landing(tree, monkeypatch, {"src/a.c", "src/b.c"})
    got = work.try_(tree.config, {"item": "func_f", "dry": True})
    assert seen == [] and [b["result"] for b in got["batch"]] == ["would-land", "fuzzy"]
    assert got["landed"] == "" and len(state.leases(tree.config, NOW)) == 2


def test_a_try_that_spent_its_cpu_budget_leaves_the_exact_names_it_did_not_reach_leased_for_the_next_try(
    tree, monkeypatch
):
    two_names(tree, monkeypatch)
    declare.next_item(tree.config, tree.snapshot, NOW)
    tree.screens.extend([1.0, 1.0])
    seen = landing(tree, monkeypatch, {"src/a.c", "src/b.c"})
    spent = iter([0.0, 0.0, 500.0, 500.0, 500.0, 500.0, 500.0])
    monkeypatch.setattr(declare, "_cpu", lambda: next(spent))
    got = work.try_(tree.config, {"item": "func_f"})
    assert [(b["item"], b["result"]) for b in got["batch"]] == [("func_f", "exact"), ("func_h", "waiting")]
    assert [u for u, _ in seen] == ["src/a.c"] and [x.item for x in state.leases(tree.config, NOW)] == ["func_h"]


def test_groups_stop_at_the_file_budget_but_a_single_big_name_still_gets_its_own_group():
    plans = [
        plan_of("a", *[f"a{i}.c" for i in range(70)]),
        plan_of("b", *[f"b{i}.c" for i in range(60)]),
        plan_of("c", "c.c"),
    ]
    assert [[p.name for p in g] for g in declare.groups(plans, budget=100)] == [["a", "c"], ["b"]]
