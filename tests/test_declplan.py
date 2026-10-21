"""declplan: the text edits of a declaration change set."""

from recipemaker import declplan


def test_copies_finds_every_declaration_of_a_name_and_not_a_definition_or_a_static():
    text = (
        "extern s32 func_f(s32 a);\n"
        "extern void other(void), func_f(s32 a);\n"
        "static s32 func_f(s32 a);\n"
        "s32 func_f(s32 a) { return a; }\n"
        "typedef s32 (*func_f)(s32);\n"
    )
    found = declplan.copies(text, "func_f")
    assert [c.prototype for c in found] == ["s32 func_f(s32 a)", "void func_f(s32 a)"]
    assert found[0].statement == "extern s32 func_f(s32 a);"


def test_remove_deletes_a_whole_line_and_a_lone_extern_marker_above_it():
    text = '#include "a.h"\n/* extern */\nextern s32 f(s32);\nint g(void);\n'
    one = declplan.copies(text, "f")[0]
    assert declplan.remove(text, one, "f") == '#include "a.h"\nint g(void);\n'


def test_remove_keeps_the_other_names_of_a_shared_statement():
    text = "extern void func_a(void),func_f(s32 a);\n"
    one = declplan.copies(text, "func_f")[0]
    assert declplan.remove(text, one, "func_f") == "extern void func_a(void);\n"
    text = "extern void func_f(s32 a),func_a(void);\n"
    one = declplan.copies(text, "func_f")[0]
    assert declplan.remove(text, one, "func_f") == "extern void func_a(void);\n"


def test_remove_leaves_a_statement_that_shares_its_line_with_code_as_code():
    text = "int x; extern s32 f(s32); int y;\n"
    one = declplan.copies(text, "f")[0]
    assert declplan.remove(text, one, "f") == "int x;  int y;\n"


def test_put_replaces_the_first_copy_in_place_and_drops_the_rest():
    text = "#ifndef H\n#define H\nextern s32 f(void);\nextern s32 g(void);\nextern s32 f(s32);\n#endif\n"
    out = declplan.put(text, "f", "s32 f(s32 a, s32 *b)")
    assert out == "#ifndef H\n#define H\nextern s32 f(s32 a, s32 *b);\nextern s32 g(void);\n#endif\n"


def test_put_adds_after_the_last_declaration_in_the_style_of_the_header():
    plain = "#ifndef H\n#define H\ns32 g(void);\n#endif\n"
    assert declplan.put(plain, "f", "s32 f(void)") == "#ifndef H\n#define H\ns32 g(void);\ns32 f(void);\n#endif\n"
    bare = "#ifndef H\n#define H\n\n#endif\n"
    assert declplan.put(bare, "f", "s32 f(void)") == "#ifndef H\n#define H\n\nextern s32 f(void);\n#endif\n"


def test_add_include_goes_after_the_last_include_or_to_the_top():
    assert declplan.add_include('#include "a.h"\n#include "b.h"\nint x;\n', "include/c/d.H") == (
        '#include "a.h"\n#include "b.h"\n#include "c/d.H"\nint x;\n'
    )
    assert declplan.add_include("int x;\n", "include/d.h") == '#include "d.h"\nint x;\n'
    mid = '#include "a.h"\n\n#include "b.h"\nint x;\n#include "late.h"\n'
    assert (
        declplan.add_include(mid, "include/c.h")
        == '#include "a.h"\n\n#include "b.h"\n#include "c.h"\nint x;\n#include "late.h"\n'
    )
    guarded = "#ifndef H\n#define H\nint x;\n#endif\n"
    assert declplan.add_include(guarded, "include/c.h") == '#ifndef H\n#define H\n#include "c.h"\nint x;\n#endif\n'


def test_reaches_follows_includes_through_other_headers_and_survives_a_cycle():
    files = {
        "src/a.c": '#include "mid.h"\n',
        "include/mid.h": '#include "fam.h"\n#include "mid.h"\n',
        "include/fam.h": "",
        "src/b.c": '#include "other.h"\n',
        "include/other.h": '#include "b2.h"\n',
        "include/b2.h": '#include "other.h"\n',
    }
    read = files.get
    assert declplan.reaches("src/a.c", "include/fam.h", read)
    assert not declplan.reaches("src/b.c", "include/fam.h", read)


def test_split_signature_drops_parameter_names_and_reads_void_as_none():
    assert declplan.split_signature("void *f(void *table, s32 index)", "f") == ("void *", ["void *", "s32"])
    assert declplan.split_signature("s32 f(void)", "f") == ("s32", [])
    assert declplan.split_signature("extern CVector3 *f(unsigned int, struct Foo *p)", "f") == (
        "CVector3 *",
        ["unsigned int", "struct Foo *"],
    )


def test_cast_calls_casts_scalar_arguments_and_results_back_to_the_old_types():
    text = "void g(void) {\n    y = f(a, b + 1);\n    f(c, d);\n}\n"
    got = declplan.cast_calls(text, "f", ("u16", ["u16", "s32"]), ("s32", ["s32", "s32"]))
    assert got.text == "void g(void) {\n    y = (u16)f((u16)a, b + 1);\n    f((u16)c, d);\n}\n"
    assert got.blocked == [] and got.count == 2


def test_cast_calls_refuses_to_cast_a_pointer_view_and_a_changed_arity():
    got = declplan.cast_calls("x = f(p);\n", "f", ("void", ["struct A *"]), ("void", ["void *"]))
    assert got.text == "x = f(p);\n" and got.blocked and got.count == 0
    got = declplan.cast_calls("x = f(p);\n", "f", ("void", ["s32", "s32"]), ("void", ["s32"]))
    assert got.blocked == ["f took 2 parameters here, the definition takes 1"]


def test_a_reference_that_is_not_a_call_is_left_alone():
    text = "p = &f;\nq = s.f(1);\nr = f(1);\n"
    got = declplan.cast_calls(text, "f", ("void", ["u8"]), ("void", ["s32"]))
    assert got.text == "p = &f;\nq = s.f(1);\nr = f((u8)1);\n"
