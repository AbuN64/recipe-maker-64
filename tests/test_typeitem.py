"""Type items: the record the accesses make, the include a unit gets, and the route a skip takes to its type item."""

from types import SimpleNamespace

import pytest
from test_dispatch import code, shapes

from recipemaker import dispatch, state, typeitem, work

NOW = 1_000_000.0


def access(offset, width=4, kind="i", sign="s", function="f"):
    return dispatch.Access(offset, width, kind, sign, function)


def test_a_pointer_made_by_addiu_adds_its_distance_and_one_stepped_by_a_loop_stops_the_walk():
    found = dispatch.flow(
        code(
            "addiu $s0, $a0, 0x190",
            "lw $v0, 0x50($s0)",  # 0x1E0 of the object
            "addiu $v1, $a0, 4",
            "sw $v0, 0($v1)",  # 4
            "addiu $v1, $v1, 0x10",  # a loop's own increment: the pointer no longer names a fixed offset
            "sw $v0, 0($v1)",
            "jal g",
            "addu $a1, $s0, $zero",  # the object plus 0x190 is no object: only $a0 is passed on
        ),
        0,
        "f",
    )
    assert shapes(found) == [(0x1E0, 4, "i", "s"), (4, 4, "i", "")] and found.passes == (("g", 0),)


def test_the_record_reads_an_integer_and_a_float_access_of_one_word_as_the_float():
    got = typeitem._record(
        [access(0xC), access(0xC, kind="f", sign=""), access(0x10, sign="u"), access(0x10, sign="s")], "R"
    )
    assert got.fields == ((0xC, 4, "f32"), (0x10, 4, "s32"))


def test_the_record_refuses_two_widths_of_one_offset_naming_both_functions():
    got = typeitem._record([access(8, function="a"), access(8, width=2, function="b")], "R")
    assert isinstance(got, str) and "b and a access 0x8 as 2 and 4 bytes" in got


def test_the_body_pads_the_gaps_and_keeps_a_reused_members_own_spelling():
    text = typeitem._body("R", [(0, 4, "s32", "unk0"), (0x10, 12, "CVector3", "unk10"), (0x20, 4, "f32", "unk20")])
    assert text == (
        "typedef struct R {\n    s32 unk0;\n    char pad4[0x10 - 0x4];\n    CVector3 unk10;\n"
        "    char pad1C[0x20 - 0x1C];\n    f32 unk20;\n} R;\n"
    )


def test_an_include_goes_after_the_last_one_the_first_use_can_see():
    text = '#include "a.h"\n#ifdef X\n#include "b.h"\n#endif\n#include "c.h"\nint g() { return f(1); }\n'
    line = '#include "new.h"'
    got = typeitem._after_includes(text.replace('#include "c.h"\n', f'#include "c.h"\n{line}\n'), line, "f")
    assert got.splitlines()[:6] == ['#include "a.h"', "#ifdef X", '#include "b.h"', "#endif", '#include "c.h"', line]
    inside = '#include "a.h"\n#ifdef X\n#include "b.h"\nint g() { return f(1); }\n#endif\n#include "new.h"\n'
    assert typeitem._after_includes(inside, line.replace("new.h", "new.h"), "f").splitlines()[:3] == [
        '#include "a.h"',
        "#ifdef X",
        '#include "b.h"',
    ]
    assert typeitem._after_includes(inside, '#include "new.h"', "f").splitlines()[3] == '#include "new.h"'


def test_the_calls_of_a_text_give_each_argument_and_skip_keywords():
    text = "int g(void) { if (f(a, h(b), 3)) return k(x); }"
    got = [(c, text[a:b].strip(), n) for c, a, b, n in typeitem._calls(text)]
    assert (
        ("f", "h(b)", 1) in got and ("h", "b", 0) in got and ("k", "x", 0) in got and all(c != "if" for c, _, _ in got)
    )
    assert {c for c, _, _ in [(c, 0, 0) for c, *_ in typeitem._calls(text, "k")]} == {"k"}


def test_an_item_name_carries_the_function_and_the_parameter():
    assert typeitem.item_name("func_80", 2) == "func_80.arg2" and typeitem.parse_item("func_80.arg2") == ("func_80", 2)
    assert typeitem.parse_item("func_80") is None


def test_a_skip_for_a_type_names_the_type_item_and_the_landing_returns_the_function(fakes, monkeypatch):
    monkeypatch.setattr(typeitem, "targets", lambda snapshot, function: [1])
    declared = {"func_a": ("h.h", 1, "extern void func_a(void *, void *);")}
    monkeypatch.setattr(
        work.index,
        "get",
        lambda s: SimpleNamespace(declarations=declared, identities=(), callers={}, shapes={}, extents={}),
    )
    fakes.items = [{"member": "func_a", "kind": "function", "segment": "main", "size": 50, "address": 1}]
    state.take(fakes.config, "func_a", NOW)
    got = work.skip(fakes.config, {"item": "func_a", "reason": "type-unresolved"})
    assert got["next"] == "recipe work func_a.arg1 --kind type"
    assert typeitem.needs(fakes.config) == {"func_a": {"item": "func_a.arg1", "next": got["next"]}}
    assert state.blocked(fakes.config)["func_a"]["func_a"] == "extern void func_a(void *, void *);"
    typeitem.done(fakes.config, "func_a")
    assert typeitem.needs(fakes.config) == {}
    assert work.skip(fakes.config, {"item": "func_a", "reason": "budget"})["next"] == "recipe next"


@pytest.mark.parametrize("reason", ["m2c-failed", "jump-table", "other"])
def test_other_skip_reasons_route_nowhere(fakes, reason):
    state.take(fakes.config, "func_a", NOW)
    assert work.skip(fakes.config, {"item": "func_a", "reason": reason})["next"] == "recipe next"
    assert typeitem.needs(fakes.config) == {}
