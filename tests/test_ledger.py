"""The type ledger: identities, merged layouts, M2C_FIELD resolution without casts, and header patches."""

import pytest

from recipemaker import cparse as structs
from recipemaker import ledger

HEADER = """
typedef struct {
    char pad0[0x10];
    f32 x10;
    char pad14[0x20 - 0x14];
    s32 x20;
    s32 sp24;
} Pair;
"""


def field(name, offset, kind="i", size=4):
    return structs.Field(name, offset, size, 1, kind)


def view(name, *fields, path=None):
    return structs.Layout(name, path or f"include/{name}.h", tuple(fields))


A = view("ActorA", field("x0", 0), field("x4", 4), field("x8", 8), field("xC", 12))
B = view("ActorB", field("x0", 0), field("x4", 4), field("x8", 8), field("x20", 0x20))
OTHER = view("Other", field("q", 0, "f"), field("r", 4, "f"))
CLASH = view("Clash", field("x0", 0, "f"), field("x4", 4), field("x8", 8))


def test_views_sharing_three_equal_fields_are_one_identity_with_the_union_of_their_fields():
    found = ledger.identities([A, B, OTHER])
    joined = next(i for i in found if "ActorA" in i.names)
    assert set(joined.names) == {"ActorA", "ActorB"} and [f.offset for f in joined.fields] == [0, 4, 8, 12, 0x20]
    assert len(found) == 2


def test_evidence_joins_views_that_share_fewer_than_three_fields():
    a, b = view("A", field("x0", 0)), view("B", field("y4", 4))
    assert len(ledger.identities([a, b])) == 2
    assert len(ledger.identities([a, b], [("A", "B")])) == 1


def test_a_join_that_would_put_two_shapes_at_one_offset_is_not_made():
    found = ledger.identities([A, CLASH], [("ActorA", "Clash")])
    assert len(found) == 2


def test_resolve_writes_a_field_when_the_declared_type_has_one_there_and_never_casts():
    identity = ledger.identities([A, B])
    text = "void f(ActorA *arg0) {\n    x = M2C_FIELD(arg0, s32 *, 0x20);\n    y = M2C_FIELD(arg0, s32 *, 4);\n}\n"
    got = ledger.resolve(text, identity, {})
    assert got.resolved == 2 and got.left == ()
    assert "arg0->x20" in got.text and "arg0->x4" in got.text and "(ActorB *)" not in got.text


def test_resolve_leaves_a_placeholder_and_says_what_is_missing():
    identity = ledger.identities([A, OTHER])
    text = (
        "void f(ActorA *arg0, void *arg1) {\n    x = M2C_FIELD(arg0, s32 *, 0x40);\n"
        "    y = M2C_FIELD(arg1, s32 *, 0);\n}\n"
    )
    got = ledger.resolve(text, identity, {})
    assert got.resolved == 0 and got.text.count("M2C_FIELD") == 2
    first, second = got.left
    assert (first.type, first.offset, first.elsewhere) == ("ActorA", 0x40, False)
    assert second.type == "" and second.identity == () and second.candidates == ("ActorA",)


def test_a_use_whose_size_or_kind_disagrees_with_the_field_is_left():
    identity = ledger.identities([A])
    got = ledger.resolve("void f(ActorA *p) { x = M2C_FIELD(p, f32 *, 4); }\n", identity, {})
    assert got.resolved == 0 and len(got.left) == 1


def test_elsewhere_is_true_when_another_view_of_the_identity_has_a_field_at_that_offset():
    a = view("A", field("x0", 0), field("x4", 4), field("x8", 8), field("w", 12, "f"))
    identity = ledger.identities([a, B])
    got = ledger.resolve("void f(A *p) { x = M2C_FIELD(p, s32 *, 12); }\n", identity, {})
    assert got.left[0].elsewhere is True


def test_uses_reads_the_offset_size_and_kind():
    found = ledger.uses("a = M2C_FIELD(p, f32 *, 0x10) + M2C_FIELD(q->r, s32, 8);")
    assert [(u.base, u.offset, u.size, u.kind) for u in found] == [("p", 0x10, 4, "f"), ("q->r", 8, 4, "i")]


def test_shared_pairs_are_the_views_with_three_or_more_equal_fields_and_nothing_else():
    shapes = ledger.view_shapes([A, B, OTHER, CLASH])
    assert ledger.shared_pairs(shapes) == [(0, 1)]  # CLASH shares only x4 and x8 with either


def test_a_group_with_a_clash_is_never_joined_and_a_join_that_would_clash_is_not_made():
    c = view("ActorC", field("x0", 0, "f"), field("x4", 4), field("x8", 8), field("x10", 0x10))
    assert [len(i.views) for i in ledger.identities([A, B, c])] == [2, 1]


def test_a_type_smaller_than_the_offsets_used_is_never_suggested():
    identity = ledger.identities([A, OTHER])
    text = "void f(void *arg1) {\n    y = M2C_FIELD(arg1, s32 *, 0);\n    z = M2C_FIELD(arg1, s32 *, 0x28);\n}\n"
    got = ledger.resolve(text, identity, {})
    assert all(row.candidates == () for row in got.left)
    near = ledger.resolve("void f(void *arg1) {\n    y = M2C_FIELD(arg1, s32 *, 0xC);\n}\n", identity, {})
    assert near.left[0].candidates == ("ActorA",) and ledger.extent(identity[0]) == 16


def test_a_prototype_above_the_function_declares_no_type_for_the_functions_own_names():
    text = "void g(void *arg0);\nvoid f(Actor *arg0) {\n    Item *p;\n}\n"
    assert ledger.declared_types(text) == {"arg0": "Actor", "p": "Item"}
    assert ledger.declared_types("Actor *a;\n") == {"a": "Actor"}  # a text with no function is read whole


# Bases typed beyond the function's own declarations: the shapes the held RW and BT drafts showed.

SLOT = structs.Layout(
    "Slot", "include/game.h", (field("unk0", 0), field("unk20", 0x20, "p")), size=0x96 - 2, aliases=()
)
GAME = structs.Layout(
    "struct Game",
    "include/game.h",
    (field("flags", 0), field("buttons", 0x10), structs.Field("bytes", 0x14, 1, 4, "i", "u8")),
    records=(structs.Field("players", 0x58, 0x94, 4, "s", "Slot"),),
    size=0x58 + 4 * 0x94,
    aliases=("Game",),
)
REPLY = structs.Layout("Reply", "include/reply.h", (field("first", 0), field("second", 4)), size=8)
SCREEN = structs.Layout(
    "Screen",
    "include/screen.h",
    (
        structs.Field("window", 0x20, 4, 1, "p", "void *"),
        structs.Field("flagged", 0x28C, 4, 1, "p", "Target *"),
        structs.Field("words", 0x18, 4, 1, "p", "s32 *"),
    ),
    size=0x290,
)
TARGET = structs.Layout("Target", "include/target.h", (field("unk100", 0x100), field("x360", 0x360)), size=0x364)
KNOWN = tuple(ledger.Identity((v,), v.fields) for v in (SLOT, GAME, REPLY, SCREEN, TARGET))


def scope(**names):
    rows = {k: v for k, v in names.items() if isinstance(v, ledger.Global)}
    return ledger.Scope(rows, {"gReply": 0x80154020, "gStray": 0x80154024, "gGame": 0x801462C8})


@pytest.mark.parametrize(
    ("text", "names", "want"),
    [
        (  # a pointer global an included header declares (RW D_800E5830)
            "void f(void) {\n    M2C_FIELD(gScreen, void **, 0x20) = 0;\n}\n",
            {"gScreen": ledger.Global("Screen", 1, False, "include/screen.h")},
            "gScreen->window = 0;",
        ),
        (  # the address of a record global (RW &D_801462C8)
            "void f(void) {\n    g(M2C_FIELD(&gGame, s32 *, 0x10));\n}\n",
            {"gGame": ledger.Global("Game", 0, False, "include/game.h")},
            "g(gGame.buttons);",
        ),
        (  # a member of an element of a nested record array
            "void *f(Game *arg1) {\n    return M2C_FIELD(arg1, Target **, 0x78);\n}\n",
            {},
            "return arg1->players[0].unk20;",
        ),
        (  # an element of a scalar array member
            "s32 f(Game *arg1) {\n    return M2C_FIELD(arg1, u8 *, 0x16);\n}\n",
            {},
            "return arg1->bytes[2];",
        ),
        (  # a symbol inside an enclosing record global (RW &D_80154024 inside D_80154020)
            "void f(void) {\n    M2C_FIELD(&gStray, s32 *, -4) = -1;\n}\n",
            {"gReply": ledger.Global("Reply", 0, False, "include/reply.h"), "gStray": ledger.Global("s32", 0, False)},
            "gReply.first = -1;",
        ),
        (  # a word read through a pointer-to-scalar member, as an index of it
            "s32 f(Screen *arg0) {\n    return M2C_FIELD(arg0->words, s32 *, 4);\n}\n",
            {},
            "return arg0->words[1];",
        ),
        (  # past the record's end: a later element of what the pointer points at (BT Gfx writes, temp_v0[1])
            "void f(Reply *arg0) {\n    M2C_FIELD(arg0, s32 *, 0xC) = 1;\n}\n",
            {},
            "arg0[1].second = 1;",
        ),
        (  # a nested M2C_FIELD: the inner member's pointer type types the outer base (BT func_800E7448 shape)
            "s32 f(Screen *arg0) {\n    M2C_FIELD(M2C_FIELD(arg0, void **, 0x28C), s32 *, 0x360) = 0;\n}\n",
            {},
            "arg0->flagged->x360 = 0;",
        ),
    ],
)
def test_a_base_typed_by_what_the_draft_sees_names_its_member(text, names, want):
    got = ledger.resolve(text, KNOWN, {}, scope(**names))
    assert want in got.text and "M2C_FIELD" not in got.text and got.left == ()


def test_a_symbol_inside_a_record_global_of_a_header_the_draft_lacks_names_it_and_includes_that_header():
    found = ledger.Scope(
        {"gStray": ledger.Global("s32", 0, False)},
        {"gReply": 0x80154020, "gStray": 0x80154024},
        {"gReply": ledger.Global("Reply", 0, False, "include/reply.h")},
    )
    text = '#include "a.h"\nvoid f(void) {\n    M2C_FIELD(&gStray, s32 *, 0) = 0;\n}\n'
    got = ledger.resolve(text, KNOWN, {}, found)
    assert '#include "a.h"\n#include "reply.h"\n' in got.text and "gReply.second = 0;" in got.text


def test_a_void_local_every_assignment_gives_one_record_pointer_is_declared_that_type():
    text = (
        "void f(Screen *arg0) {\n    void *temp;\n    temp = M2C_FIELD(arg0, void **, 0x28C);\n"
        "    if (temp != NULL) {\n        x = M2C_FIELD(temp, s32 *, 0x100);\n    }\n}\n"
    )
    got = ledger.resolve(text, KNOWN, {})
    assert "    Target *temp;\n    temp = arg0->flagged;\n" in got.text and "x = temp->unk100;" in got.text
    assert got.resolved == 2 and got.left == ()


@pytest.mark.parametrize(
    "extra",
    [
        "    temp = arg0;\n",  # a second assignment of another type
        "    y = temp + 4;\n",  # pointer arithmetic the new type would scale
    ],
)
def test_a_void_local_with_disagreeing_assignments_or_arithmetic_keeps_its_type(extra):
    text = (
        "void f(Screen *arg0) {\n    void *temp;\n    temp = M2C_FIELD(arg0, void **, 0x28C);\n"
        f"{extra}    x = M2C_FIELD(temp, s32 *, 0x100);\n}}\n"
    )
    got = ledger.resolve(text, KNOWN, {})
    assert "void *temp;" in got.text and got.left[0].base == "temp"


def test_a_void_pointer_global_is_untyped_and_a_record_global_without_the_member_is_typed():
    names = {"gAny": ledger.Global("void", 1, False), "gGame": ledger.Global("Game", 0, False)}
    text = "void f(void) {\n    M2C_FIELD(gAny, s32 *, 0x20) = 0;\n    M2C_FIELD(&gGame, f32 *, 0x10) = 0;\n}\n"
    got = ledger.resolve(text, KNOWN, {}, scope(**names))
    assert [(row.base, row.type) for row in got.left] == [("gAny", ""), ("&gGame", "Game")]


def test_a_typedef_name_other_than_the_tag_finds_the_type():
    got = ledger.resolve("void f(Game *p) {\n    x = M2C_FIELD(p, s32 *, 0x10);\n}\n", KNOWN, {})
    assert "x = p->buttons;" in got.text


def test_globals_are_the_extern_variables_a_draft_declares_above_its_function_or_a_header_declares():
    text = "extern s32 func(void);\nextern struct Game gGame;\nextern Screen *gScreen;\nextern f32 gTable[4];\n"
    draft = text + "void f(void) {\n}\nextern s32 gLate;\n"
    assert ledger.globals_in(draft) == {
        "gGame": ledger.Global("struct Game", 0, False),
        "gScreen": ledger.Global("Screen", 1, False),
        "gTable": ledger.Global("f32", 0, True),
    }
    inline = "static inline s32 g(void) {\n}\nextern s32 gLate;\n"
    assert ledger.globals_in(inline, "include/h.h") == {"gLate": ledger.Global("s32", 0, False, "include/h.h")}


UNION_VIEWS = structs._layouts(
    {
        "include/u.h": structs.scan(
            "union Preview {\n    struct { char pad0[0x20]; void *window; s32 item24; };\n"
            "    struct { char padC8[0xC8]; char workspace; };\n};\ntypedef union Preview Preview;\n"
            "typedef struct State {\n    union { s32 views[4]; f32 real; } ring;\n    s32 enabled;\n} State;\n"
        )
    }
)
UNION_KNOWN = tuple(ledger.identities(UNION_VIEWS))


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("M2C_FIELD(gPreview, void **, 0x20) = 0;", "gPreview->window = 0;"),  # RW ModelPreviewScreen D_800E5830
        ("M2C_FIELD(gPreview, s8 *, 0xC8) = 0;", "gPreview->workspace = 0;"),
        ("M2C_FIELD(arg0, s32 *, 8) = 0;", "arg0->ring.views[2] = 0;"),  # a member of a union member
        ("M2C_FIELD(arg0, f32 *, 0) = 0;", "arg0->ring.real = 0;"),  # the union member of the access's kind
    ],
)
def test_a_base_of_a_union_type_or_with_a_union_member_names_the_member(line, want):
    text = f"void f(State *arg0) {{\n    {line}\n}}\n"
    got = ledger.resolve(text, UNION_KNOWN, {}, ledger.Scope({"gPreview": ledger.Global("Preview", 1, False)}))
    assert want in got.text and got.left == ()
