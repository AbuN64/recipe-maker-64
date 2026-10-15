"""Type definitions per unit: what a unit defines in its own text and in the headers it includes."""

from recipemaker import clash


def test_definitions_normalize_whitespace_and_comments_and_read_tags_and_typedefs():
    a = clash.definitions("typedef struct  S {\n int a; /* x */\n} S;\nenum E { A, B };")
    b = clash.definitions("typedef struct S { int a; } S; enum E {A,B};")
    assert a["struct S"] == "struct S { int a; }" and a["typedef S"].startswith("typedef struct S")
    assert a["enum E"] == "enum E { A, B }" and clash.conflicts(a, b) == ["enum E"]  # spelling of A,B differs


def test_a_function_pointer_typedef_is_named_by_its_pointer():
    assert "typedef Fn" in clash.definitions("typedef void (*Fn)(int);")


def test_a_unit_has_the_definitions_of_the_headers_it_includes_and_each_header_is_read_once(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "include/a.h").write_text('#include "b.h"\nstruct A { int x; };\n')
    (tmp_path / "include/b.h").write_text("struct B { int y; };\n")
    (tmp_path / "src/u1.c").write_text('#include "a.h"\nvoid f(void) {}\n')
    (tmp_path / "src/u2.c").write_text("struct B { char y; };\n")
    types = clash.reader(tmp_path)
    assert set(types("src/u1.c")) == {"struct A", "struct B"}
    assert clash.conflicts(types("src/u1.c"), types("src/u2.c")) == ["struct B"]
    (tmp_path / "include/b.h").write_text("changed")
    assert "struct B" in types("src/u1.c")  # a header is not read again
    assert types("src/missing.c") == {}
