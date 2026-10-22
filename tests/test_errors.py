"""Error translation: one real stderr sample per class, the unknown class and the two auto-fixes."""

from types import SimpleNamespace

import pytest
from unbake.contracts import Finding

from recipemaker import errors, index

ROWS = {
    "header-disagreement": {
        "match": r"(?P<name>\w+): candidate declares '(?P<mine>[^']*)', (?P<where>\S+) declares '(?P<theirs>[^']*)'",
        "what": "{name} is declared as '{mine}' in the candidate but as '{theirs}' in {where}",
        "fix": "declare {name} as '{theirs}' like {where}, or delete the candidate's declaration of it",
    },
    "decl-conflict": {
        "match": r"conflicting types for [`'](?P<name>\w+)'",
        "key": "headers.conflict",
        "what": "{name} is declared two ways",
        "fix": "use the header's type",
        "autofix": "drop-local-definition",
    },
    "call-arity": {
        "match": r"too (?P<kind>few|many) arguments to function [`'](?P<name>\w+)'",
        "what": "{name} gets the wrong number of arguments",
        "fix": "pass {kind} arguments, see line {line}",
    },
    "pointer-mismatch": {
        "match": r"incompatible pointer type",
        "what": "pointer types differ",
        "fix": "match the type",
    },
    "undeclared": {
        "match": r"[`'](?P<name>\w+)' undeclared",
        "what": "{name} is not declared",
        "fix": "add {decl} before its first use; {typed}",
    },
    "m2c-text": {"match": r"M2C_ERROR", "what": "m2c left a note", "fix": "read the asm behind it"},
    "not-exact": {"key": "land.not_exact", "match": r"^never matches$", "what": "not exact", "fix": "see the diff"},
}
SAMPLES = {
    "decl-conflict": "version a: compile.error: src/f.c:12: conflicting types for `Actor'",
    "call-arity": "version a: compile.error: src/f.c:41: too many arguments to function `func_80012345'",
    "pointer-mismatch": "version a: compile.error: src/f.c:9: warning: incompatible pointer type",
    "undeclared": "version a: compile.error: src/f.c:3: `sp30' undeclared (first use in this function)",
    "m2c-text": "version a: compile.error: src/f.c:7: M2C_ERROR(unknown instruction)",
}


@pytest.fixture(autouse=True)
def rows(monkeypatch):
    monkeypatch.setattr(errors, "classes", lambda: ROWS)


@pytest.mark.parametrize("cls", sorted(SAMPLES))
def test_each_class_matches_its_real_sample(cls):
    got = errors.classify([SAMPLES[cls]])
    assert got.cls == cls and got.line > 0


def test_templates_fill_from_the_match_and_the_line():
    got = errors.classify([SAMPLES["call-arity"]])
    assert (got.line, got.name) == (41, "func_80012345")
    assert (
        got.what == "func_80012345 gets the wrong number of arguments" and got.fix == "pass many arguments, see line 41"
    )


def test_a_finding_key_alone_picks_a_class():
    got = errors.classify([], [Finding("land.not_exact", "bytes differ")])
    assert got.cls == "not-exact"


def test_the_first_row_in_file_order_wins():
    both = [SAMPLES["pointer-mismatch"], SAMPLES["undeclared"]]
    assert errors.classify(both).cls == "pointer-mismatch"


def test_anything_else_is_unknown_with_the_raw_first_line():
    got = errors.classify(["version a: weird failure at src/f.c:5: something new"])
    assert (got.cls, got.line) == ("unknown", 5) and "something new" in got.what and "recipe skip" in got.fix


def test_drop_local_definition_removes_a_typedef_the_headers_already_declare(fakes, monkeypatch):
    monkeypatch.setattr(
        index,
        "get",
        lambda s: SimpleNamespace(
            declarations={"Actor": ("include/actor.h", 14, "")}, identities=(), callers={}, shapes={}
        ),
    )
    text = "typedef struct {\n  int a;\n} Actor;\nvoid f(Actor *p) {}\n"
    got = errors.classify([SAMPLES["decl-conflict"]])
    fixed, said = errors.autofix(fakes.snapshot, text, got)
    assert "typedef" not in fixed and "include/actor.h:14" in said


def test_drop_local_definition_leaves_a_name_the_headers_do_not_know(fakes, monkeypatch):
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations={}, identities=(), callers={}, shapes={}, extents={})
    )
    text = "typedef struct {\n  int a;\n} Actor;\n"
    fixed, said = errors.autofix(fakes.snapshot, text, errors.classify([SAMPLES["decl-conflict"]]))
    assert (fixed, said) == (text, "")


def test_a_class_without_an_autofix_changes_nothing(fakes):
    text = "x"
    assert errors.autofix(fakes.snapshot, text, errors.classify([SAMPLES["call-arity"]])) == (text, "")


@pytest.mark.parametrize("key", ["member.split-delay-slot", "draft.m2c", "draft.data", "types.m2c", "recipe.leased"])
def test_recipe_findings_render_without_unbakes_key_table(key, monkeypatch):
    """After the unbake strip its keys.toml no longer has these keys: the lookup raises, rendering must not."""
    from unbake import config

    from recipemaker import cli

    def stripped(name):
        raise KeyError(name)

    monkeypatch.setattr(config, "sentence", stripped)
    lines = cli._finding(Finding(key, "m2c exited 1", unit="func_a", action="recipe next"))
    assert lines[0] == "refused: m2c exited 1" and lines[-1] == "next: recipe next"


def test_a_key_unbake_does_not_know_still_renders(monkeypatch):
    from unbake import config

    from recipemaker import cli

    monkeypatch.setattr(config, "sentence", lambda key: (_ for _ in ()).throw(KeyError(key)))
    assert cli._finding(Finding("land.request", "no such member"))[-1] == "next: recipe status"


def test_a_header_disagreement_names_the_symbol_and_both_types_with_their_files():
    from unbake.contracts import Finding

    found = Finding("headers.conflict", "f: candidate declares 'void', include/f.h:15 declares 'int'")
    got = errors.classify([], [found])
    assert got.cls == "header-disagreement" and got.name == "f"
    assert "'void' in the candidate but as 'int' in include/f.h:15" in got.what


DECLARED = {
    "ALCmdHandler": ("a.h", 7, "typedef Acmd *(*ALCmdHandler)(void *, short *, int, int, Acmd *);"),
    "Actor": ("b.h", 9, "typedef struct Actor Actor;"),
    "Acmd": ("c.h", 3, "typedef union { int w; } Acmd;"),
    "OSTime": ("d.h", 1, "typedef unsigned long long OSTime;"),
}


def _lines(text):
    return [n for _, _, _, n, _ in errors.banned(text, DECLARED)]


def test_a_cast_to_a_function_pointer_typedef_is_refused():
    assert _lines("void f(void) {\n    p = ((ALCmdHandler)s[i]->handler)(s[i], 0);\n}\n") == [2]


def test_a_cast_to_a_struct_pointer_typedef_or_a_struct_or_union_is_refused():
    assert _lines("a = (Actor)b;\nc = (Acmd)d;\ne = (struct Foo)g;\n") == [1, 2, 3]


def test_a_cast_through_a_typedef_the_candidate_declares_itself_is_refused():
    assert _lines("typedef void (*Local)(void);\nx = (Local)y;\n") == [2]


def test_casts_between_numbers_stay_allowed_even_through_a_scalar_typedef():
    assert _lines("a = (f32)x;\nb = (s32)y;\nc = (u8)z;\nd = (OSTime)w;\n") == []


def test_every_cast_line_is_reported_at_once():
    assert _lines("a = (Actor)b;\nok = (s32)c;\nd = (ALCmdHandler)e;\nf = (Actor *)g;\n") == [1, 3, 4]


def test_indexing_a_pointer_to_a_scalar_global_is_refused_but_index_zero_is_not():
    declared = {"G": ("g.h", 1, "extern s32 G;"), "T": ("g.h", 2, "extern s32 T[4];")}
    body = ["    s32 *p;", "    p = &G;", "    p[0] = 1;", "    p[2] = 1;", "    x = *(p + 1);", "    y = p[-1];"]
    text = "\n".join(["void f(s32 x) {", *body, "}", ""])
    hits = [(k, n) for k, _, _, n, _ in errors.banned(text, declared)]
    assert hits == [("recipe.neighbour", 5), ("recipe.neighbour", 6), ("recipe.neighbour", 7)]
    assert errors.banned("void f(void) {\n    s32 *p;\n    p = &T;\n    p[2] = 1;\n}\n", declared) == []


def test_an_undeclared_name_gets_the_extern_line_from_the_header_that_declares_it(fakes, monkeypatch):
    declared = {"gCount": ("include/count.h", 3, "extern s32 gCount;")}
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations=declared, identities=(), callers={}, shapes={}, extents={})
    )
    known = errors.translate(fakes.snapshot, "x = gCount;\n", ["src/f.c:3: `gCount' undeclared"], [])
    assert known.fix == "add `extern s32 gCount;` before its first use; the type is from include/count.h"
    other = errors.translate(fakes.snapshot, "x = gOther;\n", ["src/f.c:3: `gOther' undeclared"], [])
    assert (
        other.fix == "add an extern declaration of gOther before its first use; "
        "its type is unknown: find it with `recipe look gOther`"
    )
    ident = SimpleNamespace(names=("ResetFourStorage",), views=[SimpleNamespace(path="include/store.h")])
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations={}, identities=(ident,), callers={}, shapes={}, extents={})
    )
    kind = errors.translate(fakes.snapshot, "x;\n", ["src/f.c:3: `ResetFourStorage' undeclared"], [])
    assert (
        kind.fix == 'add `#include "store.h"` before its first use; '
        "ResetFourStorage is a type, not a variable: never declare it extern"
    )


def test_perm_marks_are_banned_like_m2c_text_so_they_never_land():
    hits = errors.banned("void f(void) {\n    PERM_GENERAL(a = 1;, a = 2;)\n}\n", {})
    assert [(k, n) for k, _, _, n, _ in hits] == [("recipe.perm", 2)]


def test_an_undeclared_standard_macro_says_to_include_its_header_never_extern(fakes, monkeypatch):
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations={}, identities=(), callers={}, shapes={}, extents={})
    )
    got = errors.translate(fakes.snapshot, "x = NULL;\n", ["src/f.c:3: `NULL' undeclared"], [])
    assert (
        got.fix.startswith("add `#include <stddef.h>` before its first use") and "extern" not in got.fix.split(";")[0]
    )


def test_a_declaration_conflict_points_at_the_candidates_own_prototype_not_the_compiled_line(fakes, monkeypatch):
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations={}, identities=(), callers={}, shapes={}, extents={})
    )
    text = "typedef int T;\n\n\nvoid g(void);\ns32 func_f(s32 a);\n"
    got = errors.translate(fakes.snapshot, text, ["src/f.c:11: conflicting types for `func_f'"], [])
    assert (got.cls, got.line) == ("decl-conflict", 5)


def test_a_local_that_is_only_assigned_is_banned_and_one_that_is_read_or_declared_before_is_not():
    text = "void f(s32 *a) {\n    s32 x = 0;\n    s32 y = 1;\n    x = 2;\n    *a = y;\n    a[0] = x == 1;\n}\n"
    unused = [(n, line) for k, _, _, n, line in errors.banned(text, {}) if k == "recipe.unused"]
    assert unused == []
    text = "void f(s32 *a) {\n    s32 x = 0;\n    s32 y = 1;\n    x = 2;\n    *a = y;\n}\n"
    unused = [(n, line) for k, _, _, n, line in errors.banned(text, {}) if k == "recipe.unused"]
    assert unused == [(2, "s32 x = 0;")]
    assert [n for k, _, _, n, _ in errors.banned(text, {}, old="    s32 x = 0;\n") if k == "recipe.unused"] == []


@pytest.mark.parametrize(
    ("message", "unbuilt"),
    [("version a: compile.error: x", True), ("version a: preprocess failed", True), ("version a: bytes differ", False)],
)
def test_a_proof_message_about_a_compile_or_preprocess_failure_is_unbuilt(message, unbuilt):
    assert errors.unbuilt(message) is unbuilt


def test_comments_and_strings_are_no_code_for_the_unused_local_and_cast_rules():
    text = 'void f(void) {\n    /* the counter\n       stays 0 after; (Foo *) x */\n    g("(Foo *) p");\n}\n'
    assert errors.banned(text, {}) == []
