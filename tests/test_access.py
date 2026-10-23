"""Typed member access: the record each `.name` and `->name` of the C belongs to, from the declarations in scope."""

from recipemaker import access

HEADER = """
typedef struct Inner { s32 count; } Inner;
typedef struct Outer {
    Inner part;
    Inner *link;
    union { s32 raw; struct { s32 flags; }; };
} Outer;
typedef struct Other { s32 count; } Other;
extern Outer *g_outer;
"""


def sites(code, word="count", extra=None):
    texts = {"include/a.h": HEADER, "src/a.c": '#include "a.h"\n' + code, **(extra or {})}
    found = access.Types(texts)
    found.learn(texts)
    return [(s.owner, texts[s.path][s.at - 3 : s.at + len(word)].strip()) for s in found.sites(texts, [word])]


def test_parameters_locals_and_globals_type_the_base():
    got = sites(
        "void f(Inner *a, Other b) { Inner c, *d; a->count = b.count + c.count + d->count + g_outer->part.count; }"
    )
    assert [o for o, _ in got] == ["Inner", "Other", "Inner", "Inner", "Inner"]


def test_chains_casts_indexes_calls_and_assignments_are_typed_and_the_rest_is_unknown():
    code = (
        "Other *pick(void);\n"
        "void f(Outer *o, void *v, Outer arr[2]) {\n"
        "    o->link->count; ((Other *)v)->count; arr[1].part.count; pick()->count; (o = 0)->part.count;\n"
        "    unknown->count; get(o)->count;\n}\n"
    )
    assert [o for o, _ in sites(code)] == ["Inner", "Other", "Inner", "Other", "Inner", "", ""]


def test_an_untagged_record_member_lends_its_members_to_the_owner():
    assert [o for o, _ in sites("void f(Outer *o) { o->flags; o->raw; }", "flags")] == ["Outer"]


def test_only_the_first_branch_of_a_conditional_pairs_the_braces_but_every_branch_is_found():
    code = "void f(Inner *p) {\n#if A\n if (p) {\n#else\n if (!p) {\n#endif\n  p->count = 1;\n}\n}\n"
    assert [o for o, _ in sites(code)] == ["Inner"]


def test_comments_and_strings_hold_no_access():
    assert sites('void f(Inner *p) { /* p->count */ char *s = "p->count"; }') == []


def test_a_file_sees_the_data_its_includes_declare_and_not_another_files():
    extra = {
        "include/b.h": "typedef struct Other Other;\nextern Other *g_outer;\n",
        "src/c.c": '#include "b.h"\nvoid g(void) { g_outer->count; }\n',
    }
    texts = {"include/a.h": HEADER, "src/a.c": '#include "a.h"\nvoid f(void) { g_outer->part; }\n', **extra}
    found = access.Types(texts)
    found.learn(texts)
    owners = {s.path: s.owner for s in found.sites(texts, ["part", "count"]) if s.path != "include/a.h"}
    assert owners == {"src/a.c": "Outer", "src/c.c": "Other"}


def test_a_record_defined_twice_is_told():
    texts = {"a.h": "struct Owner { s32 a; s32 b; };", "b.h": "struct Owner { s32 b; s32 c; };"}
    found = access.Types(texts)
    assert found.defined["struct Owner"] == 2 and found.twice == {("struct Owner", "b")}
    assert set(found.member["struct Owner"]) == {"a", "b", "c"}
