"""Slices (T3): one owner per placeholder target, the lane ownership files, suggestions, and the `slices` verb."""

import json

import pytest
from test_naming import project  # noqa: F401

from recipemaker import naming, slices


def fn(n, group="FX", vram=None, size=200, parent=None):
    vram = vram or 0x80100000 + n * 0x100
    return {
        "key": slices.fn_key("us-rev1", vram),
        "name": f"func_{vram:08X}",
        "group": group,
        "parent": parent or group,
        "vram": vram,
        "size": size,
    }


def test_chunks_cut_into_balanced_slices_within_the_caps():
    items = list(range(250))
    got = slices.chunks(items, lambda i: 100)
    assert [len(c) for c in got] == [84, 83, 83] or max(map(len, got)) <= slices.MAX_FNS
    assert sum(map(len, got)) == 250 and [i for c in got for i in c] == items
    heavy = slices.chunks(list(range(50)), lambda i: 1000)  # 50 KB of code: two slices by bytes
    assert len(heavy) == 2 and abs(len(heavy[0]) - len(heavy[1])) <= 1


def doc_of(fns, data=(), records=(), lanes=16, **more):
    return slices.compute(fns, list(data), list(records), lanes, **more)


def test_every_function_has_one_slice_and_a_small_tail_joins_its_neighbour():
    fns = [fn(i, "FX") for i in range(120)] + [fn(200 + i, "FX_2", parent="FX") for i in range(10)]
    doc = doc_of(fns)
    sl = [s for s in doc["slices"] if s["kind"] == "slice"]
    assert sum(len(s["fns"]) for s in sl) == 130 and len(doc["owner"]) == 130
    assert len(sl) == 2 and all(len(s["fns"]) <= slices.MAX_FNS for s in sl)  # 120 -> 2 slices, FX_2 joined one
    assert sl[1]["groups"][-1] == "FX_2" or sl[0]["groups"][-1] == "FX_2"


def test_data_read_by_one_slice_belongs_to_it_and_shared_data_goes_to_globals():
    a, b = fn(1, "AAA"), fn(2, "BBB")
    one = {"key": slices.datum_key("us-rev1", 0x800E0000), "name": "D_800E0000", "readers": [a["name"]]}
    two = {"key": slices.datum_key("us-rev1", 0x800E0004), "name": "D_800E0004", "readers": [a["name"], b["name"]]}
    none = {"key": slices.datum_key("us-rev1", 0x800E0008), "name": "D_800E0008", "readers": []}
    doc = doc_of([a, b], [one, two, none], lanes=8)
    assert doc["owner"][one["key"]] == doc["owner"][a["key"]]
    assert doc["owner"][two["key"]] == "G01" and none["key"] not in doc["owner"] and doc["unread"] == [none["key"]]
    assert doc["by_name"]["D_800E0004"] == two["key"] and doc["by_name"][a["name"]] == a["key"]


def test_records_with_unnamed_members_go_to_struct_slices_by_file_and_lanes_scale_them():
    recs = [{"key": f"R{i}", "tag": f"Rec{i}", "path": f"include/H{i // 2}.h", "unknown": 3} for i in range(8)]
    one = doc_of([fn(1)], records=recs, lanes=8)
    four = doc_of([fn(1)], records=recs, lanes=32)
    assert len([s for s in one["slices"] if s["kind"] == "struct"]) == 1
    structs = [s for s in four["slices"] if s["kind"] == "struct"]
    assert len(structs) == 4 and sum(s["unknown"] for s in structs) == 24
    assert four["records"]["Rec0"] == structs[0]["id"] and four["records"]["Rec7"] == structs[-1]["id"]


def test_the_ownership_check_refuses_a_target_outside_the_lane_slice_and_allows_suggestions():
    a, b = fn(1, "AAA"), fn(2, "BBB")
    doc = doc_of([a, b])
    slices.assign(doc, "lane-1", ["S001"])
    slices.assign(doc, "lane-2", ["S002"])
    mine = {"key": a["key"], "name": "Fx_Thing", "doc": "x", "evidence": ["callee y"]}
    assert slices.check(doc, "lane-1", mine) == ("", "")
    rule, why = slices.check(doc, "lane-2", mine)
    assert rule == "owner" and "S001" in why and "suggest" in why
    assert slices.check(doc, "lane-2", {**mine, "kind": "suggest"}) == ("", "")
    assert slices.check(doc, "lane-9", mine)[0] == "owner"  # no slice
    legacy = {"kind": "function", "target": a["name"], "name": "Fx_Thing"}
    assert slices.check(doc, "lane-1", legacy) == ("", "") and slices.check(doc, "lane-2", legacy)[0] == "owner"
    slot = {"kind": "local", "target": "var_s0", "function": a["name"], "name": "count"}
    assert slices.check(doc, "lane-1", slot) == ("", "") and slices.check(doc, "lane-2", slot)[0] == "owner"
    assert slices.check(doc, "lane-2", {"key": "slot:" + a["key"][3:] + ":sp1C"})[0] == "owner"
    assert slices.check(None, "x", mine) == ("", "")  # no slice file, no gate
    assert slices.owner_of(doc, "fn:" + a["key"].split(":")[2]) == "S001"  # the short key form


def test_assignment_moves_a_slice_between_lanes_and_survives_a_recompute(tmp_path):
    fns = [fn(1, "AAA"), fn(2, "BBB")]
    doc = doc_of(fns)
    slices.assign(doc, "x", ["S001", "S002"])
    slices.assign(doc, "y", ["S002"])
    assert doc["assigned"] == {"x": ["S001"], "y": ["S002"]}
    again = doc_of(fns, previous=doc)
    assert again["assigned"] == doc["assigned"]
    slices.save(doc, tmp_path)
    assert slices.load(tmp_path)["assigned"] == doc["assigned"] and slices.load(tmp_path / "none") is None


def test_lane_files_carry_the_keys_the_ledger_gates_on(tmp_path):
    a = fn(1, "AAA")
    d = {"key": slices.datum_key("us-rev1", 0x800E0000), "name": "D_800E0000", "readers": [a["name"]]}
    rec = {"key": "Pool", "tag": "Pool", "path": "include/P.h", "unknown": 2}
    doc = doc_of([a], [d], [rec])
    slices.assign(doc, "lane-1", ["S001"])
    slices.assign(doc, "lane-2", ["T01", "S001x"])
    wrote = slices.write_lane_files(doc, tmp_path)
    one = json.loads((tmp_path / "slices/lane-1.json").read_text())
    assert one == {
        "lane": "lane-1",
        "slice": "S001",
        "kind": "slice",
        "fns": [a["key"]],
        "data": [d["key"]],
        "records": [],
    }
    two = json.loads((tmp_path / "slices/lane-2.json").read_text())
    assert two["records"] == ["Pool"] and two["slice"] == "T01" and two["kind"] == "struct"
    slices.assign(doc, "lane-2", [])
    slices.write_lane_files(doc, tmp_path)
    assert not (tmp_path / "slices/lane-2.json").exists() and len(wrote) == 2


def test_suggestions_are_read_for_the_owner_by_either_key_form(tmp_path):
    key = slices.fn_key("us-rev1", 0x80100100)
    rows = [
        {"key": "fn:80100100", "name": "Fx_A", "lane": "L2", "suggest": True},
        {"key": slices.slot_key(key, "sp1C"), "name": "count", "lane": "L3", "suggest": True},
        {"key": "fn:80100200", "name": "Other", "lane": "L3"},
    ]
    (tmp_path / "suggest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "garbage\n")
    got = slices.suggestions_for([key], tmp_path)
    assert [r["name"] for r in got[key]] == ["Fx_A", "count"]
    assert slices.suggestions_for([key], tmp_path / "none") == {}


def test_module_map_regroups_units_and_ignores_the_header_row(tmp_path):
    (tmp_path / "m.tsv").write_text("unit\tmodule\n# note\nsrc/a.c\tMENU_PAUSE\nsrc/b.c\tMENU_PAUSE\n")
    assert slices.read_map(tmp_path / "m.tsv") == {"src/a.c": "MENU_PAUSE", "src/b.c": "MENU_PAUSE"}
    a, b = {**fn(1, "AAA"), "unit": "src/a.c"}, {**fn(2, "BBB"), "unit": "src/b.c"}
    doc = doc_of([a, b], module_of=slices.read_map(tmp_path / "m.tsv"))
    assert len([s for s in doc["slices"] if s["kind"] == "slice"]) == 1


def test_the_slices_verb_writes_the_document_lane_files_and_assignments(project, tmp_path):  # noqa: F811
    params = {"host": None, "action": "slices", "step": None, "lanes": 8, "out": str(tmp_path), "assign": ["l1=S001"]}
    got = naming.run(project, params)
    doc = slices.load(tmp_path)
    assert got["slices"] == len(doc["slices"]) and doc["assigned"] == {"l1": ["S001"]}
    assert (tmp_path / "slices/l1.json").is_file() and any("slices" in line for line in got["summary"])
    owned = json.loads((tmp_path / "slices/l1.json").read_text())["fns"]
    assert owned and all(k.startswith("fn:us:") for k in owned)
    # only --assign: the document is kept, not recomputed
    again = naming.run(project, {**params, "lanes": None, "assign": ["l2=S002"]})
    assert slices.load(tmp_path)["assigned"].get("l2") == ["S002"] and again["slices"] == got["slices"]
    with pytest.raises(Exception, match="--assign takes"):
        naming.run(project, {**params, "lanes": None, "assign": ["bad"]})


def test_a_missing_map_is_a_refusal_naming_the_path_and_a_merged_unit_in_the_map_is_harmless(project, tmp_path):  # noqa: F811
    params = {"host": None, "action": "slices", "step": None, "lanes": 8, "out": str(tmp_path), "assign": []}
    with pytest.raises(Exception, match=r"module map .*gone\.tsv does not exist"):
        naming.run(project, {**params, "map": str(tmp_path / "gone.tsv")})
    (tmp_path / "m.tsv").write_text("unit\tmodule\nsrc/merged_away.c\tMENU_PAUSE\n")
    got = naming.run(project, {**params, "map": str(tmp_path / "m.tsv")})
    assert got["slices"] > 0


def test_the_slicer_reads_the_projects_own_module_map_when_no_map_is_given(project, tmp_path):  # noqa: F811
    (project.project.root / "docs").mkdir(exist_ok=True)
    (project.project.root / "docs/M.tsv").write_text("unit\tmodule\nsrc/a.c\tX\n")
    project.project.module_map = "docs/M.tsv"
    assert slices.map_path(project, None) == project.project.root / "docs/M.tsv"
    project.project.module_map = "docs/none.tsv"
    with pytest.raises(Exception, match="does not exist"):
        slices.map_path(project, None)


def test_a_tree_that_changes_under_the_slicer_is_refused_with_the_way_out(project, monkeypatch):  # noqa: F811
    from unbake.contracts import Finding, Refusal

    from recipemaker import structure

    def moving(config, host=None):
        raise Refusal(Finding("recipe.evidence", "the tree changed between unbake map and its capture"))

    monkeypatch.setattr(structure, "evidence", moving)
    with pytest.raises(Exception, match="changed while the slices were cut"):
        naming.slices_evidence(project, None)
