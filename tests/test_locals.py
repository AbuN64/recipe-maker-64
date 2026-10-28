"""Decompiler-named locals and parameters: the pack gives source evidence, a submit re-derives it."""

import json
from types import SimpleNamespace

import pytest

from recipemaker import locals_, naming

SRC = (
    "void Pool_Fill(int count, void *dst);\n"
    "int Actor_Step(Actor *a, int arg1) {\n"
    "    int var_s0;\n    int i;\n    int temp_v0;\n    int sp1C;\n"
    "    temp_v0 = a->health;\n"
    "    for (var_s0 = 0; var_s0 < 4; var_s0++) { tab[var_s0] = 0; }\n"
    "    Pool_Fill(arg1, a);\n"
    "    return temp_v0;\n}\n"
)


def project(tmp_path, files=None):
    for path, text in (files or {"src/AI/NAV/a.CPP": SRC}).items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path))


def pack(config, **given):
    params = {"step": "L", "count": 40, "target": "NAV", "host": None, **given}
    return naming.next_(config, params)


def test_pack_lists_each_slot_with_its_evidence_and_counts_the_bare_ones(tmp_path):
    config = project(tmp_path)
    got = pack(config)
    card = json.loads((tmp_path / got["pack"]).read_text())["targets"][0]
    by = {s["name"]: s for s in card["slots"]}
    assert card["function"] == "Actor_Step" and card["line"] == 2
    assert by["arg1"]["evidence"] == ["param Pool_Fill count"] and by["arg1"]["kind"] == "param"
    assert by["temp_v0"]["evidence"] == ["member health", "role return"]
    assert by["var_s0"]["evidence"] == ["role loop", "role index"]
    assert got["no_evidence"] == 1 and got["slots"] == 3  # sp1C is never used


def test_count_reads_every_folder_in_one_pass_and_writes_no_pack(tmp_path):
    """`recipe name count` gives `next`'s numbers per folder (cleanup asked `next` per folder: 231 s)."""
    config = project(tmp_path, {"src/AI/NAV/a.CPP": SRC, "src/AI/HUD/b.CPP": SRC.replace("Actor_Step", "Hud_Step")})
    got = naming.count(config, {"steps": "L", "targets": ["NAV", "HUD", "NOPE"], "host": None})
    assert got["folders"] == {
        "NAV": {"L": {"namable": 3, "no_evidence": 1}},
        "HUD": {"L": {"namable": 3, "no_evidence": 1}},
        "NOPE": {"L": {"namable": 0, "no_evidence": 0}},
    }
    assert not (tmp_path / ".unbake").exists()  # read-only: no pack
    with pytest.raises(Exception, match="count takes steps"):
        naming.count(config, {"steps": "T", "targets": ["NAV"], "host": None})


def test_prototypes_index_every_name_once():
    texts = ["void F(int, char *name, int arg2);\nint G(void) {\n    if (x) {\n    }\n}\n"]
    every = locals_.prototypes(texts, None)
    assert every["F"] == locals_.prototypes(texts, ["F"])["F"] and "if" not in every


def test_one_function_target_packs_that_function_only(tmp_path):
    config = project(tmp_path, {"src/AI/a.CPP": SRC + SRC.replace("Actor_Step", "Other_Step")})
    got = pack(config, target="Other_Step")
    assert [c["function"] for c in json.loads((tmp_path / got["pack"]).read_text())["targets"]] == ["Other_Step"]


def submit(config, tmp_path, **row):
    base = {
        "kind": "local",
        "target": "arg1",
        "name": "count",
        "path": "src/AI/NAV/a.CPP",
        "line": 2,
        "function": "Actor_Step",
        "doc": "Entries to fill.",
        "evidence": ["param Pool_Fill count"],
    }
    (tmp_path / "n.jsonl").write_text(json.dumps({**base, **row}) + "\n")
    return naming.submit(config, {"file": str(tmp_path / "n.jsonl"), "host": None, "no_apply": True})


def test_submit_renames_a_placeholder_param_inside_its_function_only(tmp_path):
    config = project(tmp_path)
    got = submit(config, tmp_path)
    assert got["accepted"] == 1 and "+int Actor_Step(Actor *a, int count) {" in got["patch"]
    assert "+    Pool_Fill(count, a);" in got["patch"]


@pytest.mark.parametrize(
    "row, rule",
    [
        ({"evidence": ["param Pool_Fill dst"]}, "evidence"),
        ({"evidence": []}, "evidence"),
        ({"name": "var_s1"}, "name"),
        ({"name": "a"}, "duplicate"),
        ({"target": "i", "name": "idx", "line": 4}, "target"),
    ],
)
def test_submit_refuses_unchecked_evidence_slot_names_and_portable_targets(tmp_path, row, rule):
    got = submit(project(tmp_path), tmp_path, **row)
    assert got["refused"] == 1 and got["by_rule"] == {rule: 1}


def test_prototypes_skip_unnamed_and_slot_params():
    got = locals_.prototypes(["void F(int, char *name, int arg2);\nint G(void) {\n}\n"], ["F", "G"])
    assert (got["F"] == [["", "name", ""]] and got["G"] == [[]]) or got["G"] == [[""]]


def test_preview_lists_outside_files_and_applies_only_on_request(tmp_path):
    config = project(tmp_path)
    built = {"moves": 1, "files": ["src/AI/NAV/a.CPP", "src/ANIM/b.CPP"], "path": "p.patch"}
    (tmp_path / "src/ANIM").mkdir(parents=True)
    seen = naming._preview(config, {"step": "T", "target": "Actor_Step"}, built)
    assert seen["outside"] == ["src/ANIM/b.CPP"] and not seen["applied"] and "--apply" in seen["next"]
    refused = naming._preview(config, {"step": "T", "target": "Actor_Step", "apply": True}, built)
    assert "--allow-outside" in refused["apply_refused"]


HEADER = "typedef struct Actor {\n    int unk0;\n    int unk4;\n    int unk8;\n} Actor;\ntypedef struct Other {\n    int unk0;\n} Other;\n"  # noqa: E501
USE = (
    "void Spawn_At(int health, int count);\n"
    "void Use(Actor *a, Other *o, int hp) {\n"
    "    a->unk0 = hp;\n"
    "    o->unk0 = 7;\n"
    "    Spawn_At(a->unk4, 1);\n"
    "    hp = a->unk8;\n"
    "}\n"
)


def members_project(tmp_path):
    return project(tmp_path, {"include/a.h": HEADER, "src/G/use.c": USE})


def test_member_pack_types_accesses_so_another_records_member_is_not_counted(tmp_path):
    config = members_project(tmp_path)
    got = naming.next_(config, {"step": "M", "count": 40, "target": "Actor", "host": None})
    card = json.loads((tmp_path / got["pack"]).read_text())["targets"][0]
    by = {s["name"]: s["evidence"] for s in card["slots"]}
    assert card["struct"] == "Actor" and card["path"] == "include/a.h"
    assert by == {"unk0": ["stored hp"], "unk4": ["param Spawn_At health"], "unk8": ["loaded hp"]}


def field(tmp_path, **row):
    base = {
        "kind": "field",
        "struct": "Actor",
        "target": "unk4",
        "name": "vitality",
        "doc": "Starting health.",
        "evidence": ["param Spawn_At health"],
    }
    (tmp_path / "n.jsonl").write_text(json.dumps({**base, **row}) + "\n")
    return naming.submit(members_project(tmp_path), {"file": str(tmp_path / "n.jsonl"), "host": None, "no_apply": True})


def test_member_rename_patches_the_record_and_its_typed_accesses(tmp_path):
    got = field(tmp_path)
    assert got["accepted"] == 1 and "+    int vitality;" in got["patch"] and "Spawn_At(a->vitality, 1);" in got["patch"]


@pytest.mark.parametrize(
    "row, rule",
    [
        ({"evidence": ["stored hp"]}, "evidence"),
        ({"name": "unk4A"}, "name"),
        ({"name": "unk0"}, "name"),
        ({"name": "hp"}, "duplicate"),
        ({"target": "vitality"}, "target"),
        ({"struct": "Nope"}, "target"),
    ],
)
def test_member_rename_refuses_unchecked_evidence_and_bad_names(tmp_path, row, rule):
    assert field(tmp_path, **row)["by_rule"] == {rule: 1}


def test_debug_catalog_names_are_evidence_for_the_function_they_belong_to(monkeypatch):
    from recipemaker import debug

    body = "{\n    int sp1C;\n    sp1C = 0;\n}\n"
    assert locals_.suggestions(body, "sp1C", {}, ("pIntel", "TargetInfo")) == ["debug pIntel", "debug TargetInfo"]
    assert locals_.verify(body, "sp1C", ["debug TargetInfo"], {}, ("TargetInfo",)) == ""
    assert "not in the function" in locals_.verify(body, "sp1C", ["debug TargetInfo"], {}, ())
    fn = debug.Function("Turret_TrackTarget", "us-rev1", 0x80203000, "T.CPP", 1, 40, ("pTurret",), locals=("pIntel",))
    cat = debug.Catalog("c", (), (fn,), ())
    monkeypatch.setattr(debug, "load", lambda config: cat)
    monkeypatch.setattr(debug.api, "symbols", lambda config, v: {"Turret_Track": (0x80203000, 0, 4, "func")})
    assert debug.variables(object()) == {"Turret_Track": ("pTurret", "pIntel")}


def test_a_body_line_is_evidence_for_a_slot_when_it_lies_in_the_function_and_holds_the_quote():
    source = ["void f(void)", "{", "    int sp1C;", "    sp1C = g_count + 1;", "}"]
    body = "\n".join(source[1:])
    args = (body, "sp1C", None, {}, (), (1, 5), source)
    ok = locals_.verify(body, "sp1C", ["body L4: counter plus one"], {}, (), (1, 5), source)
    assert ok == ""
    assert locals_.verify(body, "sp1C", ['body L4: "g_count + 1"'], {}, (), (1, 5), source) == ""
    assert "outside the function" in locals_.verify(body, "sp1C", ["body L9: x"], {}, (), (1, 5), source)
    assert "does not hold" in locals_.verify(body, "sp1C", ['body L3: "g_count"'], {}, (), (1, 5), source)
    assert "outside the function" in locals_.verify(body, "sp1C", ["body L4: x"], {}, ())  # no line range given
    assert "needs evidence" in locals_.verify(*args[:2], [], *args[3:5])
    mixed = ["body L4: x", "param nothing here"]
    assert "not in the function" in locals_.verify(body, "sp1C", mixed, {}, (), (1, 5), source)


def test_canonical_slots_keep_named_and_shadowed_bindings_but_name_only_proposals_refuse(tmp_path):
    text = "int F(int arg0, int count) { int var_s0; { int var_s0; } return arg0; }\n"
    function = locals_.functions(text)[0]
    slots = locals_.slots(text, function)
    assert [s.name for s in slots] == ["arg0", "count", "var_s0", "var_s0"]
    assert [s.name for s in locals_.candidates(text, function)] == ["arg0"]
    assert len({(s.scope, s.offset) for s in slots}) == 4
    config = project(tmp_path, {"src/AI/NAV/a.CPP": text})
    got = submit(
        config,
        tmp_path,
        target="var_s0",
        name="index",
        function="F",
        line=1,
        evidence=["body L1: two different bindings"],
    )
    assert got["refused"] == 1


def test_whole_tracked_census_uses_the_canonical_api(monkeypatch, tmp_path):
    from unbake import api

    config = project(tmp_path)
    seen = []
    result = {"counts": {"macro": 2}, "paths": [], "semantic_acceptance": False}
    monkeypatch.setattr(api, "naming_census", lambda c: seen.append(c) or result)
    got = naming.count(config, {"steps": "C"})
    assert got["census"] == result and seen == [config]
    with pytest.raises(Exception, match="whole tracked census"):
        naming.count(config, {"steps": "C", "targets": ["src"]})
