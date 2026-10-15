"""Dispatch groups and their records, on shapes of a real census: table columns, passed-on objects, accesses."""

from types import SimpleNamespace

import pytest

from recipemaker import cparse, dispatch, index, ledger, rungs


def asm(name, *lines):
    return f"nonmatching {name}, 0x10\n\nglabel {name}\n" + "".join(f"    /* 0 0 0 */  {line}\n" for line in lines)


def code(*lines):
    return dispatch.instructions(asm("f", *lines))


def shapes(flow):
    return [(a.offset, a.width, a.kind, a.sign) for a in flow.accesses]


def test_the_object_is_followed_through_copies_saved_registers_and_calls():
    found = dispatch.flow(
        code(
            "addiu $sp, $sp, -0x18",
            "addu $s0, $a0, $zero",  # kept across the call
            "lh $a0, 0x18($a0)",  # loaded through itself: $a0 holds the object no more
            "jal func_800E4A18",
            "addu $a1, $s0, $zero",  # the delay slot passes it as the second argument
            "lwc1 $f0, 0x20($s0)",
            "lw $v1, 0xC($s0)",  # a word used as an address below: a pointer member
            "sb $zero, 0x2BA($v1)",
            "lw $a0, 0x10($a0)",  # $a0 died at the call: no access of the object
            "jr $ra",
            "sw $zero, 0x44($s0)",
        ),
        0,
        "f",
    )
    assert shapes(found) == [(0x18, 2, "i", "s"), (0x20, 4, "f", ""), (0xC, 4, "p", ""), (0x44, 4, "i", "")]
    assert found.passes == (("func_800E4A18", 1),)


def test_a_register_loaded_through_itself_records_the_access_and_stops_holding_the_object():
    found = dispatch.flow(code("lhu $a0, 0x1A($a0)", "jal func_800E4A18", "nop", "sw $zero, 4($a0)"), 0)
    assert shapes(found) == [(0x1A, 2, "i", "u")] and found.passes == ()


def test_an_overwritten_register_and_an_unaligned_word_access_are_what_they_are():
    found = dispatch.flow(code("addiu $a0, $a0, 0x10", "lw $v0, 0($a0)"), 0)
    assert found.accesses == ()
    assert shapes(dispatch.flow(code("lwl $v0, 1($a1)"), 1)) == [(1, 0, "i", "")]


REAL = [
    dispatch.Access(0, 4, "p", "", "func_800E70B0"),
    dispatch.Access(4, 4, "p", "", "func_800E70B0"),
    dispatch.Access(0xC, 4, "p", "", "func_800E7394"),
    dispatch.Access(0x18, 2, "i", "s", "func_800E7458"),
    dispatch.Access(0x1A, 2, "i", "u", "func_800E6C5C"),
    dispatch.Access(0x1C, 1, "i", "", "func_800E6C80"),
    dispatch.Access(0x20, 4, "f", "", "func_800E6C14"),
    dispatch.Access(0x40, 4, "i", "", "func_800E99D4"),
    dispatch.Access(0x44, 4, "i", "", "func_800E99D4"),
]


def test_the_record_holds_every_access_by_offset_with_placeholder_bytes_between():
    made = dispatch.record("Obj801358C0", REAL, {0x1C: "u8"})
    assert isinstance(made, dispatch.Record)
    assert made.text() == (
        "typedef struct Obj801358C0 {\n    void *unk0;\n    void *unk4;\n    u8 unk_8[0x4];\n    void *unkC;\n"
        "    u8 unk_10[0x8];\n    s16 unk18;\n    u16 unk1A;\n    u8 unk1C;\n    u8 unk_1D[0x3];\n    f32 unk20;\n"
        "    u8 unk_24[0x1C];\n    s32 unk40;\n    s32 unk44;\n} Obj801358C0;\n"
    )
    layout = cparse._layouts({"include/a.h": cparse.scan(made.text())})[0]
    assert [(f.name, f.offset) for f in layout.fields if not f.name.startswith("unk_")] == [
        ("unk0", 0),
        ("unk4", 4),
        ("unkC", 0xC),
        ("unk18", 0x18),
        ("unk1A", 0x1A),
        ("unk1C", 0x1C),
        ("unk20", 0x20),
        ("unk40", 0x40),
        ("unk44", 0x44),
    ]


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        (dispatch.Access(0x18, 4, "i", "", "func_800E9000"), "func_800E7458 and func_800E9000 access 0x18 as 2 and 4"),
        (dispatch.Access(0x40, 4, "f", "", "func_800E9000"), "func_800E9000 reads 0x40 as a float and func_800E99D4"),
        (dispatch.Access(0x18, 2, "i", "u", "func_800E9000"), "func_800E7458 loads 0x18 signed and func_800E9000"),
        (dispatch.Access(0x19, 1, "i", "u", "func_800E9000"), "func_800E7458 and func_800E9000 access overlapping"),
        (dispatch.Access(0x22, 4, "i", "", "func_800E9000"), "func_800E9000 accesses 0x22 (4 bytes) where no member"),
        (dispatch.Access(0x30, 0, "i", "", "func_800E9000"), "func_800E9000 reads 0x30 by unaligned word access"),
        (dispatch.Access(-4, 4, "i", "", "func_800E9000"), "func_800E9000 reads below the object"),
    ],
)
def test_functions_disagreeing_about_the_object_refuse_the_record_naming_both(extra, why):
    made = dispatch.record("Obj801358C0", [*REAL, extra])
    assert isinstance(made, str) and made.startswith(why)


DATA = (
    "dlabel D_auto_80137A3C\n"
    "    /* 0 80137A3C 0 */ .word func_800EDB20\n    /* 4 80137A40 0 */ .word 0x00090000\n"
    "    /* 8 80137A44 0 */ .word 0x00000000\n    /* C 80137A48 0 */ .word func_800EDA98\n"
    "    /* 10 80137A4C 0 */ .word 0x00800000\n    /* 14 80137A50 0 */ .word 0x00000000\n"
    "    /* 18 80137A54 0 */ .word func_800EDB20\n    /* 1C 80137A58 0 */ .word 0x0009FFD0\n"
    "enddlabel D_auto_80137A3C\n"
    "dlabel D_auto_80135A54\n"
    "    /* 0 80135A54 0 */ .word func_800EDB20\n    /* 4 80135A58 0 */ .word func_800E9A30\n"
    "    /* 8 80135A5C 0 */ .word func_800E9D80\n    /* C 80135A60 0 */ .word func_800E9A54\n"
    "enddlabel D_auto_80135A54\n"
    "dlabel D_auto_80136000\n"
    "    /* 0 80136000 0 */ .word func_800F0000\n    /* 4 80136004 0 */ .word func_800F0100\n"
    "enddlabel D_auto_80136000\n"
)
FUNCTIONS = ("func_800EDB20", "func_800EDA98", "func_800E9A30", "func_800E9D80", "func_800E9A54", "helper", "lone")
FUNCTIONS += ("func_800F0000", "func_800F0100", "caller")


def project(monkeypatch, tmp_path, sources, callers=None, declarations=None, groups=None):
    data = tmp_path / "asm" / "us" / "data"
    data.mkdir(parents=True)
    (data / "t.data.s").write_text(DATA)
    members = {n: SimpleNamespace(kind="function", group="sys") for n in FUNCTIONS}
    texts = {n: asm(n, *lines) for n, lines in sources.items()}
    get = SimpleNamespace(
        callers=callers or {}, declarations=declarations or {}, globals=groups or {}, identities=(), extents={}
    )
    monkeypatch.setattr(index, "get", lambda snap: get)
    monkeypatch.setattr(index, "asm", lambda snap, name: texts.get(name, asm(name)))
    modules = {"sys": SimpleNamespace(header="include/game/sys.h")}
    return SimpleNamespace(
        digest=str(tmp_path),
        config=SimpleNamespace(project=SimpleNamespace(root=tmp_path, names_from="us")),
        layout=SimpleNamespace(members=members, groups=modules),
    )


def test_a_column_shared_by_two_tables_and_the_callees_its_entries_pass_the_object_to_form_one_group(
    monkeypatch, tmp_path
):
    sources = {"func_800E9A30": ("addu $s0, $a0, $zero", "jal helper", "addu $a0, $s0, $zero")}
    snap = project(monkeypatch, tmp_path, sources, callers={"helper": ("func_800E9A30",)})
    found = dispatch.group(snap, "func_800EDA98", 0)
    assert found == dispatch.Group(
        "Obj80135A54",
        (
            ("func_800E9A30", 0),
            ("func_800E9A54", 0),
            ("func_800E9D80", 0),
            ("func_800EDA98", 0),
            ("func_800EDB20", 0),
            ("helper", 0),
        ),
    )
    assert dispatch.group(snap, "helper", 0) == found  # a callee finds the same group through its callers
    assert dispatch.group(snap, "func_800EDA98", 1).name == "Obj80135A54Arg1"


def test_a_callee_two_groups_pass_their_objects_to_joins_them_and_nothing_reaches_a_function_no_table_holds(
    monkeypatch, tmp_path
):
    passing = ("jal helper", "nop")
    sources = {"func_800E9A30": passing, "func_800F0000": passing}
    snap = project(monkeypatch, tmp_path, sources, callers={"helper": ("func_800E9A30", "func_800F0000")})
    found = dispatch.group(snap, "func_800F0100", 0)
    assert isinstance(found, dispatch.Group) and found.name == "Obj80135A54"
    assert {"func_800EDA98", "func_800F0000", "func_800F0100", "helper"} <= {n for n, _ in found.nodes}
    assert dispatch.group(snap, "lone", 0) is None


def test_a_walk_past_the_limit_is_refused(monkeypatch, tmp_path):
    snap = project(monkeypatch, tmp_path, {"func_800E9A30": ("jal helper", "nop")})
    monkeypatch.setattr(dispatch, "_LIMIT", 3)
    assert dispatch.group(snap, "func_800EDA98", 0) == "its group passes the object on to more than 3 functions"


def test_the_header_is_the_most_specific_one_every_group_function_reaches(monkeypatch, tmp_path):
    groups = {
        "include/game/sys.h": ({}, ("include/types.h",)),
        "include/game/other.h": ({}, ("include/types.h",)),
        "include/types.h": ({}, ()),
    }
    declared = {"lone": ("include/game/other.h", 1, "extern void lone(s32);")}
    snap = project(monkeypatch, tmp_path, {}, declarations=declared, groups=groups)
    assert dispatch.header(snap, [("func_800EDA98", 0), ("func_800EDB20", 0)]) == "include/game/sys.h"
    assert dispatch.header(snap, [("func_800EDA98", 0), ("lone", 0)]) == "include/types.h"
    snap.layout.groups = {}
    assert dispatch.header(snap, [("caller", 0)]) == ""


RECORD = dispatch.Record("Obj80137A3C", ((0x12, 2, "u16"),))
SYS_H = (
    '#ifndef SYS_H\n#define SYS_H\n#include "types.h"\nextern s32 D_1;\n'
    'extern s32 func_800EDA98(s32, u32);\n#include "late.h"\n#endif\n'
)


def test_the_record_is_defined_before_the_first_group_prototype_else_at_the_end_inside_the_guard():
    defined = dispatch.define(SYS_H, RECORD, ["func_800EDB20", "func_800EDA98"])
    assert defined.index("typedef struct Obj80137A3C {") < defined.index("extern s32 func_800EDA98(")
    assert defined.index("extern s32 D_1;") < defined.index("typedef struct Obj80137A3C {")
    types = "#ifndef T_H\n#define T_H\ntypedef unsigned short u16;\n#endif\n"
    assert dispatch.define(types, RECORD, ["func_800EDA98"]) == (
        "#ifndef T_H\n#define T_H\ntypedef unsigned short u16;\n" + RECORD.text() + "#endif\n"
    )
    assert dispatch.define(defined, RECORD, ["func_800EDA98"]) == defined


DRAFT = (
    '#include "types.h"\ns32 func_800EDA98(s32 arg0, u32 arg1) {\n'
    "    return (u16) M2C_FIELD(arg0, u16 *, 0x12) >= (u32) (arg1 >> 0x10);\n}\n"
)
GROUP = dispatch.Group("Obj80137A3C", (("func_800EDB20", 0), ("func_800EDA98", 0)))


def drafting(monkeypatch, found, accesses, sibling=None, identities=()):
    declared = {"func_800EDA98": ("include/sys.h", 5, "extern s32 func_800EDA98(s32, u32);")}
    get = SimpleNamespace(identities=identities, extents={}, globals={}, addresses={}, declarations=declared)
    monkeypatch.setattr(index, "get", lambda snap: get)
    monkeypatch.setattr(rungs.draft, "header_texts", lambda snap: {"include/sys.h": SYS_H})
    monkeypatch.setattr(rungs, "evidence", lambda snap, member, position: [None])
    monkeypatch.setattr(rungs, "_sibling_type", lambda snap, name, position: (sibling or {}).get(name, ""))
    monkeypatch.setattr(dispatch, "group", lambda snap, member, position: found)
    monkeypatch.setattr(dispatch, "header", lambda snap, nodes: "include/sys.h")
    monkeypatch.setattr(dispatch, "flows", lambda snap, node: dispatch.Flow(tuple(accesses.get(node[0], ())), ()))


def test_an_untyped_parameter_of_a_dispatch_group_gets_the_record_built_from_every_group_function(monkeypatch):
    drafting(monkeypatch, GROUP, {"func_800EDB20": [dispatch.Access(0x12, 2, "i", "u", "func_800EDB20")]})
    (proposal,) = rungs.group_record(None, "func_800EDA98", DRAFT)
    assert "s32 func_800EDA98(Obj80137A3C *arg0, u32 arg1) {" in proposal.text
    assert "return (u16) arg0->unk12 >= (u32) (arg1 >> 0x10);" in proposal.text
    assert '#include "sys.h"' in proposal.text
    header = proposal.extras["include/sys.h"]
    assert "typedef struct Obj80137A3C {\n    u8 unk_0[0x12];\n    u16 unk12;\n} Obj80137A3C;\n" in header
    assert "extern s32 func_800EDA98(Obj80137A3C *, u32);" in header
    assert header.index("} Obj80137A3C;") < header.index("extern s32 func_800EDA98(")
    assert proposal.evidence == (
        "retyped arg0 as Obj80137A3C, a record of 1 members from the accesses of the 2 functions of its dispatch "
        "group (include/sys.h)",
    )


def test_a_group_whose_functions_disagree_proposes_nothing_and_the_census_names_both(monkeypatch):
    drafting(monkeypatch, GROUP, {"func_800EDB20": [dispatch.Access(0x12, 4, "i", "", "func_800EDB20")]})
    assert rungs.group_record(None, "func_800EDA98", DRAFT) == []
    (row,) = rungs.classify(None, "func_800EDA98", DRAFT, [ledger.Left("arg0", 0x12, "", (), False)])
    assert row.why == (
        "untyped parameter, dispatch group Obj80137A3C refused: func_800EDA98 and func_800EDB20 access 0x12 as 2 and"
        " 4 bytes"
    )


@pytest.mark.parametrize(
    ("found", "why"),
    [
        (None, "untyped parameter, no caller evidence"),
        ("its group passes the object on to more than 3 functions", "untyped parameter, dispatch group refused: its"),
    ],
)
def test_no_group_or_a_refused_group_proposes_nothing(monkeypatch, found, why):
    drafting(monkeypatch, found, {})
    assert rungs.group_record(None, "func_800EDA98", DRAFT) == []
    (row,) = rungs.classify(None, "func_800EDA98", DRAFT, [ledger.Left("arg0", 0x12, "", (), False)])
    assert row.why.startswith(why)


RESET = cparse.Layout(
    "struct ResetState",
    "include/reset.h",
    (cparse.Field("field40", 0x40, 4, 1, "i", "int"),),
    (cparse.Field("pad", 0, 1, 0x40, "i", "char"),),
)
RESET_H = "struct ResetState {\n    char pad[0x40];\n    int field40;\n};\n"


def test_a_group_reaching_a_record_a_header_defines_uses_it_and_splits_what_it_lacks_out_of_its_padding(monkeypatch):
    identity = ledger.Identity((RESET,), RESET.fields)
    drafting(monkeypatch, GROUP, {}, {"func_800EDB20": "ResetState"}, (identity,))
    texts = {"include/sys.h": SYS_H, "include/reset.h": RESET_H}
    monkeypatch.setattr(rungs.draft, "header_texts", lambda snap: texts)
    (proposal,) = rungs.group_record(None, "func_800EDA98", DRAFT)
    assert "s32 func_800EDA98(struct ResetState *arg0, u32 arg1) {" in proposal.text
    assert "arg0->unk12" in proposal.text and '#include "reset.h"' in proposal.text
    assert "u16 unk12;" in proposal.extras["include/reset.h"]
    assert "extern s32 func_800EDA98(struct ResetState *, u32);" in proposal.extras["include/sys.h"]
    assert proposal.evidence[0] == (
        "retyped arg0 as struct ResetState, the record of dispatch group Obj80137A3C (include/reset.h)"
    )
    two = {"func_800EDB20": "ResetState", "func_800EDA98": "ResetState2"}
    other = cparse.Layout("ResetState2", "include/reset2.h", RESET.fields)
    drafting(monkeypatch, GROUP, {}, two, (identity, ledger.Identity((other,), other.fields)))
    assert rungs.group_record(None, "func_800EDA98", DRAFT) == []


def test_a_type_only_a_struct_tag_names_is_found_in_its_header():
    assert rungs._view((ledger.Identity((RESET,), RESET.fields),), "ResetState")[1] is RESET


def test_each_table_column_says_where_it_starts(monkeypatch, tmp_path):
    snap = project(monkeypatch, tmp_path, {})
    columns, _ = dispatch.tables(snap)
    assert ("D_auto_80137A3C", ("func_800EDB20",), 0x80137A3C) in columns["func_800EDA98"]
    assert ("D_auto_80135A54", ("func_800E9A30", "func_800E9D80", "func_800E9A54"), 0x80135A54) in columns[
        "func_800EDB20"
    ]


def test_accesses_through_a_pointer_member_are_kept_by_the_member_they_came_through():
    found = dispatch.flow(code("lw $v1, 0xC($a0)", "sb $zero, 0x2BA($v1)", "lhu $v0, 4($v1)"), 0, "f")
    assert [(at, a.offset, a.width, a.sign) for at, a in found.pointees] == [(0xC, 0x2BA, 1, ""), (0xC, 4, 2, "u")]


def test_a_void_pointer_member_whose_pointee_the_group_agrees_on_points_at_its_own_record(monkeypatch):
    drafting(monkeypatch, GROUP, {})
    flows = {
        "func_800EDB20": dispatch.Flow(
            (dispatch.Access(0x12, 2, "i", "u", "func_800EDB20"), dispatch.Access(4, 4, "p", "", "func_800EDB20")),
            (),
            ((4, dispatch.Access(0x20, 4, "f", "", "func_800EDB20")),),
        )
    }
    monkeypatch.setattr(dispatch, "flows", lambda snap, node: flows.get(node[0], dispatch.Flow((), ())))
    (proposal,) = rungs.group_record(None, "func_800EDA98", DRAFT)
    header = proposal.extras["include/sys.h"]
    assert "typedef struct Obj80137A3CPtr4 {\n    u8 unk_0[0x20];\n    f32 unk20;\n} Obj80137A3CPtr4;\n" in header
    assert "    Obj80137A3CPtr4 *unk4;\n" in header
    assert header.index("} Obj80137A3CPtr4;") < header.index("typedef struct Obj80137A3C {")
    assert proposal.evidence[1].startswith("typed its member at 0x4 as Obj80137A3CPtr4 *")


GLOBAL_H = '#ifndef G_H\n#define G_H\n#include "types.h"\nextern void *D_auto_80135834;\nextern s32 D_1;\n#endif\n'
GLOBAL_DRAFT = (
    '#include "g.h"\nvoid func_800E097C(s32 arg0) {\n    M2C_FIELD(D_auto_80135834, s32 *, 0x20) = arg0;\n}\n'
)
POINTEE = (
    "glabel f\n    /* 0 0 0 */  lui $v0, %hi(D_auto_80135834)\n"
    "    /* 0 0 0 */  lw $v0, %lo(D_auto_80135834)($v0)\n    /* 0 0 0 */  lhu $v1, 8($v0)\n"
    "    /* 0 0 0 */  jr $ra\n    /* 0 0 0 */  sw $v1, 0x20($v0)\n"
)


def globals_world(monkeypatch, declared, sources=None, uses=None, owner="include/g.h"):
    group = SimpleNamespace(header=owner)
    placed = SimpleNamespace(version="us", vram=0x80100000, size=8)
    layout = SimpleNamespace(
        members={"D_1": SimpleNamespace(group="m", placements=(placed,))},
        groups={"m": group},
    )
    texts = {
        "include/g.h": declared,
        **({owner: "#ifndef OWN_H\n#define OWN_H\n#endif\n"} if owner != "include/g.h" else {}),
    }
    get = SimpleNamespace(
        identities=(),
        extents={},
        addresses={"D_auto_80135834": 0x80135834, "D_1": 0x80100000},
        declarations={},
        globals={"include/g.h": (ledger.globals_in(declared, "include/g.h"), ())},
        uses=uses or {"D_auto_80135834": (("f", "lw", 0),)},
        callers={},
    )
    monkeypatch.setattr(index, "get", lambda snap: get)
    monkeypatch.setattr(index, "asm", lambda snap, name: POINTEE)
    monkeypatch.setattr(index, "source", lambda snap, path: (sources or {}).get(path, ""))
    monkeypatch.setattr(rungs.cparse, "landed_sources", lambda snap: sorted(sources or {}))
    monkeypatch.setattr(rungs.draft, "header_texts", lambda snap: texts)
    config = SimpleNamespace(project=SimpleNamespace(names_from="us"))
    return SimpleNamespace(digest=str(id(get)), layout=layout, config=config)


def test_a_void_pointer_global_points_at_one_record_built_from_every_function_reading_through_it(monkeypatch):
    snap = globals_world(monkeypatch, GLOBAL_H)
    (proposal,) = rungs.global_record(snap, "func_800E097C", GLOBAL_DRAFT)
    assert "    D_auto_80135834->unk20 = arg0;" in proposal.text
    header = proposal.extras["include/g.h"]
    assert (
        "typedef struct Data80135834Ptr {\n    u8 unk_0[0x8];\n    u16 unk8;\n    u8 unk_A[0x16];\n    s32 unk20;\n"
        "} Data80135834Ptr;\nextern Data80135834Ptr *D_auto_80135834;\n" in header
    )
    assert "extern void *D_auto_80135834;" not in header
    assert proposal.evidence[0].startswith("retyped D_auto_80135834 as Data80135834Ptr *, a record of 2 members")


def test_a_scalar_global_read_by_offset_becomes_one_record_and_its_declaration_names_it(monkeypatch):
    snap = globals_world(monkeypatch, GLOBAL_H, uses={"D_1": (("f", "lw", 0), ("g", "sh", 6))})
    text = '#include "g.h"\nvoid h(void) {\n    M2C_FIELD(&D_1, s16 *, 6) = 0;\n}\n'
    (proposal,) = rungs.global_record(snap, "h", text)
    assert "    D_1.unk6 = 0;" in proposal.text
    assert "} Data80100000;\nextern Data80100000 D_1;\n" in proposal.extras["include/g.h"]


@pytest.mark.parametrize(
    ("declared", "sources", "why"),
    [
        (GLOBAL_H, {"src/a.c": "s32 f(void) {\n    return D_1;\n}\n"}, "global, landed code reads it as the s32"),
        ('#include "types.h"\nextern f32 D_1;\n', {}, "global, declared f32, not a scalar"),
    ],
)
def test_a_global_landed_code_reads_as_its_scalar_or_no_header_declares_is_left_and_named(
    monkeypatch, declared, sources, why
):
    snap = globals_world(monkeypatch, declared, sources, {"D_1": (("f", "lw", 0),)})
    text = '#include "g.h"\nvoid h(void) {\n    M2C_FIELD(&D_1, s16 *, 6) = 0;\n}\n'
    assert rungs.global_record(snap, "h", text) == []
    (row,) = rungs.classify(snap, "h", text, [ledger.Left("&D_1", 6, "", (), False)])
    assert row.why.startswith(why)


def test_a_pointer_global_landed_code_adds_to_is_left_and_named(monkeypatch):
    sources = {"src/a.c": "void *f(void) {\n    return D_auto_80135834 + 0xD8;\n}\n"}
    snap = globals_world(monkeypatch, GLOBAL_H, sources)
    assert rungs.global_record(snap, "func_800E097C", GLOBAL_DRAFT) == []
    left = [ledger.Left("D_auto_80135834", 0x20, "", (), False)]
    (row,) = rungs.classify(snap, "func_800E097C", GLOBAL_DRAFT, left)
    assert row.why.startswith("global, landed code does arithmetic on it")


def test_a_global_no_header_declares_gets_its_record_and_declaration_in_the_owner_units_header(monkeypatch):
    snap = globals_world(monkeypatch, '#include "types.h"\n', uses={"D_1": (("f", "lw", 0),)}, owner="include/own.h")
    text = '#include "g.h"\nextern s32 D_1;\nvoid h(void) {\n    M2C_FIELD(&D_1, s16 *, 6) = 0;\n}\n'
    (proposal,) = rungs.global_record(snap, "h", text)
    assert "    D_1.unk6 = 0;" in proposal.text and '#include "own.h"' in proposal.text
    assert "extern s32 D_1;" not in proposal.text
    assert proposal.extras["include/own.h"].endswith("} Data80100000;\nextern Data80100000 D_1;\n#endif\n")
    assert proposal.evidence[0].startswith("declared D_1 as Data80100000, a record of 2 members")


FED = (
    '#include "g.h"\nvoid func_800E097C(s32 arg0) {\n    void *temp_v0;\n\n    temp_v0 = D_auto_80135834;\n'
    "    M2C_FIELD(temp_v0, s32 *, 0x20) = arg0;\n}\n"
)


def test_a_local_holding_a_symbols_value_shares_the_symbols_record(monkeypatch):
    snap = globals_world(monkeypatch, GLOBAL_H)
    (proposal,) = rungs.global_record(snap, "func_800E097C", FED)
    assert "    Data80135834Ptr *temp_v0;" in proposal.text
    assert "    temp_v0->unk20 = arg0;" in proposal.text
    assert "extern Data80135834Ptr *D_auto_80135834;\n" in proposal.extras["include/g.h"]
    assert proposal.evidence[0].endswith("; typed temp_v0 as Data80135834Ptr *")


def test_another_user_extends_the_symbols_own_record_with_the_members_it_shows(monkeypatch):
    declared = (
        '#include "types.h"\ntypedef struct Data80135834Ptr {\n    u8 unk_0[0x8];\n    u16 unk8;\n} Data80135834Ptr;\n'
        "extern Data80135834Ptr *D_auto_80135834;\n"
    )
    snap = globals_world(monkeypatch, declared)
    (proposal,) = rungs.global_record(snap, "func_800E097C", GLOBAL_DRAFT)
    assert "    D_auto_80135834->unk20 = arg0;" in proposal.text
    header = proposal.extras["include/g.h"]
    assert header.count("typedef struct Data80135834Ptr {") == 1 and "    s32 unk20;\n} Data80135834Ptr;" in header
    assert proposal.evidence[0].startswith("extended D_auto_80135834")


def test_users_disagreeing_at_an_offset_refuse_the_record_never_a_union(monkeypatch):
    snap = globals_world(monkeypatch, GLOBAL_H)
    text = GLOBAL_DRAFT.replace("s32 *, 0x20", "s32 *, 8")  # f loads 8 as a halfword
    assert rungs.global_record(snap, "func_800E097C", text) == []
    (row,) = rungs.classify(snap, "func_800E097C", text, [ledger.Left("D_auto_80135834", 8, "", (), False)])
    assert row.why == "global, f and the draft access 0x8 as 2 and 4 bytes"
    fed = FED.replace("s32 *, 0x20", "s32 *, 8")
    (row,) = rungs.classify(snap, "func_800E097C", fed, [ledger.Left("temp_v0", 8, "", (), False)])
    assert row.why == "local fed by D_auto_80135834, f and the draft access 0x8 as 2 and 4 bytes"


def test_a_local_fed_by_no_symbol_stays_a_placeholder_and_is_named(monkeypatch):
    snap = globals_world(monkeypatch, GLOBAL_H)
    text = FED.replace("temp_v0 = D_auto_80135834;", "temp_v0 = func_1();")
    assert rungs.global_record(snap, "func_800E097C", text) == []
    (row,) = rungs.classify(snap, "func_800E097C", text, [ledger.Left("temp_v0", 0x20, "", (), False)])
    assert row.why == "untyped local, not fed by one data symbol"


@pytest.mark.parametrize(
    ("symbol", "pointee", "name"),
    [
        ("D_auto_80135834", False, "Data80135834"),
        ("D_80135834", True, "Data80135834Ptr"),
        ("g_menuState", False, "DataMenuState"),
        ("s_cursor_pos", True, "DataCursorPosPtr"),
    ],
)
def test_a_symbols_record_is_named_from_the_symbol(symbol, pointee, name):
    assert dispatch.data_name(symbol, 0x80135834, pointee) == name
