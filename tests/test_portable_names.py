"""Naming portability is enforced before evidence exceptions and before any project write."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from unbake.contracts import Refusal

from recipemaker import naming, structure, style, tree


def _ctx(taken=(), sdk=None):
    """A Map stand-in: `sdk` is the name the project's SDK table gives each target."""
    return SimpleNamespace(taken=set(taken), members={}, canon=lambda n: n, sdk_name=lambda n: (sdk or {}).get(n, ""))


@pytest.mark.parametrize("name", ["class", "_Private", "Bad$Name", "A" * 248])
def test_name_judgment_cannot_bypass_portability_with_debug_or_sdk_evidence(monkeypatch, name):
    monkeypatch.setattr(naming, "_exists", lambda ctx, row: "")
    ctx = _ctx()
    row = {"kind": "function", "target": "func_8000", "name": name, "doc": "original", "evidence": [f"debug {name}"]}
    assert naming.judge(ctx, row)[0] == "portable"
    assert not style.legal("function", name)


def test_a_reserved_name_the_sdk_evidence_declares_the_original_is_not_refused_as_portable(monkeypatch):
    monkeypatch.setattr(naming, "_exists", lambda ctx, row: "")
    row = {
        "kind": "function",
        "target": "func_8000",
        "name": "__zorkHelper",
        "doc": "sdk",
        "evidence": ["sdk __zorkHelper"],
    }
    assert naming.judge(_ctx(sdk={"func_8000": "__zorkHelper"}), row)[0] != "portable"


def test_a_reserved_name_without_sdk_evidence_is_refused_as_portable(monkeypatch):
    monkeypatch.setattr(naming, "_exists", lambda ctx, row: "")
    row = {
        "kind": "function",
        "target": "func_8000",
        "name": "__zorkHelper",
        "doc": "sdk",
        "evidence": ["sdk __zorkHelper"],
    }
    assert naming.judge(_ctx(), row)[0] == "portable"
    assert naming.judge(_ctx(sdk={"func_8000": "__zorkOther"}), row)[0] == "portable"
    assert naming.judge(_ctx(sdk={"func_8000": "class"}), {**row, "name": "class"})[0] == "portable"


def test_name_judgment_catches_casefolded_existing_symbol(monkeypatch):
    monkeypatch.setattr(naming, "_exists", lambda ctx, row: "")
    ctx = _ctx(["Actor_Draw"])
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


def test_structure_admits_a_reserved_name_the_sdk_evidence_declares_and_refuses_the_rest(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.c").write_text("int old(void) { return 1; }\n")
    move = {"op": "symbol", "subject": "old", "target": "__zorkHelper"}
    with pytest.raises(Refusal, match="reserved identifier"):
        structure.build(tmp_path, [move], {}.get, {}, evidenced=["__zorkOther"])
    assert structure.build(tmp_path, [move], {}.get, {}, evidenced=["__zorkHelper"])["moves"] == 1


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


def _apply_rename(tmp_path, new, table):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("void zork(void) {}\n")
    (tmp_path / "versions" / "v1").mkdir(parents=True)
    (tmp_path / "versions" / "v1" / "libultra_names.tsv").write_text("vram\tname\n" + table)
    patch = f"--- a/src/a.c\n+++ b/src/a.c\n@@ -1 +1 @@\n-void zork(void) {{}}\n+void {new}(void) {{}}\n"
    structure._portable_patch(tmp_path, patch)


def test_applying_a_reserved_name_the_sdk_table_declares_original_is_not_refused(tmp_path):
    _apply_rename(tmp_path, "__zorkHelper", "80000000\t__zorkHelper\n")


def test_applying_a_reserved_name_no_sdk_table_declares_is_refused(tmp_path):
    with pytest.raises(Refusal):
        _apply_rename(tmp_path, "__zorkHelper", "80000000\tzorkOther\n")
