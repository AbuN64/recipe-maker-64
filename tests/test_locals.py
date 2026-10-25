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
