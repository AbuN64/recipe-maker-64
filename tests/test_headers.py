"""headers: what the project's headers and sources declare, read from their text."""

from pathlib import Path
from types import SimpleNamespace

from recipemaker import headers, world


def snapshot(tmp_path: Path, files: dict[str, str], groups=()):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)

    def read(path):
        return (tmp_path / path).read_bytes()

    def peek(path):
        return read(path) if (tmp_path / path).is_file() else None

    layout = SimpleNamespace(groups={g: None for g in groups})
    project = SimpleNamespace(root=tmp_path)
    return SimpleNamespace(config=SimpleNamespace(project=project), overlays={}, digest=str(tmp_path), read=read,
                           peek=peek, layout=layout)  # fmt: skip


def test_the_catalog_holds_the_first_header_declaration_of_each_name(tmp_path):
    snap = snapshot(
        tmp_path,
        {
            "include/a.h": "extern int gA;\nvoid f(int x);\ntypedef struct S { int a; } S;\n",
            "include/b.h": "extern int gA;\nextern char *gB[], *gC;\n",
        },
    )
    found = headers.catalog(snap)
    assert found["gA"] == ("include/a.h", 1, "extern int gA;") and found["f"][2] == "void f(int x);"
    assert found["struct S"][0] == "include/a.h" and found["gC"][0] == "include/b.h"


def test_sources_list_one_kind_under_a_folder_with_overlays_laid_over(tmp_path):
    snap = snapshot(tmp_path, {"include/a.h": "", "include/x.txt": "", "src/m.CPP": "", "src/n.c": ""})
    snap.overlays = {"include/new.h": b"", "include/a.h": None}
    assert headers.sources(snap) == ["include/new.h"]
    assert headers.sources(snap, "src", ".c") == ["src/m.CPP", "src/n.c"]


def test_externs_name_every_source_declaration_with_its_type(tmp_path):
    snap = snapshot(tmp_path, {"src/a.c": "extern s32 gX;\nvoid f(void) {\n    extern u8 gY[4];\n}\n"})
    found = headers.externs(snap)
    assert found["gX"] == [("src/a.c", 1, "s32")]


def test_a_name_declared_two_ways_is_one_conflict_and_a_definition_is_canonical(tmp_path):
    snap = snapshot(
        tmp_path,
        {
            "include/a.h": "extern s32 gX;\nint f(int a);\n",
            "include/b.h": "extern f32 gX;\n",
            "src/f.c": '#include "a.h"\nint f(void) { return 0; }\n',
        },
    )
    _, found = headers.disagreements(snap)
    by = {f.unit: f for f in found}
    assert set(by) == {"gX", "f"} and "declared 2 ways and defined nowhere" in by["gX"].reason
    assert by["f"].symptoms["definition"]["path"] == "src/f.c"


def test_consumers_are_the_modules_a_header_reaches_through_other_headers(tmp_path, monkeypatch):
    files = {
        "include/a.h": "",
        "include/b.h": '#include "a.h"\n',
        "src/m.c": '#include "b.h"\n',
        "src/n.c": "",
    }
    snap = snapshot(tmp_path, files, groups=("src/m.c", "src/n.c"))
    monkeypatch.setattr(world, "of", lambda s: s.layout)
    assert headers.consumers(snap, ["include/a.h"]) == ("src/m.c",)
    assert headers.consumers(snap, []) == ()


def test_normal_blanks_attributes_and_plain_spells_gnu_words():
    text = 'int f(void) __attribute__((noreturn));\n# 1 "a.h" 1 3\nstatic __inline__ int g(void);\n'
    out = headers.normal(text)
    assert "__attribute__" not in out and out.count("\n") == text.count("\n")
    assert '# 1 "a.h"\n' in out and "static inline int g(void);" in out


def test_the_source_and_header_suffixes_are_the_ones_of_unbakes_unit_contract():
    from unbake import config

    for path in ("a.c", "A.CPP", "src/x/y_2.cpp", "a.h", "A.H", "a.s", "a.txt", "a.hpp", "a.cc"):
        assert headers.is_source(path) == config.is_source(path) and headers.is_source(path) == (
            path.lower().endswith((".c", ".cpp"))
        )
        assert headers.is_header(path) >= config.is_header(path)  # unbake's headers are among ours
