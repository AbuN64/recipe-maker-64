"""recipe fuzzy: the fence text, the m2c debt count and the land path against a real local bare remote."""

import subprocess
from pathlib import Path

from recipemaker import fuzzy

MODULE = (
    '#include "include_asm.h"\nvoid a(void) {}\n'
    'INCLUDE_ASM("span_1000", func_80200610_de);\n'
    'INCLUDE_ASM("span_1000", func_80200611_de);\n'
)


def test_fence_replaces_only_the_named_stand_in():
    out = fuzzy.fence(MODULE, "func_80200610_de", "void func_80200610_de(void) {}")
    assert out == (
        '#include "include_asm.h"\nvoid a(void) {}\n#ifdef NON_MATCHING\nvoid func_80200610_de(void) {}\n#else\n'
        'INCLUDE_ASM("span_1000", func_80200610_de);\n#endif\nINCLUDE_ASM("span_1000", func_80200611_de);\n'
    )


def test_fence_skips_a_function_already_fenced_or_absent():
    fenced = fuzzy.fence(MODULE, "func_80200610_de", "void f(void) {}")
    assert fuzzy.fence(fenced, "func_80200610_de", "x") is None
    assert fuzzy.fence(MODULE, "func_nothing", "x") is None


def test_debt_counts_m2c_leftovers():
    assert fuzzy.debt("void f(void) { s32 var_v0; arg0->x = M2C_ERROR(1); }") == 3
    assert fuzzy.debt("void f(Player *p) { p->x = 1; }") == 0


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_land_file_commits_one_file_and_pushes_fast_forward(tmp_path: Path):
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "a.CPP").write_text(MODULE)
    _git(work, "add", "."), _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(work, "remote", "add", "origin", str(bare)), _git(work, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "b.CPP").write_text("x\n")
    _git(other, "add", "."), _git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "other")
    _git(other, "push", "-q", "origin", "main")  # the branch moved under our feet
    text = fuzzy.fence(MODULE, "func_80200610_de", "void func_80200610_de(void) {}")
    got = fuzzy.land_file(work, "origin", "main", "a.CPP", MODULE, text, ["func_80200610_de"])
    assert got["landed"] and got["subject"].startswith("Fuzzy m2c body for func_80200610_de")
    log = subprocess.run(["git", "log", "--format=%an <%ae>|%s", "-1"], cwd=bare, capture_output=True, text=True).stdout
    assert log.startswith("mo <itsmoalawi@gmail.com>|Fuzzy m2c body")
    assert (work / "b.CPP").exists()  # synced to the moved tip before writing


def test_land_file_refuses_a_module_that_moved(tmp_path: Path):
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "a.CPP").write_text(MODULE + "// moved\n")
    _git(work, "add", "."), _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(work, "remote", "add", "origin", str(bare)), _git(work, "push", "-q", "origin", "main")
    got = fuzzy.land_file(work, "origin", "main", "a.CPP", MODULE, MODULE, ["f"])
    assert not got["landed"] and "changed" in got["why"]


def test_fence_defines_m2c_macros_once_per_module():
    first = fuzzy.fence(MODULE, "func_80200610_de", "void f(void) { M2C_FIELD(a, int *, 4); }")
    assert first.count("typedef int M2C_UNK;") == 1
    both = fuzzy.fence(first, "func_80200611_de", "void g(void) { M2C_FIELD(a, int *, 8); }")
    assert both.count("typedef int M2C_UNK;") == 1 and both.count("M2C_FIELD(a") == 2


def test_amend_drops_the_clashing_header_and_declares_the_undeclared():
    text = '#include "AI/X.H"\n#include "TYPES.H"\ns32 f(void) { return D_1 + D_2; }\n'
    why = "include/AI/X.H:18: conflicting types for `D'; `D_1' undeclared (first use in this function)"
    out = fuzzy.amend(text, why)
    assert '#include "AI/X.H"' not in out and "extern s32 D_1;" in out and out.index("extern") > out.index("TYPES.H")
    assert fuzzy.amend(out, "some other error") == out


def test_land_renames_lands_only_when_renameproof_holds(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    bare, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "a.CPP").write_text("void func_1(void) {}\n")
    _git(work, "add", "."), _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(work, "remote", "add", "github", str(bare)), _git(work, "push", "-q", "github", "main")
    _git(work, "worktree", "add", "-q", "--detach", str(tmp_path / "wt"))
    wt = tmp_path / "wt"
    patch = tmp_path / "r.patch"
    patch.write_text("--- a/a.CPP\n+++ b/a.CPP\n@@ -1 +1 @@\n-void func_1(void) {}\n+void Real_Name(void) {}\n")
    config = SimpleNamespace(project=SimpleNamespace(root=wt))
    monkeypatch.setattr(fuzzy, "renamed", lambda c, against: "not a pure rename")
    refused = fuzzy.land_renames(config, patch, "Rename func_1 to Real_Name")
    assert not refused["landed"] and "pure rename" in refused["why"]
    assert (wt / "a.CPP").read_text() == "void func_1(void) {}\n"
    monkeypatch.setattr(fuzzy, "renamed", lambda c, against: "")
    got = fuzzy.land_renames(config, patch, "Rename func_1 to Real_Name")
    assert got["landed"]
    shown = subprocess.run(["git", "show", "HEAD:a.CPP"], cwd=bare, capture_output=True, text=True).stdout
    assert "Real_Name" in shown
