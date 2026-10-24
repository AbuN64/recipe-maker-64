"""Naming portability is enforced before evidence exceptions and before any project write."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from unbake.contracts import Refusal

from recipemaker import naming, structure, style, tree


@pytest.mark.parametrize("name", ["class", "_Private", "Bad$Name", "A" * 248])
def test_name_judgment_cannot_bypass_portability_with_debug_or_sdk_evidence(monkeypatch, name):
    monkeypatch.setattr(naming, "_exists", lambda ctx, row: "")
    ctx = SimpleNamespace(taken=set())
    row = {"kind": "function", "target": "func_8000", "name": name, "doc": "original", "evidence": [f"debug {name}"]}
    assert naming.judge(ctx, row)[0] == "portable"
    assert not style.legal("function", name)


def test_name_judgment_catches_casefolded_existing_symbol(monkeypatch):
    monkeypatch.setattr(naming, "_exists", lambda ctx, row: "")
    ctx = SimpleNamespace(taken={"Actor_Draw"})
    row = {"kind": "function", "target": "func_8000", "name": "actor_draw", "doc": "draw", "evidence": ["tag actor"]}
    assert naming.judge(ctx, row)[0] == "duplicate"


@pytest.mark.parametrize(
    "op,target",
    [("symbol", "class"), ("type", "__Private"), ("field", "new"), ("source", "src/CON.c"), ("header", "include/a?.h")],
)
def test_structure_checks_direct_move_inputs_before_building_a_patch(tmp_path, op, target):
    before = sorted(tmp_path.iterdir())
    with pytest.raises(Refusal):
        structure.build(tmp_path, [{"op": op, "subject": "old", "target": target}], {}.get, {})
    assert sorted(tmp_path.iterdir()) == before


def test_structure_checks_collision_against_unchanged_source(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.c").write_text("int old(void) { return 1; }\n")
    (tmp_path / "src/b.c").write_text("int Draw(void) { return 2; }\n")
    with pytest.raises(Refusal, match="collides"):
        structure.build(tmp_path, [{"op": "symbol", "subject": "old", "target": "draw"}], {}.get, {})
    assert "old" in (tmp_path / "src/a.c").read_text()


def test_structure_paths_follow_all_targets():
    with pytest.raises(ValueError, match="reserved file"):
        tree.Naming().file("src", {"name": "CON", "vram": 0x8000})


def test_apply_refuses_a_tampered_saved_patch_before_git_mutates(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    old = "extern int Function(void);\n"
    (tmp_path / "src/a.c").write_text(old)
    patch = tmp_path / "bad.diff"
    patch.write_text(
        "diff --git a/src/a.c b/src/a.c\n--- a/src/a.c\n+++ b/src/a.c\n"
        '@@ -1 +1 @@\n-extern int Function(void);\n+extern int Function(void) __asm__("Other");\n'
    )
    run = Mock(side_effect=AssertionError("must refuse before git apply"))
    monkeypatch.setattr(structure.recipe_host, "run", run)
    with pytest.raises(Refusal, match="explicit asm"):
        structure.apply(SimpleNamespace(project=SimpleNamespace(root=tmp_path)), {"patch": str(patch), "host": None})
    run.assert_not_called()
    assert (tmp_path / "src/a.c").read_text() == old


def test_plain_c_renames_keep_house_style(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.c").write_text("#define Alias old\nint old(void) { return 1; }\n")
    result = structure.build(tmp_path, [{"op": "symbol", "subject": "old", "target": "Draw"}], {}.get, {})
    assert "+#define Alias Draw" in result["patch"]
    assert "__asm__" not in result["patch"]
