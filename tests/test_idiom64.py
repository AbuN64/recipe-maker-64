"""idiom64: m2c's helper call over hi/lo words collapses to a u64 expression only when the words prove to be halves."""

from recipemaker import idiom64

HELPERS = {"__udivdi3": "/", "func_80124EC0": "/", "__umoddi3": "%"}
MUL = """extern s32 now(void);
extern s32 func_80124EC0(s32, s32, s32, s32);
extern s32 D_80145DA0;
extern s32 D_80145DA4;
extern s32 D_80327C70;
extern s32 D_80327C74;

void f(void) {
    s32 temp_ret;
    s32 temp_ret_2;
    u32 temp_v1;
    u32 temp_v1_2;
    s32 temp_v0;

    temp_ret = now();
    temp_v0 = temp_ret;
    temp_v1 = (u32) (u64) temp_ret;
%s
    temp_ret_2 = func_80124EC0(%s, temp_v1 * 0x3E8, D_80145DA0, D_80145DA4);
%s
}
"""
HI = "temp_v0 * 0x3E8 + (u32) (((u64) temp_v1 * 0x3E8) >> 0x20)"
ADD = """    temp_v1_2 = (u32) (u64) temp_ret_2 + 0x10;
    D_80327C70 = temp_ret_2 + (temp_v1_2 < 0x10U);
    D_80327C74 = (s32) temp_v1_2;"""
PLAIN = """    D_80327C70 = temp_ret_2;
    D_80327C74 = (s32) (u32) (u64) temp_ret_2;"""


def test_a_call_result_times_a_constant_over_a_global_pair_becomes_one_u64_expression() -> None:
    out = idiom64.collapse(MUL % ("", HI, ADD), HELPERS)
    assert "D_80327C70 = now() * 1000ULL / D_80145DA0 + 0x10ULL;" in out
    assert "extern u64 now(void);" in out
    assert "extern u64 D_80145DA0;" in out and "D_80145DA4" not in out and "D_80327C74" not in out
    assert "extern u64 D_80327C70;" in out
    assert "func_80124EC0" not in out and "temp_" not in out


def test_the_plain_result_stores_collapse_without_an_addend() -> None:
    assert "D_80327C70 = now() * 1000ULL / D_80145DA0;" in idiom64.collapse(MUL % ("", HI, PLAIN), HELPERS)


def test_a_store_between_the_call_and_the_division_keeps_the_order_through_one_u64_temp() -> None:
    out = idiom64.collapse(MUL % ("    other = 1;", HI, PLAIN), HELPERS)
    assert (
        out.index("tmp64 = now() * 1000ULL;") < out.index("other = 1;") < out.index("D_80327C70 = tmp64 / D_80145DA0;")
    )
    assert "    u64 tmp64;\n" in out


def test_the_modulo_helper_prints_a_remainder() -> None:
    out = idiom64.collapse((MUL % ("", HI, PLAIN)).replace("func_80124EC0", "__umoddi3"), HELPERS)
    assert "now() * 1000ULL % D_80145DA0;" in out


def test_high_words_that_are_not_the_product_are_left_alone() -> None:
    text = MUL % ("", "temp_v0 + 7", PLAIN)
    assert idiom64.collapse(text, HELPERS) == text


def test_a_denominator_that_is_not_an_adjacent_global_pair_is_left_alone() -> None:
    text = (MUL % ("", HI, PLAIN)).replace("D_80145DA4", "D_80145DA8")
    assert idiom64.collapse(text, HELPERS) == text


def test_without_helpers_the_text_is_untouched() -> None:
    assert idiom64.collapse(MUL % ("", HI, ADD), {}) == MUL % ("", HI, ADD)


def test_aliases_name_the_helper_and_its_label_in_the_version() -> None:
    table = {"__udivdi3": {"kind": "function", "us": 0x80124EC0}, "other": {"us": 1}, "__moddi3": {"de": 5}}
    assert idiom64.aliases(table, "us") == {"__udivdi3": "/", "func_80124EC0": "/"}


TYPED = (
    MUL.replace("s32 temp_ret;", "u64 temp_ret;")
    .replace("s32 temp_ret_2;", "u64 temp_ret_2;")
    .replace("s32 temp_v0;", "u64 temp_v0;")
    .replace("(u32) (u64) ", "(u32) ")
)


def test_u64_typed_temps_and_single_cast_low_words_collapse_the_same() -> None:
    sink = (
        "    temp_v1_2 = (u32) temp_ret_2 + 0x10;\n"
        "    D_80327C70 = temp_ret_2 + (temp_v1_2 < 0x10U);\n    D_80327C74 = temp_v1_2;"
    )
    out = idiom64.collapse(TYPED % ("", HI, sink), HELPERS)
    assert "D_80327C70 = now() * 1000ULL / D_80145DA0 + 0x10ULL;" in out and "D_80327C74" not in out


def test_a_signed_word_addend_and_an_offset_one_collapse_to_the_sum() -> None:
    word = (
        "    temp_v1_2 = (u32) temp_ret_2 + D_80327C60;\n"
        "    D_80327C70 = temp_ret_2 + ((s32) D_80327C60 >> 0x1F) + (temp_v1_2 < (u32) D_80327C60);\n"
        "    D_80327C74 = temp_v1_2;"
    )
    assert "D_80327C70 = now() * 1000ULL / D_80145DA0 + D_80327C60;" in idiom64.collapse(
        TYPED % ("", HI, word), HELPERS
    )
    offset = (
        "    temp_a2 = D_80327C60 + 0xFFE17B80;\n    temp_v1_2 = (u32) temp_ret_2 + temp_a2;\n"
        "    D_80327C70 = temp_ret_2 + (temp_a2 >> 0x1F) + (temp_v1_2 < (u32) temp_a2);\n    D_80327C74 = temp_v1_2;"
    )
    assert "/ D_80145DA0 + (D_80327C60 - 2000000);" in idiom64.collapse(TYPED % ("", HI, offset), HELPERS)


WORDS = {**HELPERS, "func_80125450": "cmp", "func_801254A0": "f32"}
PAIR = """extern u64 now(void);
extern s32 func_80124EC0(s32, s32, s32, s32);
extern s32 D_80145DA0;
extern s32 D_80145DA4;
extern s32 D_80327D48;
extern s32 D_80327D4C;

void f(void) {
    u64 temp_ret;
    u64 temp_ret_2;
    u64 temp_s2;
    u32 temp_s3;
    u64 temp_v0;
    u32 temp_v1;

    temp_ret = now();
    temp_v0 = temp_ret;
    temp_v1 = (u32) temp_ret;
    temp_ret_2 = func_80124EC0(%s, temp_v1 * 0x3E8, D_80145DA0, D_80145DA4);
    temp_s2 = temp_ret_2;
    temp_s3 = (u32) temp_ret_2;
%s
}
"""
BELOW = (
    "    if ((temp_s2 < (u32) D_80327D48) || ((D_80327D48 == temp_s2) && (temp_s3 < (u32) D_80327D4C))) {\n"
    "        x = 1;\n    }"
)
ABOVE = (
    "    if (((u32) D_80327D48 >= temp_s2) && ((temp_s2 != D_80327D48) || ((u32) D_80327D4C >= temp_s3))) {\n"
    "        x = 2;\n    }"
)


def test_pair_locals_of_a_division_result_become_one_u64_and_a_word_comparison_one_comparison() -> None:
    out = idiom64.collapse(PAIR % (HI, BELOW), WORDS)
    assert "temp_ret_2 = now() * 1000ULL / D_80145DA0;" in out
    assert "if (temp_ret_2 < D_80327D48) {" in out
    assert "extern u64 D_80327D48;" in out and "D_80327D4C" not in out and "temp_s" not in out
    assert "if (D_80327D48 >= temp_ret_2) {" in idiom64.collapse(PAIR % (HI, ABOVE), WORDS)


def test_nested_ifs_that_are_one_comparison_merge_into_one_if() -> None:
    nested = (
        "    if ((u32) D_80327D48 >= temp_s2) {\n"
        "        if ((temp_s2 != D_80327D48) || ((u32) D_80327D4C >= temp_s3)) {\n"
        "            x = 3;\n        }\n    }"
    )
    out = idiom64.collapse(PAIR % (HI, nested), WORDS)
    assert "    if (D_80327D48 >= temp_ret_2) {\n        x = 3;\n    }\n" in out


def test_a_comparison_the_words_do_not_make_leaves_the_whole_text_alone() -> None:
    wrong = BELOW.replace("temp_s3 < (u32) D_80327D4C", "temp_s3 > (u32) D_80327D4C")
    text = PAIR % (HI, wrong)
    assert idiom64.collapse(text, WORDS) == text


def test_a_low_word_that_something_else_still_reads_keeps_the_pair_as_it_was() -> None:
    text = PAIR % (HI, BELOW + "\n    other = temp_s3 + 1;")
    assert idiom64.collapse(text, WORDS) == text


def test_a_difference_of_two_pairs_and_the_float_conversion_of_it_are_one_subtraction_and_one_cast() -> None:
    body = (
        "    temp_s1 = D_80327D4C - temp_s3;\n    temp_s0 = (D_80327D48 - temp_s2) - ((u32) D_80327D4C < temp_s3);\n"
        "    if (func_80125450(temp_s0, temp_s1, 0, 0) > 0) {\n        v = func_801254A0(temp_s0, temp_s1);\n"
        "    } else {\n        temp_a2 = temp_s0 << 0x1F;\n"
        "        v = 2.0f * func_801254A0((temp_s0 & 0) | (temp_s0 >> 1), (temp_s1 & 1) | ((temp_s1 >> 1) | temp_a2), "
        "temp_a2);\n    }\n    use = v;"
    )
    text = PAIR.replace(
        "u32 temp_v1;", "u32 temp_v1;\n    s32 temp_s0;\n    u32 temp_s1;\n    s32 temp_a2;\n    f32 v;"
    )
    out = idiom64.collapse(text % (HI, body), WORDS)
    assert "temp_s0 = D_80327D48 - temp_ret_2;" in out and "v = (f32) temp_s0;" in out
    assert "u64 temp_s0;" in out and "func_801254A0" not in out and "temp_a2" not in out
    swapped = body.replace("temp_s0 >> 1", "temp_s0 >> 2")
    assert "(f32)" not in idiom64.collapse(text % (HI, swapped), WORDS)


def test_a_one_sided_range_over_a_constant_is_a_comparison_with_that_constant() -> None:
    cond = "    if ((temp_s2 != 0) || ((temp_s2 == 0) && (temp_s3 > 0x1E8480U))) {\n        x = 4;\n    }"
    out = idiom64.collapse(PAIR % (HI, cond), WORDS)
    assert "if (temp_ret_2 > 2000000ULL) {" in out
