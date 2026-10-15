"""Moved tests for the m2c signature and header context code, with unbake's seams replaced by mocks."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tomlkit
from fakes import native_result
from unbake import layout, recipes
from unbake import view as view_module
from unbake.contracts import (
    Config,
    Host,
    LayoutMap,
    Member,
    Placement,
    Project,
    Refusal,
    Snapshot,
)

from recipemaker import signatures

HEADER = (
    "#ifndef G\n#define G\ntypedef int s32;\nextern s32 gCount;\nvoid known(int a);\n"
    "struct Pair { int a; int b; };\n#endif\n"
)

AGREED: dict = {}  # what headers.disagreements answers: name -> the one type its declarations agree on
CONFLICTS: list = []
HEADER_DECLARATIONS: dict = {}  # what headers.catalog answers: name -> (path, line, statement)


def _document(**tables):
    if "global_" in tables:
        tables["global"] = tables.pop("global_")
    return {"schema": 1, "function": {}, "global": {}, "struct": {}, **tables}


def _with_doc(snapshot, doc):
    return replace(snapshot, overlays={**snapshot.overlays, "types.toml": tomlkit.dumps(doc).encode()})


def _member(name, kind="function", holders=("a", "b"), section=".text", size=8):
    placements = tuple(
        Placement(
            v,
            section,
            0x1000,
            0x1000 if section == ".bss" else 0x1000 + size,
            0x80000400,
            size if section == ".bss" else 0,
        )
        for v in holders
    )
    return Member(name, kind, "asm" if kind == "function" else "data", "group", placements)


def _document(**tables):
    if "global_" in tables:
        tables["global"] = tables.pop("global_")
    return {"schema": 1, "function": {}, "global": {}, "struct": {}, **tables}


def _with_doc(snapshot, doc):
    return replace(snapshot, overlays={**snapshot.overlays, "types.toml": tomlkit.dumps(doc).encode()})


def _member(name, kind="function", holders=("a", "b"), section=".text", size=8):
    placements = tuple(
        Placement(
            v,
            section,
            0x1000,
            0x1000 if section == ".bss" else 0x1000 + size,
            0x80000400,
            size if section == ".bss" else 0,
        )
        for v in holders
    )
    return Member(name, kind, "asm" if kind == "function" else "data", "group", placements)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    host = Host(
        2,
        2,
        1024,
        1024,
        tmp_path / "cache",
        1024,
        tmp_path / "tools",
        {"m2c": tmp_path / "m2c"},
        None,
        2,
        2,
        0.8,
        2,
        ("test", "test@invalid"),
        {},
        "host",
    )
    project = Project(tmp_path, "test", "test", "Test", ("a", "b"), "a", "test-tc", {}, {}, {}, {}, {}, "project")
    cfg = Config(project, host, "config")
    members = {name: _member(name) for name in ("alpha", "zeta")}
    members["buffer"] = _member("buffer", "data", section=".data", size=0x18)
    snapshot = Snapshot(cfg, "head", LayoutMap({}, members, {}, "layout", (), {}), {}, {}, "snapshot")
    overlays = {
        f"asm/{v}/{name}.s": f"glabel {name}\n nop\n".encode() for name in ("alpha", "zeta") for v in ("a", "b")
    }
    snapshot = replace(
        _with_doc(snapshot, _document()),
        overlays={
            **_with_doc(snapshot, _document()).overlays,
            **overlays,
            "include/h.h": b"header",
        },
    )
    span = Mock()

    @contextmanager
    def stage(_name):
        yield span

    work_dirs = []

    @contextmanager
    def work(config):
        assert config is cfg
        path = tmp_path / f"work-{len(work_dirs)}"
        path.mkdir()
        work_dirs.append(path)
        yield path

    cache = {}
    cache_calls = []

    def cached(config, kind, key, produce):
        assert config is cfg
        cache_calls.append((kind, key))
        if (kind, key) not in cache:
            cache[kind, key] = produce()
        return cache[kind, key]

    def run(_stage, argv, cwd, **kw):
        assert cwd == tmp_path
        name = argv[argv.index("--function") + 1]
        return native_result(
            stdout=(f"  int   {name}(int *arg0) {{\n arg0->unk10; arg0->unk4; arg0->unk4;\n}}\n").encode()
        )

    runner = Mock(side_effect=run)
    context = tmp_path / "context.i"
    context.write_bytes(b"int a_header(void);\nint z_header(void);")
    catalog = Mock(return_value=context)

    def keyed(config, name, fn, jobs, key=None):  # the pool answers a warm job from its key and runs only the rest
        out = []
        for job in jobs:
            cache_calls.append((name, key(job)))
            if (name, key(job)) not in cache:
                cache[name, key(job)] = fn(job)
            out.append(cache[name, key(job)])
        return out

    mapper = Mock(side_effect=keyed)
    monkeypatch.setattr(signatures.effort, "stage", stage)
    monkeypatch.setattr(signatures, "context", catalog)
    monkeypatch.setattr(
        signatures.headers, "sources", lambda snap, place="include", suffix=".h": ["include/h.h"] * (place == "include")
    )
    monkeypatch.setattr(signatures.headers, "disagreements", lambda snap, defined=(): (AGREED, CONFLICTS))
    monkeypatch.setattr(signatures.headers, "catalog", lambda snap, version: HEADER_DECLARATIONS)
    HEADER_DECLARATIONS.clear()
    AGREED.clear()
    CONFLICTS.clear()
    monkeypatch.setattr(
        signatures.layout,
        "unit_of",
        lambda snap, name: next((unit for unit in snap.layout.units.values() if name in unit.members), None),
    )
    return snapshot, runner, span, catalog, mapper, cache_calls


def test_signature_reads_the_prototype_types_toml_holds(lane):
    snapshot, *_ = lane
    row = {"signature": "int alpha(int *arg0)", "evidence": "landed"}
    snapshot = _with_doc(snapshot, _document(function={"alpha": row}))
    assert signatures.signature(snapshot, ["alpha", "unknown"]) == {"alpha": "int alpha(int *arg0)"}


def test_context_and_declarations(lane):
    snapshot, *_ = lane
    snapshot = _with_doc(
        snapshot,
        _document(
            function={
                **{name: {"signature": f"int {name}(void)", "evidence": "m2c"} for name in ("z", "a")},
                "h": {"signature": "int h(void)", "evidence": "authored"},
            },
            global_={"global": {"declaration": "extern int global;", "evidence": "authored"}},
            struct={"S": {"declaration": "typedef int S;", "evidence": "authored"}},
        ),
    )
    # the headers already declare what they authored, so the context repeats only what the tool derived
    assert signatures._type_map(snapshot) == "int a(void);\nint z(void);"
    assert signatures.declarations(snapshot, ["global", "a", "missing", "S"]) == {
        "global": "extern int global;",
        "a": "int a(void);",
    }


def snap(tmp_path: Path, files: dict[str, str]):
    root = tmp_path / "proj"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    group = SimpleNamespace(segment="main", name="grp")
    return SimpleNamespace(
        config=SimpleNamespace(project=SimpleNamespace(root=root, toolchain="t")),
        overlays={},
        tree=False,
        layout=SimpleNamespace(groups={"g": group}),
        read=lambda p: (root / p).read_bytes(),
        peek=lambda p: (root / p).read_bytes() if (root / p).exists() else None,
    )


def _stub_context(monkeypatch, extra, catalog=None):
    monkeypatch.setattr(signatures, "_type_map", lambda s, declared=(): extra)
    monkeypatch.setattr(signatures.headers, "catalog", lambda s: catalog or {})
    monkeypatch.setattr(layout, "overlay", lambda s, w: s)
    monkeypatch.setattr(recipes, "resolve", lambda c, u, o: None)
    monkeypatch.setattr(view_module, "get", lambda *a, **k: SimpleNamespace(text="int cat;\n", key="view"))


def test_context_includes_type_map(tmp_path, monkeypatch):
    _stub_context(monkeypatch, "typedef int mapped;\n")
    out = signatures.context(snap(tmp_path, {"include/g.h": HEADER}), "a")
    text = out.read_text()
    assert text.index("int cat;") < text.index("typedef int mapped;")
    assert out.suffix == ".i"


def test_context_key_changes_with_types(tmp_path, monkeypatch):
    s = snap(tmp_path, {"include/g.h": HEADER})
    _stub_context(monkeypatch, "one\n")
    first = signatures.context(s, "a")
    _stub_context(monkeypatch, "two\n")
    second = signatures.context(s, "a")
    assert first != second


def test_context_refusal_is_draft_context(tmp_path, monkeypatch):
    _stub_context(monkeypatch, "x\n")

    def boom(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(view_module, "get", boom)
    with pytest.raises(Refusal) as error:
        signatures.context(snap(tmp_path, {"include/g.h": HEADER}), "a")
    assert error.value.findings[0].key == "draft.context"


def test_context_keeps_only_type_map_lines_the_headers_can_read():
    text = "typedef int s32;\nstruct Query { s32 a; };\n"
    extra = "void a(s32);\nvoid b(void *, Query *);\nextern u8 resources/rsp/x.s[8];\nvoid c(struct Query *);"
    kept, dropped = signatures._parseable(text, extra)
    assert kept == "void a(s32);\nvoid c(struct Query *);\n"
    assert dropped == ["void b(void *, Query *);", "extern u8 resources/rsp/x.s[8];"]


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


def test_the_type_map_leaves_out_names_a_header_declares(lane):
    snapshot, *_ = lane
    rows = {name: {"signature": f"void {name}(void)", "evidence": "landed"} for name in ("assert", "own")}
    snapshot = _with_doc(snapshot, _document(function=rows))
    assert signatures._type_map(snapshot, {"assert": ("include/a.h", 1, "")}) == "void own(void);"
