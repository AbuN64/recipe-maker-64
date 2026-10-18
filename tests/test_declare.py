"""Declaration items: the item and its brief, and a change set of edited copies proved and landed as one."""

from dataclasses import replace

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
    assert seen["edits"]["src/a.c"] == b'#include "a.h"\nextern f32 gY;\nint func_a(void) { return 0; }\n'
    assert got["auto_fixes"] == ["dropped 2 repeated externs from src/a.c"]
    assert "auto-fixes: dropped 2 repeated externs from src/a.c" in cli.render("try", got)


def test_a_name_with_a_landed_definition_starts_with_every_spelling_rewritten_to_it(world, monkeypatch):
    row = {
        **ROW,
        "name": "func_f",
        "spellings": [("include/a.h", 1, "s32 func_f(void);"), ("src/a.c", 2, "extern s32 func_f(s32 a);")],
        "definition": ("src/f.c", 4, "s32 func_f(s32 a, s32 *b)"),
    }
    monkeypatch.setattr(declare, "conflicts", lambda config, snapshot: [row])
    world.snapshot = replace(
        world.snapshot,
        overlays={
            "include/a.h": b"s32 func_f(void);\n",
            "src/a.c": b'#include "a.h"\nextern s32 func_f(s32 a);\nint g(void) { return 0; }\n',
        },
    )
    declare.take(world.config, world.snapshot, row, NOW)
    where = declare.folder(world.config, "func_f")
    assert (where / "include/a.h").read_text() == "s32 func_f(s32 a, s32 *b);\n"
    assert (where / "src/a.c").read_text().splitlines()[1] == "extern s32 func_f(s32 a, s32 *b);"
    assert "The copies already spell it so" in (where / "brief.md").read_text()
