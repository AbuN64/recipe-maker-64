"""The twin rung: a landed function of the same shape, its source carried over and renamed."""

from dataclasses import replace

from fakes import unit as make_unit

from recipemaker import twin

SOURCE = (
    b'#include "a.h"\n\nint helper(void);\n\n'
    b"int func_b(int x) {\n    if (x) {\n        return callee_b(x);\n    }\n    return 0;\n}\n"
)
LOCAL = (
    b'#include "a.h"\n\ntypedef struct Own {\n    int q;\n} Own;\nextern int D_gone_b;\nint helper(void);\n'
    b"int other(void) {\n    return 1;\n}\nstatic inline int bit(int v) {\n    return v | 1;\n}\n"
    b"int func_b(Own *o) {\n    return bit(o->q);\n}\n"
)


def with_twin(fakes, *groups):
    fakes.with_units({"src/b.c": make_unit("src/b.c", ("func_b",))})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": SOURCE})
    fakes.shapes = {f"s{i}": tuple(g) for i, g in enumerate(groups or [("func_a", "func_b")])}


def test_candidate_carries_the_types_data_and_static_helpers_the_twin_source_defines_but_no_other_function(fakes):
    with_twin(fakes)
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": LOCAL})
    text = twin.candidate(fakes.snapshot, "func_a")
    assert text is not None and "typedef struct Own" in text and "extern int D_gone_b;" in text
    assert "static inline int bit" in text and "int func_a(Own *o)" in text
    assert "other" not in text and "helper" not in text and text.count("#include") == 1


def test_a_landed_function_of_the_same_shape_is_the_clone(fakes):
    with_twin(fakes)
    found = twin.find(fakes.snapshot, "func_a")
    assert found is not None
    assert (found.counterpart, found.version, found.other_version, found.unit) == ("func_b", "a", "a", "src/b.c")
    text = twin.candidate(fakes.snapshot, "func_a")
    assert text is not None and text.startswith('#include "a.h"\n')
    assert "int func_a(int x) {" in text and "func_b" not in text and "helper" not in text


def test_candidate_includes_the_header_that_declares_a_name_the_swap_brought_in(fakes, monkeypatch):
    with_twin(fakes)
    monkeypatch.setattr(twin.draft, "declared_names", lambda snap: {"func_a": ("include/c.h", 3, "x")})
    text = twin.candidate(fakes.snapshot, "func_a")
    assert text is not None and text.startswith('#include "a.h"\n#include "c.h"\n')


def test_no_landed_function_of_the_shape_means_no_twin(fakes):
    assert twin.find(fakes.snapshot, "func_a") is None
    with_twin(fakes, ("func_a",), ("func_b", "data_d"))
    assert twin.find(fakes.snapshot, "func_a") is None
    assert twin.candidate(fakes.snapshot, "func_a") is None


def test_data_has_no_twin(fakes):
    with_twin(fakes, ("data_d", "func_b"))
    assert twin.find(fakes.snapshot, "data_d") is None


def test_ready_counts_the_open_functions_that_share_a_shape_with_a_landed_one_and_alike_lists_them(fakes):
    with_twin(fakes, ("func_a", "func_b", "data_d"))
    assert twin.ready(fakes.snapshot) == {"func_a"}
    assert twin.alike(fakes.snapshot, "func_b") == ["data_d", "func_a"]
    assert twin.alike(fakes.snapshot, "func_a") == ["data_d"]


def test_a_function_is_never_its_own_twin(fakes):
    fakes.with_units({"src/a.c": make_unit("src/a.c", ("func_a",))})
    fakes.shapes = {"s": ("func_a",)}
    assert twin.find(fakes.snapshot, "func_a") is None


def test_swap_replaces_whole_words_only_and_all_at_once():
    assert twin.swap("a ab b", {"a": "b", "b": "a"}) == "b ab a"
    assert twin.swap("x", {}) == "x"
