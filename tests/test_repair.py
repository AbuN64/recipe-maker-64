"""repair: text rewrites of an m2c draft for the errors the compiler would name; pure functions, no compiler."""

import pytest

from recipemaker import repair


def test_a_stack_name_used_and_never_declared_gets_a_local_and_a_declared_one_does_not():
    text = "void f(void) {\n    M2C_UNK sp10;\n    x = sp14 + sp10 + spaced;\n    y = sp24;\n}\n"
    out = repair.stack(text)
    assert out.count("M2C_UNK sp14;") == 1 and out.count("M2C_UNK sp24;") == 1 and out.count("sp10;") == 1
    assert "M2C_UNK spaced;" not in out and repair.stack(out) == out  # a word that only looks hex is no stack name


def test_a_stack_name_in_a_comment_is_no_use():
    text = "void f(void) {\n    /* was sp14 */\n    x = 1;\n}\n"
    assert repair.stack(text) == text


def test_a_small_number_symbol_added_to_a_base_becomes_a_field_typed_like_what_reads_it():
    text = (
        "extern M2C_UNK D_26DB0;\nvoid f(void *a) {\n    f32 t;\n    t = *(a + &D_26DB0);\n"
        "    *(a + &D_26DB0) = 0;\n    g(&D_800C54B8);\n}\n"
    )
    out = repair.offsets(text)
    assert "t = M2C_FIELD(a, f32 *, 0x26DB0);" in out and "M2C_FIELD(a, s32 *, 0x26DB0) = 0;" in out
    assert "extern M2C_UNK D_26DB0;" not in out and "&D_800C54B8" in out  # a real address stays


def test_a_small_number_symbol_still_used_elsewhere_keeps_its_declaration():
    text = "extern M2C_UNK D_26DB0;\nvoid f(void *a) {\n    g(&D_26DB0, *(a + &D_26DB0));\n}\n"
    assert "extern M2C_UNK D_26DB0;" in repair.offsets(text)


def test_a_scalar_the_compiler_says_is_dereferenced_gets_one_more_pointer_level_per_round():
    text = "extern M2C_UNK D_800DFA6C;\nvoid f(s32 n) {\n    s32 v;\n    v = **(&D_800DFA6C + n);\n}\n"
    reason = "build/views/abc/src/f_2.c:33: invalid type argument of `unary *'"
    first = repair.deref(text, reason, skew=29)  # line 33 of the measured file is line 4 of the draft
    assert "extern M2C_UNK *D_800DFA6C;" in first and "s32 n" in first and "s32 v;" in first
    assert "extern M2C_UNK **D_800DFA6C;" in repair.deref(first, reason, skew=29)


def test_a_multiplication_is_no_dereference_and_a_line_outside_the_draft_changes_nothing():
    text = "void f(s32 a, s32 b) {\n    a = a * b;\n}\n"
    assert repair.deref(text, "x.c:31: invalid type argument of `unary *'", skew=29) == text
    assert repair.deref(text, "x.c:99: invalid type argument of `unary *'", skew=29) == text
    assert repair.refused("x.c:3: invalid type argument of `unary *'") and not repair.refused("other")


def test_a_bitwise_read_of_a_struct_local_is_its_first_field_when_the_types_agree():
    read = "M2C_BITWISE(f32, sp68) + M2C_BITWISE(s32, sp68) + M2C_BITWISE(f32, n)"
    text = f"void f(void) {{\n    Vec3 sp68;\n    s32 n;\n    x = {read};\n}}\n"
    out = repair.members(text, {"Vec3": "    f32 x, y, z;\n"})
    assert "x = sp68.x + M2C_BITWISE(s32, sp68) + M2C_BITWISE(f32, n);" in out


def test_a_pointer_cast_on_a_whole_call_argument_goes_and_one_in_arithmetic_or_an_assignment_stays():
    text = (
        "    h((struct Menu *) temp_v0_2);\n    h((Menu *) D_800E4EF0->unk1C);\n    f(a, (u8 *) b, c);\n"
        "    g((u8 *) p + 4);\n    x = (s32 *) y;\n"
    )
    out = repair.uncast(text)
    assert "h(temp_v0_2);" in out and "h(D_800E4EF0->unk1C);" in out and "f(a, b, c);" in out
    assert "g((u8 *) p + 4);" in out and "x = (s32 *) y;" in out


def test_m2c_unknown_types_are_spelled_as_the_scalars_they_are_typedefs_of():
    text = "M2C_UNK f(s32, M2C_UNK);\nvoid g(void) {\n    M2C_UNK sp10;\n    M2C_UNK8 a;\n"
    text += "    x = (M2C_UNK64) f(1, 2);\n}\n"
    out = repair.unknown(text)
    assert "M2C_" not in out
    assert "s32 f(s32, s32);" in out and "s32 sp10;" in out and "s8 a;" in out and "(s64)" in out


FAKED = (
    "Record_func_802A1990_de *func_802A1990_de(s32 arg0);\n"
    "void func_8041AD34_de(struct Menu_func_8041AD34_de *menu);\n"
    "Actor_func_1_de *func_80000001_de(Widget *w);\n"
    "s32 f(Actor_func_1_de *arg0) {\n"
    "    Record_func_802A1990_de *temp_v0;\n"
    "    Record_func_802A1990_de *temp_v1;\n"
    "    temp_v0 = func_802A1990_de(1);\n"
    "    func_8041AD34_de(temp_v0);\n"
    "    temp_v1 = func_802A1990_de(2);\n"
    "    return temp_v1->field_44;\n"
    "}\n"
)


def test_per_function_record_types_of_callee_prototypes_and_unread_locals_become_void_pointers():
    out = repair.unfake(FAKED, "f")
    assert "void *func_802A1990_de(s32 arg0);" in out and "void func_8041AD34_de(void *menu);" in out
    assert "void *func_80000001_de(Widget *w);" in out  # a real type in a prototype stays
    assert "    void *temp_v0;" in out  # only assigned and passed on: nothing reads through it


def test_a_local_read_through_and_the_functions_own_signature_keep_their_record_type():
    out = repair.unfake(FAKED, "f")
    assert "    Record_func_802A1990_de *temp_v1;" in out  # temp_v1->field_44 needs the record
    assert "s32 f(Actor_func_1_de *arg0) {" in out  # the definition follows the project's declaration
    assert repair.unfake(out, "f") == out


@pytest.mark.parametrize(
    ("line", "want"),
    [
        (  # registers m2c guessed past a known prototype (RW func_80204030)
            "x = f(a * a + b * b, /* extra? */ a, /* extra? */ b) < y;",
            "x = f(a * a + b * b) < y;",
        ),
        (  # a struct passed by value, spelled as its words (RW func_802149C0)
            "v = f(&g, /* Vec3+0x0 */ arg0->pos.x, /* Vec3+0x4 */ arg0->pos.y, /* Vec3+0x8 */ arg0->pos.z, arg0->n);",
            "v = f(&g, arg0->pos, arg0->n);",
        ),
        (  # words read through a pointer to the struct, and two structs in one call
            "f(a, /* Vec3+0x0 */ p->x, /* Vec3+0x4 */ p->y, /* Vec3+0x8 */ p->z, /* Vec3+0x0 */ q.a.x, "
            "/* Vec3+0x4 */ q.a.y, /* Vec3+0x8 */ q.a.z);",
            "f(a, *p, q.a);",
        ),
        (  # words of no common record stay; the guessed extra still goes (RW func_80440838)
            "f(g(1, 2), /* Vec3+0x0 */ sp70, /* Vec3+0x4 */ sp74, /* Vec3+0x8 */ sp78, 0, /* extra? */ t);",
            "f(g(1, 2), /* Vec3+0x0 */ sp70, /* Vec3+0x4 */ sp74, /* Vec3+0x8 */ sp78, 0);",
        ),
    ],
)
def test_m2c_argument_notes_become_the_call_the_project_prototype_takes(line, want):
    text = f"void h(void) {{\n    {line}\n}}\n"
    assert repair.arguments(text) == f"void h(void) {{\n    {want}\n}}\n"
    assert "/* extra? */" not in repair.repair(text)  # every draft gets the rewrite
