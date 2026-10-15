"""records: m2c's re-printed project structs folded into the header that defines them; pure text, no compiler."""

import pytest

from recipemaker import records

HEADER = (
    "struct Thing;\ntypedef struct Thing Thing;\nstruct Other;\ntypedef struct Other Other;\n"
    "struct Thing {\n    char pad[0x10];\n    s32 named;\n    char tail[4];\n};\n"
)
PRINTED = (
    "typedef struct Thing {\n"
    "    u8 unk0;                            /* inferred */\n"
    "    char pad1[3];                       /* maybe part of unk0[4]? */\n"
    "    Other *unk4;                        /* inferred */\n"
    "    char pad8[8];\n"
    "    s32 named;\n"
    "    s8 tail[4];\n"
    "} Thing;\n\n"
)
DRAFT = '#include "a.h"\n' + PRINTED + "void f(Thing *a) {\n    a->unk0 = 1;\n}\n"


def test_a_struct_m2c_printed_again_leaves_the_draft_and_its_new_fields_go_into_the_header_body():
    out, edits, notes = records.fold(DRAFT, {"include/a.h": HEADER})
    assert "typedef struct Thing {" not in out and "a->unk0 = 1;" in out
    head = edits["include/a.h"]
    assert "    u8 unk0;\n    char pad1[3];\n    struct Other *unk4;\n    char pad8[8];\n" in head  # no m2c comments
    assert "    s32 named;\n    char tail[4];\n};\n" in head  # a field the header names keeps the header's line
    assert "char pad[0x10];" not in head and head.startswith("struct Thing;\n") and len(notes) == 1


def test_a_struct_m2c_printed_that_drops_a_named_field_or_holds_m2c_text_is_left_alone():
    dropped = PRINTED.replace("    s32 named;\n", "")
    for printed in (dropped, PRINTED.replace("Other *unk4", "M2C_UNK16 unk4")):
        text = printed + "void f(void) {\n}\n"
        assert records.fold(text, {"include/a.h": HEADER}) == (text, {}, [])


def test_a_struct_no_header_defines_and_the_stack_struct_stay_in_the_draft():
    text = "struct _m2c_stack_f {\n    s32 x;\n};\n\ntypedef struct New {\n    s32 x;\n} New;\n\nvoid f(void) {\n}\n"
    assert records.fold(text, {"include/a.h": HEADER}) == (text, {}, [])


def test_a_struct_m2c_printed_with_nothing_new_leaves_the_draft_and_edits_no_header():
    printed = "struct Thing {\n    char pad[0x10];\n    s32 named;\n    char tail[4];\n};\n"
    out, edits, _ = records.fold(printed + "void f(void) {\n}\n", {"include/a.h": HEADER})
    assert out == "void f(void) {\n}\n" and edits == {}


def test_two_structs_of_one_header_both_fold_into_it():
    header = HEADER + "typedef struct Pair {\n    char pad0[8];\n} Pair;\n"
    pair = "typedef struct Pair {\n    s32 unk0;\n    s32 unk4;\n} Pair;\n\n"
    _, edits, notes = records.fold(PRINTED + pair + "void f(void) {\n}\n", {"include/a.h": header})
    assert "    u8 unk0;" in edits["include/a.h"] and "    s32 unk0;\n    s32 unk4;\n} Pair;" in edits["include/a.h"]
    assert len(notes) == 2


def test_a_field_m2c_could_not_size_with_no_padding_after_it_is_left_out_of_the_header():
    printed = PRINTED.replace("    char pad8[8];\n", "    char pad8[8];\n    M2C_UNK unk10;\n")
    _, edits, _ = records.fold(printed + "void f(void) {\n}\n", {"include/a.h": HEADER})
    assert "unk10" not in edits["include/a.h"] and "    char pad8[8];\n" in edits["include/a.h"]


def test_a_field_m2c_could_not_size_before_padding_at_its_offset_takes_a_word_of_that_padding():
    printed = PRINTED.replace(
        "    char pad8[8];\n", "    M2C_UNK unk8;                        /* inferred */\n    char pad8[8];\n"
    )
    _, edits, _ = records.fold(printed + "void f(void) {\n}\n", {"include/a.h": HEADER})
    assert "    s32 unk8;\n    char padC[0x4];\n    s32 named;" in edits["include/a.h"]


def test_padding_that_keeps_its_name_but_shrinks_takes_m2cs_size_and_a_field_after_it_is_added():
    printed = PRINTED.replace("    char pad1[3];  ", "    char pad1[3];  ").replace(
        "typedef struct Thing {\n", "typedef struct Thing {\n    char pad[0x8];\n    s32 unk8;\n    char padC[4];\n"
    )
    header = HEADER.replace("    char pad[0x10];\n", "    char pad[0x10];\n")
    head = records.fold(
        printed.split("    u8 unk0;")[0] + "    s32 named;\n    char tail[4];\n} Thing;\n\n", {"include/a.h": header}
    )[1]
    assert "    char pad[0x8];\n    s32 unk8;\n    char padC[4];\n    s32 named;" in head["include/a.h"]


def test_a_header_line_declaring_several_fields_matches_each_and_a_header_m2c_left_alone_stays_as_written():
    header = "struct Trio {\n    s32 unk0, unk4;\n    char pad8[8];\n};\n"
    printed = "typedef struct Trio {\n    s32 unk0;\n    s32 unk4;\n    s32 unk8;\n    char padC[4];\n} Trio;\n\n"
    edits = records.fold(printed + "void f(void) {\n}\n", {"include/t.h": header})[1]
    assert edits["include/t.h"] == "struct Trio {\n    s32 unk0;\n    s32 unk4;\n    s32 unk8;\n    char padC[4];\n};\n"


def test_a_field_m2c_typed_with_a_record_named_after_one_function_is_a_void_pointer_in_the_header():
    printed = PRINTED.replace("    Other *unk4;", "    Rec_func_80001234_xx *unk4;")
    edits = records.fold(printed + "void f(void) {\n}\n", {"include/a.h": HEADER})[1]
    assert "    void *unk4;\n" in edits["include/a.h"] and "func_80001234" not in edits["include/a.h"]
    by_value = PRINTED.replace("    Other *unk4;", "    Rec_func_80001234_xx unk4;")
    assert records.fold(by_value, {"include/a.h": HEADER})[1] == {}


SCREEN = (  # an anonymous union member (RW ResultsOptionsScreen)
    "struct Screen;\ntypedef struct Screen Screen;\n"
    "struct Screen {\n    char pad0[0x970];\n    union { MenuWidget *root; void *parent; };\n"
    "    char pad974[0xA44 - 0x974];\n    s32 selection;\n};\n"
)
SCREEN_PRINTED = (  # as the draft holds it: m2c's offset and size notes are gone
    "typedef struct Screen {\n"
    "    char pad0[0x970];\n"
    "    MenuWidget *root;\n"
    "    void *parent;                       /* overlap */\n"
    "    Node_func_8041B610_de *unk974;      /* inferred */\n"
    "    char pad978[0xCC];\n"
    "    s32 selection;\n"
    "} Screen;\n\n"
)
STATE = (  # a named nested union, a declarator list and several members a line (RW RenderState, RenderView)
    "typedef struct View {\n    char pad00[0x14]; s32 kind; char pad18[8];\n    struct View *previous, *next;\n"
    "} View;\n"
    "typedef struct State {\n    union { View views[4]; s32 raw; } ring; char pad100[0x10]; s32 enabled, more;\n"
    "} State;\n"
)
STATE_PRINTED = (
    "typedef struct State {\n"
    "    union _anonymous ring;\n"
    "    s32 unk100;                       /* inferred */\n"
    "    char pad104[0xC];\n"
    "    s32 enabled;\n"
    "    s32 more;\n"
    "} State;\n\n"
)
VIEW_PRINTED = (
    "typedef struct View {\n    s32 unk0;   /* inferred */\n    char pad4[0x10];\n    s32 kind;\n"
    "    char pad18[8];\n    View *previous;\n    View *next;\n} View;\n\n"
)


@pytest.mark.parametrize(
    ("header", "printed", "folded"),
    [
        (
            SCREEN,
            SCREEN_PRINTED,
            "    union { MenuWidget *root; void *parent; };\n    void *unk974;\n    char pad978[0xCC];\n",
        ),
        (STATE, STATE_PRINTED, "    union { View views[4]; s32 raw; } ring;\n    s32 unk100;\n    char pad104[0xC];\n"),
        (STATE, VIEW_PRINTED, "    s32 unk0;\n    char pad4[0x10];\n    s32 kind;\n    char pad18[8];\n"),
    ],
)
def test_a_header_body_with_a_nested_record_or_a_declarator_list_takes_m2cs_fields(header, printed, folded):
    out, edits, notes = records.fold(printed + "void f(void) {\n}\n", {"include/s.h": header})
    assert out == "void f(void) {\n}\n" and len(notes) == 1
    assert folded in edits["include/s.h"]


def test_a_nested_record_m2c_printed_nothing_of_is_a_named_field_that_would_vanish():
    printed = SCREEN_PRINTED.replace("    MenuWidget *root;\n", "").replace(
        "    void *parent;                       /* overlap */\n", "    char pad970[4];\n"
    )
    text = printed + "void f(void) {\n}\n"
    assert records.fold(text, {"include/s.h": SCREEN}) == (text, {}, [])


@pytest.mark.parametrize(
    ("pad", "want"),
    [
        ("char pad190[4];", "    s32 unk190;\n    s16 unk194;"),  # RW func_8041C610: the whole padding is the word
        ("char pad190[2];", "    s16 unk190;\n    char pad192[2];"),
        ("char pad190[1];", "    s8 unk190;\n    char pad191[3];"),
    ],
)
def test_a_field_m2c_could_not_size_takes_the_widest_integer_its_padding_holds(pad, want):
    header = "struct T {\n    s32 unk0;\n    char pad4[0x18C];\n    char pad190[4];\n    s16 unk194;\n};\n"
    rest = "" if pad.endswith("[4];") else f"    char pad{0x190 + int(pad[-3]):X}[{4 - int(pad[-3])}];\n"
    printed = (
        "struct T {\n    s32 unk0;\n    char pad4[0x18C];\n    M2C_UNK unk190;   /* inferred */\n"
        f"    {pad}\n{rest}    s16 unk194;\n}};\n\n"
    )
    _, edits, _ = records.fold(printed + "void f(void) {\n}\n", {"include/t.h": header})
    assert want in edits["include/t.h"]
