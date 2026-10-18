"""m2c's context and the prototypes a draft takes from the project headers."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from unbake.contracts import Finding, Refusal

from recipemaker import signatures

HEADER = (
    "#ifndef G\n#define G\ntypedef int s32;\nextern s32 gCount;\nvoid known(int a);\n"
    "struct Pair { int a; int b; };\n#endif\n"
)

CATALOG = {
    "func_a": ("include/f.h", 3, "void func_a(s32 x);"),
    "gCount": ("include/f.h", 4, "extern s32 gCount;"),
}


def test_signature_and_declarations_come_from_the_header_catalog(monkeypatch):
    monkeypatch.setattr(signatures.headers, "catalog", lambda snap: CATALOG)
    assert signatures.signature(None, ["func_a", "gCount", "none"]) == {"func_a": "void func_a(s32 x)"}
    assert signatures.declarations(None, ["func_a", "gCount", "none"]) == {
        "func_a": "void func_a(s32 x);",
        "gCount": "extern s32 gCount;",
    }


def snap(tmp_path: Path, files: dict[str, str]):
    root = tmp_path / "proj"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return SimpleNamespace(config=SimpleNamespace(project=SimpleNamespace(root=root)), overlays={})


def _stub(monkeypatch, text="int cat;\n"):
    calls = []
    monkeypatch.setattr(signatures.headers, "catalog", lambda s: {})
    monkeypatch.setattr(signatures.headers, "sources", lambda s: ["include/g.h"])
    monkeypatch.setattr(signatures.cpp, "run", lambda c, p, src, v: calls.append(src) or text)
    return calls


def test_context_preprocesses_every_header_once_and_is_named_by_its_content(tmp_path, monkeypatch):
    calls = _stub(monkeypatch)
    out = signatures.context(snap(tmp_path, {"include/g.h": HEADER}), "a")
    assert out.suffix == ".i" and out.read_text() == "int cat;\n" and calls == ['#include "g.h"\n']
    _stub(monkeypatch, "int dog;\n")
    assert signatures.context(snap(tmp_path, {"include/g.h": HEADER}), "a") != out


def test_context_refusal_is_draft_context(tmp_path, monkeypatch):
    _stub(monkeypatch)

    def boom(*a):
        raise Refusal(Finding("native.exit", "cpp: no such header"))

    monkeypatch.setattr(signatures.cpp, "run", boom)
    with pytest.raises(Refusal) as error:
        signatures.context(snap(tmp_path, {"include/g.h": HEADER}), "a")
    assert error.value.findings[0].key == "draft.context" and "no such header" in error.value.findings[0].reason


def test_neutralise_blanks_file_scope_asm_and_guards_sizeof_casts():
    text = '# 1 "a.h"\ntypedef int T;\n__asm__(".include \\"x;y\\"\\n"\n  ".end");\nint g(void) { return 2; }\n'
    text += "char pad[0x40 - sizeof(T) - sizeof(struct S)];\n"
    out = signatures._neutralise(text)
    assert out.count("\n") == text.count("\n") and "__asm__" not in out and "typedef int T;" in out
    assert "int g(void) { return 2; }" in out and "0x40 - (sizeof(T)) - (sizeof(struct S))" in out


def test_context_keeps_the_declaration_of_the_header_the_catalog_names_and_no_jump_table():
    text = (
        '# 1 "include/a.h"\nextern struct Game gSettings;\nextern int jtbl_800DE300[];\nextern int gSame;\n'
        '# 1 "include/b.h"\nextern struct Menu gSettings;\nextern char *gOther[], *gTitle[];\nextern int gSame;\n'
        '# 1 "include/c.h"\nextern int gTitle[];\n'
    )
    text = "struct Game { int a; };\nstruct Menu { int b; };\n" + text
    catalog = {"gSettings": ("include/b.h", 1, ""), "gTitle": ("include/c.h", 1, "")}
    out = signatures._agree(text, catalog)
    assert out.count("\n") == text.count("\n") and len(out) == len(text)
    assert "struct Game gSettings" not in out and "extern struct Menu gSettings;" in out
    assert "jtbl_800DE300" not in out and out.count("extern int gSame;") == 2  # agreeing declarations both stay
    assert "gOther" not in out and "extern int gTitle[];" in out  # a statement goes whole
