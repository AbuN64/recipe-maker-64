"""The twin rung: pairing, name swapping and the work it costs (one pairs call and one correspondences call)."""

from dataclasses import replace

from unbake import infer
from unbake.contracts import UnitSpec

from recipemaker import twin

SOURCE = (
    b'#include "a.h"\n\nint helper(void);\n\n'
    b"int func_b(int x) {\n    if (x) {\n        return callee_b(x);\n    }\n    return 0;\n}\n"
)


def with_twin(fakes):
    unit = UnitSpec("src/b.c", "c", "main", ("func_b",), "gcc-test", {})
    fakes.with_units({"src/b.c": unit})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": SOURCE})
    fakes.pairs = {("a", "b"): {"func_a": "func_b"}}


LOCAL = (
    b'#include "a.h"\n\ntypedef struct Own {\n    int q;\n} Own;\nextern int D_gone_b;\nint helper(void);\n'
    b"int other(void) {\n    return 1;\n}\nstatic inline int bit(int v) {\n    return v | 1;\n}\n"
    b"int func_b(Own *o) {\n    return bit(o->q);\n}\n"
)


def test_candidate_carries_the_types_data_and_static_helpers_the_twin_source_defines_but_no_other_function(fakes):
    with_twin(fakes)
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": LOCAL})
    text = twin.candidate(fakes.snapshot, "func_a")
    assert text is not None and "typedef struct Own" in text and "extern int D_gone_b;" in text
    assert "static inline int bit" in text and "int func_a(Own *o)" in text
    assert "other" not in text and "helper" not in text and text.count("#include") == 1


def test_find_names_the_counterpart_unit_and_the_name_map(fakes, monkeypatch):
    with_twin(fakes)
    monkeypatch.setattr(infer, "correspondences", lambda snap: (("callee_a", "a", "callee_b", "b"),))
    got = twin.find(fakes.snapshot, "func_a")
    assert got is not None and (got.counterpart, got.unit, got.version) == ("func_b", "src/b.c", "a")
    assert got.names == {"callee_b": "callee_a", "func_b": "func_a"}


def test_candidate_copies_the_function_with_its_includes_and_swaps_every_name(fakes, monkeypatch):
    with_twin(fakes)
    monkeypatch.setattr(infer, "correspondences", lambda snap: (("callee_a", "a", "callee_b", "b"),))
    text = twin.candidate(fakes.snapshot, "func_a")
    assert text is not None and text.startswith('#include "a.h"\n')
    assert "int func_a(int x) {" in text and "callee_a(x)" in text and "callee_b" not in text
    assert "helper" not in text  # only the one function travels


def test_candidate_includes_the_header_that_declares_a_name_the_swap_brought_in(fakes, monkeypatch):
    with_twin(fakes)
    monkeypatch.setattr(infer, "correspondences", lambda snap: (("callee_a", "a", "callee_b", "b"),))
    monkeypatch.setattr(twin.draft, "declared_names", lambda snap: {"callee_a": ("include/c.h", 3, "x")})
    text = twin.candidate(fakes.snapshot, "func_a")
    assert text is not None and text.startswith('#include "a.h"\n#include "c.h"\n')


def test_no_pair_or_an_undecompiled_counterpart_means_no_twin(fakes):
    assert twin.find(fakes.snapshot, "func_a") is None
    fakes.pairs = {("a", "b"): {"func_a": "func_b"}}
    assert twin.find(fakes.snapshot, "func_a") is None  # func_b has no decompiled unit
    assert twin.candidate(fakes.snapshot, "func_a") is None


def test_data_has_no_twin_and_costs_no_pairs_call(fakes):
    assert twin.find(fakes.snapshot, "data_d") is None
    assert fakes.calls["infer.pairs"] == 0


def test_a_warm_find_asks_unbake_once_for_pairs_and_once_for_correspondences(fakes):
    with_twin(fakes)
    twin.find(fakes.snapshot, "func_a")
    assert (fakes.calls["infer.pairs"], fakes.calls["infer.correspondences"]) == (1, 1)


def test_ready_lists_members_whose_counterpart_is_decompiled_and_who_are_not_themselves(fakes):
    with_twin(fakes)
    assert twin.ready(fakes.snapshot) == {"func_a"}
    assert fakes.calls["infer.pairs"] == 1


def test_ready_runs_one_twin_pass_per_snapshot_digest(fakes):
    with_twin(fakes)
    first = twin.ready(fakes.snapshot)
    assert twin.ready(fakes.snapshot) == first and fakes.calls["infer.pairs"] == 1


def test_swap_replaces_whole_words_only_and_all_at_once():
    assert twin.swap("a ab b", {"a": "b", "b": "a"}) == "b ab a"
    assert twin.swap("x", {}) == "x"


def same_shape(monkeypatch, *groups):
    from types import SimpleNamespace

    from recipemaker import index

    shapes = {f"s{i}": tuple(g) for i, g in enumerate(groups)}
    monkeypatch.setattr(
        index, "get", lambda snap: SimpleNamespace(declarations={}, identities=(), callers={}, shapes=shapes)
    )


def decompiled_b(fakes):
    fakes.with_units({"src/b.c": UnitSpec("src/b.c", "c", "main", ("func_b",), "gcc-test", {})})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": SOURCE})


def test_a_landed_function_of_the_same_shape_is_the_clone_when_no_other_version_has_it(fakes, monkeypatch):
    decompiled_b(fakes)
    same_shape(monkeypatch, ("func_a", "func_b"))
    found = twin.find(fakes.snapshot, "func_a")
    assert (found.counterpart, found.version, found.other_version, found.unit) == ("func_b", "a", "a", "src/b.c")
    text = twin.candidate(fakes.snapshot, "func_a")
    assert "int func_a(int x)" in text and "func_b" not in text


def test_no_landed_function_of_the_shape_means_no_clone(fakes, monkeypatch):
    decompiled_b(fakes)
    same_shape(monkeypatch, ("func_a",), ("func_b", "data_d"))
    assert twin.find(fakes.snapshot, "func_a") is None


def test_ready_counts_the_open_functions_that_share_a_shape_with_a_landed_one_and_alike_lists_them(fakes, monkeypatch):
    decompiled_b(fakes)
    same_shape(monkeypatch, ("func_a", "func_b", "data_d"))
    assert twin.ready(fakes.snapshot) == {"func_a"}
    assert twin.alike(fakes.snapshot, "func_b") == ["data_d", "func_a"]
    assert twin.alike(fakes.snapshot, "func_a") == ["data_d"]


def test_a_function_is_never_its_own_twin_even_when_another_version_holds_its_source(fakes):
    fakes.with_units({"src/a.c": UnitSpec("src/a.c", "c", "main", ("func_a",), "gcc-test", {})})
    fakes.pairs = {("a", "b"): {"func_a": "func_a"}}
    assert twin.find(fakes.snapshot, "func_a") is None


def test_a_landed_member_with_a_version_withheld_is_ready_when_find_finds_its_twin(fakes):
    fakes.with_units(
        {
            "src/a.c": UnitSpec("src/a.c", "c", "main", ("func_a",), "gcc-test", {}, withheld=("b",)),
            "src/b.c": UnitSpec("src/b.c", "c", "main", ("func_b",), "gcc-test", {}),
        }
    )
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/a.c": SOURCE, "src/b.c": SOURCE})
    fakes.pairs = {("a", "b"): {"func_a": "func_b"}}
    assert twin.find(fakes.snapshot, "func_a") is not None
    assert twin.ready(fakes.snapshot) == {"func_a"}
