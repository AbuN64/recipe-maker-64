"""The rungs: what each proposes from synthetic facts."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from recipemaker import cparse, index, ledger, rungs


def world(monkeypatch, *views):
    found = tuple(ledger.Identity((v,), v.fields) for v in views)
    monkeypatch.setattr(
        index, "get", lambda snap: SimpleNamespace(identities=found, extents={}, globals={}, addresses={})
    )


ACTOR = cparse.Layout("Actor", "include/actor.h", (cparse.Field("x", 0, 4, 1, "i"), cparse.Field("hp", 4, 4, 1, "i")))
WIDE = cparse.Layout("Wide", "include/wide.h", (*ACTOR.fields, cparse.Field("tail", 8, 0x40, 1, "i")))
TEXT = '#include "a.h"\nvoid f(void *arg0) {\n    return M2C_FIELD(arg0, s32 *, 4);\n}\n'


def test_retype_gives_an_untyped_base_the_smallest_struct_that_fits_and_names_its_fields(monkeypatch):
    world(monkeypatch, WIDE, ACTOR)
    (proposal,) = rungs.retype(None, "f", TEXT)
    assert "void f(Actor *arg0)" in proposal.text and "arg0->hp" in proposal.text
    assert '#include "actor.h"' in proposal.text
    assert proposal.evidence == ("retyped arg0 as Actor (include/actor.h): 1 of 1 placeholders named",)


def test_retype_proposes_nothing_when_no_struct_fits_every_access(monkeypatch):
    world(monkeypatch)
    assert rungs.retype(None, "f", TEXT) == []


def test_retype_changes_the_parameter_of_the_function_never_the_same_name_in_a_callee_prototype(monkeypatch):
    world(monkeypatch, WIDE, ACTOR)
    text = "void g(void *arg0, s32 n);\nvoid f(void *arg0) {\n    g(arg0, M2C_FIELD(arg0, s32 *, 4));\n}\n"
    (proposal,) = rungs.retype(None, "f", text)
    assert "void g(void *arg0, s32 n);" in proposal.text and "void f(Actor *arg0) {" in proposal.text


PADDED = cparse.Layout(
    "Box",
    "include/box.h",
    (cparse.Field("x", 0, 4, 1, "i"), cparse.Field("tail", 0x40, 4, 1, "i")),
    (cparse.Field("pad4", 4, 1, 0x3C, "i"),),
)
BOX_H = "typedef struct Box {\n    s32 x;\n    char pad4[0x3C];\n    s32 tail;\n} Box;\n"
BOXED = "void f(Box *arg0) {\n    M2C_FIELD(arg0, s32 *, 0x1C) = 0;\n    y = M2C_FIELD(arg0, u8 *, 0x21);\n}\n"


def test_an_access_inside_a_padding_array_splits_the_pad_in_the_header_and_names_the_field():
    found = (ledger.Identity((PADDED,), PADDED.fields),)
    text, edits, notes = rungs.split(BOXED, found, {"include/box.h": BOX_H})
    assert text == "void f(Box *arg0) {\n    arg0->unk1C = 0;\n    y = arg0->unk21;\n}\n"
    assert edits["include/box.h"] == (
        "typedef struct Box {\n    s32 x;\n    char pad4[0x18];\n    s32 unk1C;\n    char pad20[0x1];\n"
        "    u8 unk21;\n    char pad22[0x1E];\n    s32 tail;\n} Box;\n"
    )
    assert notes == ["split pad4 of Box (include/box.h) for 2 accesses through arg0"]


def test_a_misaligned_or_disagreeing_access_never_moves_a_byte_of_the_header():
    found = (ledger.Identity((PADDED,), PADDED.fields),)
    for text in (
        "void f(Box *arg0) {\n    M2C_FIELD(arg0, s32 *, 0x1E) = 0;\n}\n",
        "void f(Box *arg0) {\n    M2C_FIELD(arg0, s32 *, 0x8) = 0;\n    M2C_FIELD(arg0, s16 *, 0x8) = 0;\n}\n",
        "void f(void *arg0) {\n    M2C_FIELD(arg0, s32 *, 0x8) = 0;\n}\n",
    ):
        assert rungs.split(text, found, {"include/box.h": BOX_H}) == (text, {}, [])


CALLEE = "void g(void *arg0, s32 n) {\n    M2C_FIELD(arg0, s32 *, 4) = n;\n}\n"
PROTO_H = '#include "types.h"\nextern void g(void *arg0, s32 n);\n'


def callers(monkeypatch, found, seen):
    index_ = SimpleNamespace(
        identities=found, extents={}, globals={}, addresses={}, declarations={"g": ("include/g.h", 2, "")}
    )
    monkeypatch.setattr(index, "get", lambda snap: index_)
    monkeypatch.setattr(rungs.draft, "header_texts", lambda snap: {"include/g.h": PROTO_H, "include/box.h": BOX_H})
    monkeypatch.setattr(rungs, "evidence", lambda snap, member, position: seen)
    monkeypatch.setattr(rungs.dispatch, "group", lambda snap, member, position: None)  # no table holds g


def test_an_untyped_parameter_takes_the_one_type_its_callers_pass_with_the_prototype_retyped_too(monkeypatch):
    callers(monkeypatch, (ledger.Identity((ACTOR,), ACTOR.fields),), ["Actor", None, "Actor"])
    (proposal,) = rungs.retype_from_callers(None, "g", CALLEE)
    assert "void g(Actor *arg0, s32 n) {" in proposal.text and "arg0->hp = n;" in proposal.text
    assert '#include "actor.h"' in proposal.text
    assert proposal.extras == {"include/g.h": '#include "types.h"\nextern void g(Actor *arg0, s32 n);\n'}
    assert proposal.evidence[0] == "retyped arg0 as Actor from 2 landed calls (include/actor.h)"


def test_a_caller_type_lacking_a_field_gets_it_split_out_of_its_padding(monkeypatch):
    callers(monkeypatch, (ledger.Identity((PADDED,), PADDED.fields),), ["Box"])
    text = "void g(void *arg0, s32 n) {\n    M2C_FIELD(arg0, s32 *, 0x1C) = n;\n}\n"
    (proposal,) = rungs.retype_from_callers(None, "g", text)
    assert "void g(Box *arg0, s32 n) {" in proposal.text and "arg0->unk1C = n;" in proposal.text
    assert "s32 unk1C;" in proposal.extras["include/box.h"] and "g(Box *arg0" in proposal.extras["include/g.h"]


def test_no_callers_disagreeing_callers_or_arithmetic_on_the_base_propose_nothing(monkeypatch):
    for seen in ([], [None], ["Actor", "Wide"], ["s32"]):
        callers(monkeypatch, (ledger.Identity((ACTOR,), ACTOR.fields), ledger.Identity((WIDE,), WIDE.fields)), seen)
        assert rungs.retype_from_callers(None, "g", CALLEE) == []
    callers(monkeypatch, (ledger.Identity((ACTOR,), ACTOR.fields),), ["Actor"])
    moved = "void g(void *arg0, s32 n) {\n    M2C_FIELD(arg0, s32 *, 4) = n;\n    h(arg0 + 8);\n}\n"
    assert rungs.retype_from_callers(None, "g", moved) == []


def test_each_placeholder_left_is_classed_for_the_census(monkeypatch):
    callers(monkeypatch, (), [])
    text = "void g(void *arg0, Box *arg1) {\n    x = M2C_FIELD(arg0 + 4, s32 *, 0);\n}\n"
    rows = [ledger.Left("arg0 + 4", 0, "", (), False), ledger.Left("arg0", 4, "", (), False)]
    rows += [ledger.Left("arg1", 8, "Box", ("Box",), False), ledger.Left("D_1", 0, "", (), False)]
    assert [r.why for r in rungs.classify(None, "g", text, rows)] == [
        "expression base",
        "untyped parameter, no caller evidence",
        "typed base, no field or padding there",
        "untyped local or global, no evidence",
    ]


def test_type_of_reads_casts_pointer_locals_globals_and_addresses():
    declared = {"D_g": ("include/a.h", 1, "extern Actor *D_g;"), "D_s": ("include/a.h", 2, "extern Box D_s;")}
    local = {"p": "Wide"}
    assert [rungs._type_of(e, local, declared) for e in ("(Actor *) q", "p", "D_g", "&D_s", "q + 4", "q")] == [
        "Actor",
        "Wide",
        "Actor",
        "Box",
        None,
        None,
    ]


GAME = cparse.Layout("Game", "include/game.h", (cparse.Field("flags", 0, 4, 1, "i"),), size=4)
TARGET = cparse.Layout("Target", "include/target.h", (cparse.Field("hp", 0, 4, 1, "i"),), size=4)
SCREEN = cparse.Layout("Screen", "include/screen.h", (cparse.Field("flagged", 0x28C, 4, 1, "p", "Target *"),))
GLOBALS = {
    "gGame": ledger.Global("Game", 0, False),
    "gScreen": ledger.Global("Screen", 1, False),
    "gAny": ledger.Global("void", 1, False),
}
PROTOS = {
    "getTarget": ("include/t.h", 1, "extern Target *getTarget(void);"),
    "useTarget": ("include/t.h", 2, "extern void useTarget(s32 n, Target *t);"),
}


def asm(*lines):
    return "glabel caller\n" + "".join(f"    /* 0 0 0 */  {line}\n" for line in lines)


def program(monkeypatch, sources, callers):
    found = tuple(ledger.Identity((v,), v.fields) for v in (GAME, TARGET, SCREEN))
    get = SimpleNamespace(
        identities=found, globals={"include/g.h": (GLOBALS, ())}, declarations=PROTOS, callers=callers, extents={}
    )
    monkeypatch.setattr(index, "get", lambda snap: get)
    monkeypatch.setattr(index, "asm", lambda snap, name: sources[name])
    monkeypatch.setattr(rungs.layout, "unit_of", lambda snap, name: None)
    project = SimpleNamespace(root=Path("/nonexistent"), names_from="us")
    return SimpleNamespace(
        digest=str(id(sources)), config=SimpleNamespace(project=project), layout=SimpleNamespace(members={})
    )


@pytest.mark.parametrize(
    ("lines", "want"),
    [
        (("lui $a0, %hi(gGame)", "jal f", "addiu $a0, $a0, %lo(gGame)"), ("Game", "")),
        (("lui $at, %hi(gScreen)", "lw $a0, %lo(gScreen)($at)", "jal f", "nop"), ("Screen", "")),
        (("lw $v0, %lo(gScreen)($at)", "jal f", "lw $a0, 0x28C($v0)"), ("Target", "")),
        (("jal getTarget", "nop", "addu $a0, $v0, $zero", "jal f", "nop"), ("Target", "")),
        (
            ("lui $a0, %hi(gGame)", "addiu $a0, $a0, %lo(gGame)", "jal g", "nop", "jal f", "nop"),
            ("", "clobbered by a call"),
        ),
        (("lw $a0, %lo(gAny)($at)", "jal f", "nop"), ("", "global of no record type")),
        (("sll $a0, $a1, 2", "jal f", "nop"), ("", "computed")),
    ],
)
def test_a_caller_still_in_assembly_shows_the_record_its_argument_points_at(monkeypatch, lines, want):
    snap = program(monkeypatch, {"caller": asm(*lines)}, {"f": ("caller",)})
    assert rungs.asm_evidence(snap, "f", 0) == [want]


def test_a_caller_passing_its_own_parameter_on_gives_what_its_own_callers_pass_it(monkeypatch):
    sources = {
        "caller": asm("addu $s0, $a0, $zero", "jal f", "addu $a0, $s0, $zero"),
        "top": asm("lui $a0, %hi(gGame)", "jal caller", "addiu $a0, $a0, %lo(gGame)"),
    }
    snap = program(monkeypatch, sources, {"f": ("caller",), "caller": ("top",)})
    assert rungs.asm_evidence(snap, "f", 0) == [("Game", "")]
    lone = {"lone": asm("jal f", "nop")}
    snap = program(monkeypatch, lone, {"f": ("lone",)})
    assert rungs.asm_evidence(snap, "f", 0) == [("", "caller parameter (no caller)")]


def test_a_parameter_passed_as_it_is_to_a_callee_takes_the_callees_record_parameter_type(monkeypatch):
    program(monkeypatch, {}, {})
    text = "void f(void *arg0) {\n    useTarget(1, arg0);\n    useTarget(arg0, 0);\n    getTarget();\n}\n"
    assert rungs.passed(None, text, "arg0") == ["Target"]


TABLE = (
    "dlabel D_table\n"
    "    /* 0 80100000 0 */ .word f\n    /* 4 80100004 0 */ .word 0x10\n"
    "    /* 8 80100008 0 */ .word sibling\n    /* C 8010000C 0 */ .word 0x20\n"
    "enddlabel D_table\ndlabel D_outer\n    /* 10 80100010 0 */ .word D_table\nenddlabel D_outer\n"
)


def tabled(monkeypatch, tmp_path, sources, uses, sibling_proto=""):
    data = tmp_path / "asm" / "us" / "data"
    data.mkdir(parents=True)
    (data / "t.data.s").write_text(TABLE)
    snap = program(monkeypatch, sources, {})
    get = index.get(None)
    get.uses = uses
    if sibling_proto:
        get.declarations = {**PROTOS, "sibling": ("include/s.h", 1, sibling_proto)}
    members = {n: SimpleNamespace(kind="function") for n in ("f", "sibling", "reader")}
    project = SimpleNamespace(root=tmp_path, names_from="us")
    snap.config, snap.layout = SimpleNamespace(project=project), SimpleNamespace(members=members)
    return snap


def test_a_table_entry_takes_the_type_a_sibling_in_its_column_declares(monkeypatch, tmp_path):
    snap = tabled(monkeypatch, tmp_path, {}, {}, "extern void sibling(Game *g);")
    assert ("Game", "") in rungs.asm_evidence(snap, "f", 0)


def test_a_table_entry_takes_what_reaches_the_argument_at_the_indirect_call_of_a_reader_of_its_table(
    monkeypatch, tmp_path
):
    reader = asm("lui $a0, %hi(gGame)", "jalr $t9", "addiu $a0, $a0, %lo(gGame)")
    snap = tabled(monkeypatch, tmp_path, {"reader": reader}, {"D_outer": (("reader", "lw", 0),)})
    assert rungs.asm_evidence(snap, "f", 0) == [("Game", "")]  # read through the table that points at it
    snap = tabled(monkeypatch, tmp_path / "none", {}, {})
    assert rungs.asm_evidence(snap, "f", 0) == [("", "table read by no function")]


def test_the_resolve_rung_names_what_the_resolver_can_in_a_candidate_an_earlier_tool_left(monkeypatch):
    world(monkeypatch, ACTOR)
    (proposal,) = rungs.resolve(None, "f", "void f(Actor *arg0) {\n    x = M2C_FIELD(arg0, s32 *, 4);\n}\n")
    assert "x = arg0->hp;" in proposal.text and proposal.evidence == ("resolved 1 M2C_FIELD of the candidate",)
    assert rungs.resolve(None, "f", TEXT) == []
