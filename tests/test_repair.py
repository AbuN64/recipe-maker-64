"""Source-only name repairs preserve literals and bindings without mapping ROMs."""

import json
from types import SimpleNamespace

import pytest

from recipemaker import naming, repair, structure


def project(tmp_path, files):
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return SimpleNamespace(project=SimpleNamespace(root=tmp_path))


def test_local_submit_never_maps_and_limits_the_function(tmp_path, monkeypatch):
    config = project(
        tmp_path,
        {
            "src/a.c": (
                "int keep(int template) { return template; }\n"
                "int change(int template) { /* template */ return template + obj.template; }\n"
                'const char *word = "template";\n'
            )
        },
    )
    monkeypatch.setattr(structure, "evidence", lambda *_: pytest.fail("local rename requested a ROM map"))
    row = {
        "kind": "local",
        "target": "template",
        "name": "entry",
        "path": "src/a.c",
        "line": 2,
        "function": "change",
        "doc": "Entry to process.",
    }
    path = tmp_path / "names.jsonl"
    path.write_text(json.dumps(row) + "\n")
    result = naming.submit(config, {"file": str(path), "host": None, "no_apply": True})
    assert result["accepted"] == 1 and result["refused"] == 0
    patch = result["patch"]
    assert "+int change(int entry) { /* template */ return entry + obj.template; }" in patch
    assert "-int keep" not in patch
    assert '"template"' in (tmp_path / "src/a.c").read_text()
    assert (tmp_path / "src/a.c").read_text().count("int template") == 2  # proposal only


def test_symbol_repair_updates_headers_all_versions_and_aliases_preserves_literals(tmp_path):
    config = project(
        tmp_path,
        {
            "src/a.c": 'int Actor__Step(void) { return 1; }\nconst char *s = "Actor__Step";\n',
            "include/a.h": "int Actor__Step(void);\n",
            "versions/a/symbol_addrs.txt": "Actor__Step = 0x80001000;\n",
            "versions/b/symbol_addrs.txt": "Actor__Step = 0x80002000;\n",
            "versions/b/aliases.txt": "Actor__Step = 0x80002000;\n",
        },
    )
    row = {
        "kind": "repair",
        "target": "Actor__Step",
        "name": "Actor_Step",
        "path": "src/a.c",
        "line": 1,
        "doc": "Retire the reserved class separator.",
    }
    rule, reason, moves = repair.Repairs(tmp_path).judge(row)
    assert (rule, reason) == ("", "")
    result = naming._patch(config, {}, moves)
    assert set(result["files"]) == {
        "src/a.c",
        "include/a.h",
        "versions/a/symbol_addrs.txt",
        "versions/b/symbol_addrs.txt",
        "versions/b/aliases.txt",
    }
    assert "+int Actor_Step(void)" in result["patch"]
    assert "+const char *s" not in result["patch"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"name": "return"}, "portable"),
        ({"name": "taken"}, "duplicate"),
        ({"line": 99}, "evidence"),
        ({"path": "../outside.c"}, "evidence"),
        ({"function": "missing"}, "scope"),
        ({"target": "ordinary"}, "target"),
    ],
)
def test_local_repairs_refuse_before_writing(tmp_path, change, expected):
    text = "int f(int template) { int taken; return template; }\n"
    project(tmp_path, {"src/a.c": text})
    row = {
        "kind": "local",
        "target": "template",
        "name": "entry",
        "path": "src/a.c",
        "line": 1,
        "function": "f",
        "doc": "Input entry.",
    } | change
    rule, _, moves = repair.Repairs(tmp_path).judge(row)
    assert rule == expected and not moves
    assert (tmp_path / "src/a.c").read_text() == text


def test_prototype_parameter_and_type_repair(tmp_path):
    project(
        tmp_path, {"src/a.c": "extern void construct(int template);\ntypedef struct Shared__Entry Shared__Entry;\n"}
    )
    repairs = repair.Repairs(tmp_path)
    local = {
        "kind": "local",
        "target": "template",
        "name": "entry",
        "path": "src/a.c",
        "line": 1,
        "function": "construct",
        "doc": "Template entry.",
    }
    typedef = {
        "kind": "repair",
        "target": "Shared__Entry",
        "name": "Shared_Entry",
        "path": "src/a.c",
        "line": 2,
        "doc": "Single class separator.",
    }
    rules = [repairs.judge(row) for row in (local, typedef)]
    assert all(not rule and not why for rule, why, _ in rules)
    built = structure.build(tmp_path, [m for _, _, moves in rules for m in moves], lambda _: None, {})
    assert "+extern void construct(int entry);" in built["patch"]
    assert "+typedef struct Shared_Entry Shared_Entry;" in built["patch"]
