"""The folder tree: segment labels, libraries, call-graph subsystems bounded by file count, data ownership, paths."""

import json
from pathlib import Path

import pytest
from test_structure import DATA, SETTINGS, _one_unit, answer, names, project

from recipemaker import state, structure, tree


@pytest.fixture(autouse=True)
def calm(monkeypatch):
    monkeypatch.setattr(structure.time, "sleep", lambda s: None)
    monkeypatch.setattr(structure, "_dirty", lambda root: set())


@pytest.fixture
def doc():
    return json.loads(Path(DATA).read_text(encoding="utf-8"))


def by_name(proposal):
    return {m["name"]: m for m in proposal["modules"]}


def test_segments_fold_per_version_spans_into_main_and_overlays():
    seg = lambda name, vram: {"name": name, "versions": {"us": [0x1000, 0x2000, vram], "eu": [0x9000, 0xA000, vram]}}  # noqa: E731
    doc = {
        "segments": [seg("span_1000", 0x80200000), seg("span_164000", 0x80400000), seg("span_166000", 0x80400000)],
        "modules": [
            {"name": n, "segment": s, "members": [f"f_{n}"]}
            for n, s in (("a", "span_1000"), ("b", "span_164000"), ("c", "span_166000"))
        ],
        "members": [{"name": f"f_{n}", "kind": "function"} for n in "abc"],
    }
    assert tree.segments(doc) == {"a": "main", "b": "ovl_80400000", "c": "ovl_80400000"}
    one = {**doc, "segments": doc["segments"][:1], "modules": doc["modules"][:1]}
    assert tree.segments(one) == {"a": ""}  # a one-segment game has no level


def test_the_proposal_folds_the_fixture_into_main_and_an_overlay(doc):
    got = by_name(structure.propose(doc, SETTINGS))
    assert got["code_80001000"]["label"] == "main" and got["code_80002000"]["label"] == "ovl_80002000"
    assert got["code_80002000"]["source"] == "src/game/ovl_80002000/sys_80002000/code_80002000.c"
    assert got["code_80002000"]["header"] == "include/game/ovl_80002000/sys_80002000.h"


def _region(count=8, leak=0, outside=4):
    """`count` modules in a row that call each other and are called from three game modules before and after them."""
    rows = [{"name": f"g{i}", "label": "", "vram": 0x1000 + i * 0x10} for i in range(6)]
    rows += [{"name": f"l{i}", "label": "", "vram": 0x2000 + i * 0x10} for i in range(count)]
    rows += [{"name": f"h{i}", "label": "", "vram": 0x3000 + i * 0x10} for i in range(6)]
    game = [f"g{i}" for i in range(6)] + [f"h{i}" for i in range(6)]
    calls = {(f"l{i}", f"l{i + 1}"): 5 for i in range(count - 1)}
    calls |= {(f"g{i}", "l0"): 3 for i in range(6)} | {(f"h{i}", f"l{i % count}"): 3 for i in range(6)}
    calls |= {(a, b): 12 for a in game for b in game if a != b}  # game code talks to itself a lot
    calls |= {(f"l{i}", "g0"): leak for i in range(count)} if leak else {}
    return rows, calls


def test_a_leaf_closed_region_becomes_one_library_named_after_its_first_module():
    rows, calls = _region()
    got = tree.libraries(rows, calls, SETTINGS)
    assert got == {f"l{i}": "lib_00002000" for i in range(8)}


def test_a_region_that_leaks_or_is_small_is_no_library():
    rows, calls = _region(count=5)
    assert tree.libraries(rows, calls, SETTINGS) == {}
    rows, calls = _region()
    calls[("l2", "g0")] = 40
    assert tree.libraries(rows, calls, SETTINGS) == {}
    rows, calls = _region(leak=1)  # one call out of every module: 8 of 43
    loose = state.Structure(library_leak=0.5)
    assert len(tree.libraries(rows, calls, loose)) == 8 and tree.libraries(rows, calls, SETTINGS) == {}
    assert tree.libraries(rows, calls, state.Structure(library_leak=0.5, library_fan_in=99)) == {}


def test_catalog_hits_rename_module_files_and_split_by_library(doc):
    for m in doc["members"]:
        hit = {"f1": ("libx", "contread"), "f2": ("libx", "contread"), "f3": ("liby", "pimgr")}.get(m["name"])
        m.update(sdk_library=hit[0], sdk_module=hit[1], sdk_name=f"os_{m['name']}") if hit else None
    got = structure.propose(doc, SETTINGS)
    mods = by_name(got)
    assert mods["contread"]["atoms"] == ["f1", "f2"] and mods["pimgr"]["atoms"] == ["f3"]
    assert (mods["contread"]["source"], mods["contread"]["header"]) == ("src/sdk/libx/contread.c", "include/sdk/libx.h")
    assert mods["pimgr"]["source"] == "src/sdk/liby/pimgr.c"
    assert got["symbols"] == {"f1": "os_f1", "f2": "os_f2", "f3": "os_f3"}
    batches = structure._batches(got)
    assert batches[0][0] == "symbols" and {"op": "symbol", "subject": "f1", "target": "os_f1"} in batches[0][1]
    assert [b for b, _ in batches][1].startswith("src/sdk/")  # libraries right after the symbols


def test_one_c_unit_stays_one_module_whatever_the_catalog_says_of_its_later_functions(doc):
    # BT src/code_8011E3F0.c (drvrnew + two unlisted functions) and src/func_8011D0E0.c (sptaskyield + sptaskyielded)
    hits = {"f1": ("libx", "contread"), "f2": ("libx", "contread"), "f3": ("liby", "pimgr")}
    for m in doc["members"]:
        hit = hits.get(m["name"])
        m.update(sdk=True, sdk_library=hit[0], sdk_module=hit[1], sdk_name=m["name"]) if hit else None
    _one_unit(doc, "f2", "f3", "f4")  # f4 has no hit: its sdk flag differs, which is a hard cut between two units
    mods = by_name(structure.propose(doc, SETTINGS))
    assert mods["contread"]["atoms"] == ["f1", "f2", "f3", "f4"] and "pimgr" not in mods


def _graph(clusters, links=()):
    nodes = [n for c in clusters for n in c]
    mods = [{"name": n, "vram": 0x1000 + 0x10 * i} for i, n in enumerate(sorted(nodes, key=lambda n: (len(n), n)))]
    graph = {(a, b): 4 for c in clusters for i, a in enumerate(c) for b in c[i + 1 :]}
    return mods, graph | dict(links)


def test_interleaved_call_clusters_are_two_subsystems_and_the_result_is_deterministic():
    odd, even = [f"m{i}" for i in range(1, 11, 2)], [f"m{i}" for i in range(0, 10, 2)]
    mods = [{"name": f"m{i}", "vram": 0x1000 + 0x10 * i} for i in range(10)]
    graph = {(a, b): 4 for c in (odd, even) for i, a in enumerate(c) for b in c[i + 1 :]} | {("m0", "m1"): 1}
    one, two = tree.subsystems(mods, graph, SETTINGS), tree.subsystems(list(reversed(mods)), dict(graph), SETTINGS)
    assert one == two == [even, odd]  # each folder interleaves with the other in ROM order


def test_the_file_bound_splits_a_40_file_cluster_and_merges_a_2_file_one():
    big = [f"b{i:02d}" for i in range(40)]
    mods = [{"name": n, "vram": 0x1000 + 0x10 * i} for i, n in enumerate(big)]
    graph = {(big[i], big[j]): 1 for i in range(40) for j in range(i + 1, 40)}
    got = tree.subsystems(mods, graph, SETTINGS)
    assert len(got) == 2 and all(len(g) <= 32 for g in got) and sorted(n for g in got for n in g) == sorted(big)
    small = ["s0", "s1"]
    rows = [*mods[:8], {"name": "s0", "vram": 0x9000}, {"name": "s1", "vram": 0x9010}]
    graph = {(big[i], big[j]): 4 for i in range(8) for j in range(i + 1, 8)} | {("s0", "s1"): 9, ("s1", big[2]): 1}
    got = tree.subsystems(rows, graph, SETTINGS)
    assert got == [[*big[:8], *small]]  # the 2-file cluster joins the neighbour it is linked to


def test_files_of_owned_data_count_toward_the_bound():
    mods = [{"name": f"n{i}", "vram": 0x1000 + i, "files": 4} for i in range(9)]
    graph = {(f"n{i}", f"n{j}"): 1 for i in range(9) for j in range(i + 1, 9)}
    got = tree.subsystems(mods, graph, SETTINGS)
    assert len(got) == 2 and all(
        4 * len(g) <= 32 for g in got
    )  # 9 nodes of 4 files are 36 files, one folder is too many


def _with_data(doc, name, us, eu, loaders):
    place = [
        {"version": "us", "section": ".data", "rom_start": us, "rom_end": us + 16, "vram": us},
        {"version": "eu", "section": ".data", "rom_start": eu, "rom_end": eu + 16, "vram": us},
    ]
    doc["members"].append(
        {
            "name": name,
            "kind": "data",
            "state": "c",
            "module": "code_old1",
            "unit": f"src/{name}.c",
            "fuzzy": None,
            "sdk": False,
            "padded": False,
            "placements": place,
            "calls": [],
            "loads": [],
            "strings": [],
        }
    )
    doc["units"].append(
        {
            "path": f"src/{name}.c",
            "kind": "data",
            "module": "code_old1",
            "members": [name],
            "toolchain": f"gcc-{name}",
            "options": {"add": [], "omit": []},
            "withheld": [],
        }
    )
    for m in doc["members"]:
        if m["name"] in loaders:
            m["loads"] = [*m["loads"], name]


def test_data_goes_into_its_module_beside_it_to_shared_or_to_the_data_folder(doc):
    _with_data(doc, "dA", 0x1180, 0x11180, ["f4"])  # starts where f6 ends in both versions, only f4 loads it
    _with_data(doc, "dB", 0x9000, 0x19000, ["f1"])  # owned, apart
    _with_data(doc, "dC", 0xA000, 0x1A000, ["f1", "g1"])  # two subsystems, no winner
    _with_data(doc, "dD", 0xB000, 0x1B000, [])  # nobody loads it
    got = structure.propose(doc, SETTINGS)
    mods = by_name(got)
    assert "dA" in mods["code_800010C0"]["atoms"] and not [m for m in mods if mods[m]["atoms"] == ["dA"]]
    owned = next(m for m in mods.values() if m["atoms"] == ["dB"])
    assert (
        owned["source"].startswith("src/game/main/sys_80001000/data_")
        and owned["header"] == "include/game/main/sys_80001000.h"
    )
    shared = next(m for m in mods.values() if m["atoms"] == ["dC"])
    assert shared["source"].startswith("src/game/main/shared/data_")
    loose = next(m for m in mods.values() if m["atoms"] == ["dD"])
    assert loose["source"].startswith("src/data/span_1000/data_")
    flat = [m for _, g in structure._batches(got) for m in g]
    assert {"op": "member", "subject": "dA", "target": "code_800010C0"} in flat
    assert structure._batches(got)[-2][0].startswith(
        ("src/game/main/shared", "src/data/")
    )  # shared data last before headers


def test_the_rule_d_winner_needs_twice_the_loads_and_room():
    room = {"a": 1, "b": 1}
    home = {"x1": "a", "x2": "a", "x3": "a", "y": "b"}
    got = tree.data_owner({"d1": ["x1", "x2", "y"], "d2": ["x1", "y"], "d3": ["x1", "x2", "x3", "y"]}, home, room, 32)
    assert got == {"d1": "a", "d2": "", "d3": "a"} and room == {"a": 3, "b": 1}
    assert tree.data_owner({"d": ["x1", "x2"]}, home, {"a": 32}, 32) == {"d": ""}


def test_a_collision_suffix_is_the_second_atoms_vram_never_a_counter(doc):
    got = structure.propose(doc, SETTINGS)
    f = next(a for a in structure.atoms(doc) if a["name"] == "f1")
    g = next(a for a in structure.atoms(doc) if a["name"] == "f2")
    assert tree.unique("code_80001000", {"code_80001000"}, [f, g], "us") == "code_80001000_80001040"
    assert tree.unique("code_80001000", {"code_80001000", "code_80001000_80001040"}, [f, g], "us").startswith(
        "code_80001000_"
    )
    assert not [n for n in names(got) if n.rsplit("_", 1)[-1].isdigit() and len(n.rsplit("_", 1)[-1]) < 4]


def test_a_fuzzy_row_without_a_module_goes_to_its_rom_predecessors_module(doc):
    doc["members"].append(
        {
            "name": "fz",
            "kind": "function",
            "state": "fuzzy",
            "module": "",
            "unit": None,
            "fuzzy": {"score": 1},
            "sdk": False,
            "padded": False,
            "placements": [
                {"version": "us", "section": ".text", "rom_start": 0x1090, "rom_end": 0x10A0, "vram": 0x80001090}
            ],
            "calls": [],
            "loads": [],
            "strings": [],
        }
    )
    got = structure.propose(doc, SETTINGS)
    assert "fz" in by_name(got)["code_80001000"]["atoms"]  # after f3 (0x1080), before f4 (0x10c0)
    assert tree.fuzzy_home(doc, structure.atoms(doc), "us", {"f3": "code_80001000"}) == {"fz": "code_80001000"}


def test_library_dirs_units_enter_the_atoms(doc):
    member = next(m for m in doc["members"] if m["name"] == "f9")
    unit = next(u for u in doc["units"] if u["path"] == member["unit"])
    member["module"] = "gone"
    unit["path"] = member["unit"] = "src/libgcc/f9.c"
    assert "f9" not in {a["name"] for a in structure.atoms(doc)}
    assert "f9" in {a["name"] for a in structure.atoms(doc, None, ("src/libgcc",))}


def test_estimate_gates_are_printed_with_the_tree_numbers(doc):
    got = structure.propose(doc, SETTINGS)
    assert got["gates"] == {"dir_max_ok": True, "padding_zero": True, "func_files_zero": True}
    assert got["tree"]["dir_max"] <= 32 and got["tree"]["depth"] == 4 and got["tree"]["func_files"] == 0
    assert {"c_files", "data_files", "dir_median", "padding_files", "api_headers", "subsystems", "libraries"} <= set(
        got["tree"]
    )


def test_apply_retries_a_busy_lock_twice_then_succeeds(tmp_path, monkeypatch):
    busy = ({"ok": False, "result": None, "findings": [{**structure_finding(), "key": "setup.busy"}]}, "")
    config, fake, _, _ = project(tmp_path, monkeypatch, [busy, busy, answer()])
    slept = []
    monkeypatch.setattr(structure.time, "sleep", slept.append)
    got = structure.apply(config, {"host": None})
    assert len(fake.moves()) == 3 and slept == [5, 15] and got["applied"] == 17 and len(got["unbake_calls"]) == 3


def structure_finding():
    from test_structure import FINDING

    return FINDING


def test_moves_on_dirty_files_wait_for_the_end_of_the_stream(tmp_path, monkeypatch):
    config, fake, result, saved = project(tmp_path, monkeypatch, [answer(), answer(), answer(), answer()])
    stream = [json.loads(x) for x in result["stream"].splitlines()]
    monkeypatch.setattr(structure, "_dirty", lambda root: {"include/b.h"})
    for _ in range(2):
        structure.apply(config, {"host": None})
    got = structure.apply(config, {"host": None})
    assert [d["subject"] for d in got["deferred"]] == ["include/b.h"] and got["applied"] == 4 and got["remaining"] == 1
    assert [len(c[1]) for c in fake.moves()] == [17, 3, 4] and all(
        m["subject"] != "include/b.h" for m in fake.moves()[-1][1]
    )
    last = [json.loads(x) for x in (saved / "moves.jsonl").read_text(encoding="utf-8").splitlines()]
    assert last[-1] == next(m for m in stream if m["subject"] == "include/b.h") and len(last) == len(stream)
    again = structure.apply(config, {"host": None})
    assert (again["applied"], again["remaining"], len(fake.moves())) == (0, 1, 3)
    monkeypatch.setattr(structure, "_dirty", lambda root: set())
    assert structure.apply(config, {"host": None})["applied"] == 1


def test_a_changed_journal_in_a_refusal_is_retried_too(tmp_path, monkeypatch):
    stale = {"key": "journal.changed", "reason": "r"}
    first = (
        {
            "ok": False,
            "result": {"refused": [{"index": 0, "findings": [{**structure_finding(), **stale}]}]},
            "findings": [],
        },
        "",
    )
    config, fake, _, _ = project(tmp_path, monkeypatch, [first, answer()])
    assert structure.apply(config, {"host": None})["applied"] == 17 and len(fake.moves()) == 2


def test_the_dominance_knob_is_named_in_recipe_knowledge_and_the_shared_headers_come_from_the_layout():
    assert tree.table("knowledge/structure.toml")["dominance"] == 4
    assert tree.shared_headers() == ("include/types.h", "include/game/common.h")


def test_a_catalog_name_two_members_claim_or_a_member_already_has_renames_none_of_them(doc):
    for m in doc["members"]:
        hit = {"f1": "osYield", "f2": "osYield", "f3": "os_f3", "f4": "f5"}.get(m["name"])
        m.update(sdk_library="libx", sdk_module="mod", sdk_name=hit) if hit else None
    got = structure.propose(doc, SETTINGS)
    assert got["symbols"] == {"f3": "os_f3"}  # f1 and f2 claim one name; f4 would take f5's name


@pytest.mark.parametrize(
    "naming", [tree.PLAIN, tree.Naming(case="upper", stem="{vram}", stem_max=8, source_ext=".CPP")]
)
def test_module_paths_use_the_first_atom_address_not_the_minimum_across_versions(doc, naming):
    every = [a for a in structure.atoms(doc) if a["name"] in {"f1", "f2", "f4"}]
    for a in every:
        version, address = {"f1": ("eu", 0x80001040), "f2": ("de", 0x80001000), "f4": ("us", 0x80001000)}[a["name"]]
        place = {**next(iter(a["text"].values())), "version": version, "vram": address}
        a.update(text={version: place}, placements=[place], calls=[], loads=[])
    modules = [
        {"name": "code_80001040", "segment": "span_1000", "origin": "code_old1", "atoms": ["f1", "f2"]},
        {"name": "code_80001000", "segment": "span_1000", "origin": "code_old2", "atoms": ["f4"]},
    ]
    tree.place(doc, every, modules, SETTINGS, naming)
    assert [m["vram"] for m in modules] == [0x80001040, 0x80001000]
    assert len({m["source"] for m in modules}) == 2
    if naming.stem == "{vram}":
        assert [Path(m["source"]).name for m in modules] == ["80001040.CPP", "80001000.CPP"]


@pytest.mark.parametrize(
    "naming", [tree.PLAIN, tree.Naming(case="upper", stem="{vram}", stem_max=8, source_ext=".CPP")]
)
def test_address_style_keeps_the_existing_identity_collision_choice(doc, naming):
    every = [a for a in structure.atoms(doc) if a["name"] in {"f1", "f2", "f4"}]
    by_name = {a["name"]: a for a in every}
    for a in every:
        version, address = {"f1": ("eu", 0x80001000), "f2": ("us", 0x80001040), "f4": ("us", 0x80001000)}[a["name"]]
        place = {**next(iter(a["text"].values())), "version": version, "vram": address}
        a.update(text={version: place}, placements=[place], calls=[], loads=[])
    taken, modules = set(), []
    for members in (["f4"], ["f1", "f2"]):
        name, address = tree.identity("code_80001000", taken, [by_name[n] for n in members], "us")
        taken.add(name)
        modules.append(
            {"name": name, "address": address, "segment": "span_1000", "origin": "code_old1", "atoms": members}
        )
    tree.place(doc, every, modules, SETTINGS, naming)
    assert [m["vram"] for m in modules] == [0x80001000, 0x80001000]
    assert [m["address"] for m in modules] == [0x80001000, 0x80001040]
    assert len({m["source"] for m in modules}) == 2
    if naming.stem == "{vram}":
        assert [Path(m["source"]).name for m in modules] == ["80001000.CPP", "80001040.CPP"]


def test_project_layout_shared_headers_are_used_for_data_and_header_moves(doc, tmp_path):
    (tmp_path / "config.toml").write_text('[layout]\nshared_headers = ["include/TYPES.H", "include/COMMON.H"]\n')
    naming = tree.style(tmp_path)
    assert naming.shared_headers == ("include/TYPES.H", "include/COMMON.H")
    data = {"name": "data_80001000", "vram": 0x80001000, "folder": "src/data", "library": "", "sub": ""}
    assert tree.paths(data, naming)[1] == "include/COMMON.H"
    doc["headers"].append(
        {"path": "include/types.h", "modules": [], "declares": ["f1"], "includes": [], "included_by": []}
    )
    moves = structure.headers(doc, 0.8, naming=naming)
    assert {"op": "file", "subject": "include/types.h", "target": "include/TYPES.H"} in moves
    assert not any(m["target"] in tree.shared_headers() for m in moves)
    assert (
        structure._cluster_header(["include/game/A.H", "include/game/B.H"], naming.shared_headers[1])
        == "include/game/COMMON.H"
    )


def test_project_named_subsystems_replace_the_templated_name(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[naming]\nsubsystem = "{label}/{vram}"\n[naming.subsystems]\n"MAIN/80208000" = "AUDIO"\n', encoding="utf-8"
    )
    naming = tree.style(tmp_path)
    assert naming.named("MAIN/80208000") == "AUDIO"
    assert naming.named("MAIN/80255C40") == "MAIN/80255C40"
