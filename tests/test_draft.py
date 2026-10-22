"""draft: m2c function drafts and word/byte data drafts. m2c is mocked through host.run."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fixture
import pytest
from unbake import api, effort
from unbake.contracts import Refusal, Snapshot, Version

from recipemaker import draft, host, index, judge, signatures, world
from recipemaker.world import Group, Layout, Member, Placement


@dataclass(frozen=True)
class Snap(Snapshot):
    """A snapshot that carries its layout, as the tests build it by hand."""

    layout: Any = None


CODE = bytes.fromhex("03e0000800000000")
REAL_CHECKED = draft._checked
USES: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def references(monkeypatch: pytest.MonkeyPatch) -> None:
    USES.clear()
    monkeypatch.setattr(index, "get", lambda snapshot: SimpleNamespace(uses=USES))


def _create(snapshot: Snapshot, member: str, out: Path) -> str:
    """Draft one member; the answer is why the draft does not compile, "" when it does."""
    if member in snapshot.layout.members and snapshot.layout.members[member].kind == "data":
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(draft.data_text(snapshot, member), encoding="utf-8")
        return ""
    return draft.create(snapshot, member, out)


def _words(*values: int) -> bytes:
    return b"".join(v.to_bytes(4, "big") for v in values)


def _snapshot(tmp: Path, kinds: dict[str, str] | None = None, **kw: Any) -> Snap:
    code = {"func_80000400": {"a": (0x1000, CODE), "b": (0x1000, CODE)}}
    kw.setdefault("functions", code)
    root = fixture.project(tmp, **kw)
    config = api.load(root, fixture.host(tmp))
    members, symbols, facts = {}, {}, {}
    for kind, items in (("function", kw["functions"]), ("data", kw.get("data", {}))):
        for name, by in items.items():
            places = []
            for version, (rom, blob) in sorted(by.items()):
                vram = fixture.VRAM_BASE + rom - fixture.ROM_BASE
                symbols.setdefault(version, {})[name] = vram
                facts.setdefault(version, {})[name] = (vram, rom, len(blob), "func" if kind == "function" else "data")
                section = {"rodata": ".rodata", "bss": ".bss"}.get((kinds or {}).get(name, ""), "")
                section = section or (".text" if kind == "function" else ".data")
                bss = section == ".bss"
                places.append(Placement(version, section, rom, rom if bss else rom + len(blob), vram, len(blob) * bss))
            members[name] = Member(name, kind, "src/code_80000400.c", tuple(places), False)
    group = Group("src/code_80000400.c", "main", tuple(members), False, "src/code_80000400.c")
    layout = Layout({group.name: group}, members, {}, {})
    versions = {
        v: Version(v, root / f"roms/baserom.{v}.z64", "0" * 64, "", "", symbols.get(v, {}), ())
        for v in kw.get("versions", ("a", "b"))
    }
    return Snap(config, "0" * 40, SimpleNamespace(symbols=facts), versions, {}, "0" * 64, layout=layout)


def _data_snapshot(tmp: Path, blob: bytes, kinds: dict[str, str] | None = None) -> Snapshot:
    data = {"d": {"a": (0x1200, blob)}, "e": {"a": (0x1200 + len(blob), b"\0\0\0\0")}}
    return _snapshot(tmp, kinds, data=data, versions=("a",))


def _mock_m2c(monkeypatch: pytest.MonkeyPatch, stdout: bytes, exit: int = 0, stderr: bytes = b"") -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(signatures, "context", lambda snapshot, version: Path("context.c"))
    monkeypatch.setattr(
        draft.api, "pool_map", lambda config, name, function, items, key=None: [function(i) for i in items]
    )
    monkeypatch.setattr(draft, "_checked", lambda snapshot, member, drafts: [{**d, "reason": ""} for d in drafts])

    def run(argv: Any, cwd: Path, config: Any, **kw: Any) -> Any:
        calls.append(("m2c", list(argv)))
        return fixture.native_result(exit, stdout, stderr)

    monkeypatch.setattr(host, "run", run)
    return calls


def _base(tmp: Path) -> Snapshot:
    return _snapshot(tmp)


def _with_asm(tmp: Path, text: str) -> Snapshot:
    """A snapshot whose function_80000400 has the assembly `text` in version a."""
    snapshot = _snapshot(tmp)
    asm = tmp / "project/asm/a/func_80000400.s"
    asm.parent.mkdir(parents=True)
    asm.write_text(text)
    return snapshot


@pytest.fixture(autouse=True)
def cards(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the compiler card lookup."""
    monkeypatch.setattr(draft, "m2c_target", lambda toolchain: "mips-test")
    monkeypatch.setattr(draft.state, "tool", lambda name: Path(draft.__file__))
    monkeypatch.setattr(world, "asm_path", lambda cfg, version, name: cfg.project.root / f"asm/{version}/{name}.s")


def test_function_draft_writes_m2c_output(tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _with_asm(tmp_path, "glabel func_80000400\n")
    calls = _mock_m2c(monkeypatch, b"void func_80000400(void) {\n    gDPPipeSync(gfx++);\n}\n")
    monkeypatch.setattr(draft.signatures, "declarations", lambda s, names: {})
    out = tmp_path / "out" / "draft.c"
    assert _create(snapshot, "func_80000400", out) == ""
    assert out.read_text()
    assert calls[0][0] == "m2c" and "--valid-syntax" in calls[0][1] and "--stack-structs" in calls[0][1]


def test_function_draft_declares_the_target_and_its_callees(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _with_asm(
        tmp_path, "glabel func_80000400\n/* 0 0 0 */ jal func_80000500\n/* 4 4 4 */ jal func_80000400\n"
    )
    _mock_m2c(monkeypatch, b"void func_80000400(void) {\n}\n")
    seen = []
    monkeypatch.setattr(
        draft.signatures,
        "declarations",
        lambda s, names: (
            seen.append(names)
            or {"func_80000400": "void func_80000400(void);", "func_80000500": "s32 func_80000500(s32 arg0);"}
        ),
    )
    out = tmp_path / "out" / "draft.c"
    _create(snapshot, "func_80000400", out)
    assert seen == [["func_80000400", "func_80000500"]]
    assert "s32 func_80000500(s32 arg0);\nvoid func_80000400" in out.read_text()


def test_a_draft_names_no_per_function_record_of_a_callee_and_no_m2c_unknown_type(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _with_asm(tmp_path, "glabel func_80000400\n/* 0 0 0 */ jal func_80000500\n")
    body = b"void func_80000400(void) {\n    Rec_func_80000500 *temp_v0;\n    M2C_UNK sp10;\n\n"
    body += b"    temp_v0 = func_80000500(sp10);\n}\n"
    _mock_m2c(monkeypatch, body)
    proto = "Rec_func_80000500 *func_80000500(s32 arg0);"
    monkeypatch.setattr(
        draft.signatures,
        "declarations",
        lambda s, names: {"func_80000400": "void func_80000400(void);", "func_80000500": proto},
    )
    out = tmp_path / "out" / "draft.c"
    _create(snapshot, "func_80000400", out)
    text = out.read_text()
    assert "void *func_80000500(s32 arg0);" in text and "    void *temp_v0;" in text and "    s32 sp10;" in text
    assert "_func_" not in text and "M2C_" not in text


def test_function_draft_m2c_failure_refuses(tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _with_asm(tmp_path, "glabel func_80000400\n")
    _mock_m2c(monkeypatch, b"", exit=1, stderr=b"boom\n")
    with pytest.raises(Refusal) as error:
        _create(snapshot, "func_80000400", tmp_path / "o.c")
    assert error.value.findings[0].key == "draft.m2c"


def test_function_draft_m2c_failure_names_what_m2c_printed(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _with_asm(tmp_path, "glabel func_80000400\n")
    printed = b"/*\nDecompilation failure in function f:\n\nLast instruction is missing a delay slot:\nj .L1\n*/\n"
    _mock_m2c(monkeypatch, printed, exit=1, stderr=b"")
    with pytest.raises(Refusal) as error:
        _create(snapshot, "func_80000400", tmp_path / "o.c")
    assert "missing a delay slot: j .L1" in error.value.findings[0].reason


def test_function_draft_m2c_internal_error_names_its_last_line_and_a_misaligned_jump_table(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _with_asm(tmp_path, "glabel func_80000400\n")
    jumps = tmp_path / "project/.unbake/recipe/jumps/func_80000400"
    jumps.mkdir(parents=True)
    (jumps / "tables.s").write_text("dlabel jtbl_1\n    /* A 800 */ .byte 0x00\n    /* B 801 */ .word 0x1\n")
    printed = (
        b"/*\nInternal error in function f:\n\nTraceback:\n  File x\nAssertionError: jtbl list must not be empty\n*/\n"
    )
    _mock_m2c(monkeypatch, printed, exit=1, stderr=b"")
    with pytest.raises(Refusal) as error:
        _create(snapshot, "func_80000400", tmp_path / "o.c")
    reason = error.value.findings[0].reason
    assert "AssertionError: jtbl list must not be empty" in reason
    assert "jump table jtbl_1 is misaligned (it starts with .byte)" in reason


def test_unknown_member_refuses_land_request(tmp_path: Path, toolchains: Any) -> None:
    with pytest.raises(Refusal) as error:
        _create(_base(tmp_path), "nope", tmp_path / "o.c")
    assert error.value.findings[0].key == "land.request"


def test_data_draft_words_symbols_and_rodata_const(tmp_path: Path, toolchains: Any) -> None:
    blob = _words(0x80000400, 0x8000061C, 0x12345678, 0x80000600, 0x80000608, 0x8000061E, 0x80000404)
    snapshot = _data_snapshot(tmp_path, blob, {"d": "rodata"})
    out = tmp_path / "out" / "d.c"
    _create(snapshot, "d", out)
    text = out.read_text()
    assert "extern void func_80000400(void);" in text
    assert "extern unsigned char e[];" in text
    assert text.index("extern") < text.index("const unsigned int d[] = {")
    # function f, data e, plain, itself, inside itself, inside e, inside a function (no name for the middle)
    assert "func_80000400, &e, 0x12345678, &d[0]," in text and "&d[2], &e[2], 0x80000404," in text


def test_data_draft_non_rodata_has_no_const(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(1, 2))
    out = tmp_path / "d.c"
    _create(snapshot, "d", out)
    assert out.read_text() == "unsigned int d[] = {\n    0x00000001, 0x00000002,\n};\n"
    assert "const" not in out.read_text()


def test_data_draft_bytes_when_unaligned(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, bytes(range(10)))
    out = tmp_path / "d.c"
    _create(snapshot, "d", out)
    text = out.read_text()
    assert "unsigned char d[] = {" in text
    assert "0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,\n    0x08, 0x09," in text


def test_data_draft_takes_the_element_type_every_reference_agrees_on_and_decodes_it(
    tmp_path: Path, toolchains: Any
) -> None:
    blob = bytes.fromhex("424CCCCC3F6666663FC00000")
    snapshot = _data_snapshot(tmp_path, blob, {"d": "rodata"})
    out = tmp_path / "d.c"
    USES["d"] = (("f", "lwc1", 0), ("g", "lwc1", 8), ("g", "addiu", 0))
    _create(snapshot, "d", out)
    assert out.read_text() == "const float d[] = {\n    51.199997f, 0.9f, 1.5f,\n};\n"
    assert draft.data_code(snapshot, "d", blob) == "f"
    assert draft.data_values(bytes.fromhex("44160000"), "f") == ["600.0f"]
    USES["d"] = (("f", "lwc1", 0), ("g", "lw", 4))  # mixed widths stay words
    _create(snapshot, "d", out)
    assert "const unsigned int d[] = {\n    0x424CCCCC," in out.read_text()
    USES["d"] = (("f", "lhu", 0),)
    _create(snapshot, "d", out)
    assert "const unsigned short d[] = {\n    0x424C, 0xCCCC," not in out.read_text()
    assert "16972, 52428, 16230," in out.read_text()


def test_data_draft_names_the_object_by_its_symbol_not_its_member_path(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(1))
    member = replace(snapshot.layout.members["d"], name="rodata/f/80000600")
    snapshot = replace(
        snapshot, layout=replace(snapshot.layout, members={**snapshot.layout.members, "rodata/f/80000600": member})
    )
    out = tmp_path / "named.c"
    _create(snapshot, "rodata/f/80000600", out)
    assert "unsigned int d[] = {" in out.read_text()  # the symbol at its address
    record = replace(snapshot.versions["a"], symbols={"func_80000400": 0x80000400})
    _create(replace(snapshot, versions={"a": record}), "rodata/f/80000600", out)
    assert "unsigned int D_80000600[] = {" in out.read_text()  # no symbol there: a C name from the address


def test_bss_only_refuses_draft_data(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _data_snapshot(tmp_path, _words(0, 0), {"d": "bss"})
    with pytest.raises(Refusal) as error:
        _create(snapshot, "d", tmp_path / "d.c")
    assert error.value.findings[0].key == "draft.data"


def test_fragment_ending_on_a_jump_refuses_naming_the_slot_holder(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cut = {
        "func_80000400": {"a": (0x1000, _words(0x03E00008)), "b": (0x1000, _words(0x03E00008))},
        "func_80000404": {"a": (0x1004, _words(0x03E00008, 0)), "b": (0x1004, _words(0x03E00008, 0))},
    }
    snapshot = _snapshot(tmp_path, functions=cut)
    calls = _mock_m2c(monkeypatch, b"never")
    assert draft.split_slot(snapshot, "func_80000400") == "func_80000404"
    assert draft.split_slot(snapshot, "func_80000404") is None
    with pytest.raises(Refusal) as caught:
        _create(snapshot, "func_80000400", tmp_path / "out.c")
    (finding,) = caught.value.findings
    assert finding.key == "member.split-delay-slot" and "func_80000404" in finding.reason
    assert not calls and not (tmp_path / "out.c").exists()


def test_whole_function_has_no_split_slot(tmp_path: Path, toolchains: Any) -> None:
    assert draft.split_slot(_snapshot(tmp_path), "func_80000400") is None


def _function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes) -> Snapshot:
    snapshot = _with_asm(tmp_path, "glabel func_80000400\n")
    _mock_m2c(monkeypatch, body)
    monkeypatch.setattr(draft.signatures, "declarations", lambda s, names: {})
    return snapshot


def test_a_draft_file_never_holds_the_m2c_macro_block_but_the_compile_probe_gets_it(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "draft.c"
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n    M2C_ERROR(1);\n}\n")
    _create(snapshot, "func_80000400", out)
    assert "#define" not in out.read_text() and "M2C_UNK" not in out.read_text()
    assert "#define M2C_FIELD(" in draft.with_macros(out.read_text()) and draft.with_macros("int x;\n") == "int x;\n"


def test_a_name_a_project_header_defines_as_a_macro_gets_that_header_included(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n    f(NULL);\n}\n")
    monkeypatch.setattr(draft.headers, "sources", lambda s: ["include/stddef.h"])
    monkeypatch.setattr(draft.headers, "catalog", lambda s: {})
    monkeypatch.setattr(type(snapshot), "read", lambda self, path: b"#define NULL ((void *)0)\n", raising=False)
    out = tmp_path / "draft.c"
    _create(snapshot, "func_80000400", out)
    assert out.read_text().startswith('#include "stddef.h"\n')


def test_the_project_headers_are_read_once_per_snapshot_however_many_drafts_ask(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _base(tmp_path)
    reads: list[str] = []
    monkeypatch.setattr(draft.headers, "sources", lambda s: reads.append("x") or [])
    monkeypatch.setattr(draft.headers, "catalog", lambda s: {"f": ("include/f.h", 1, "")})
    first = draft.declared_names(snapshot)
    assert draft.declared_names(snapshot) is first and len(reads) == 1


def test_draft_drops_the_annotations_m2c_adds_to_its_output(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    noisy = (
        "struct _m2c_stack_f {\n    /* 0x00 */ char pad0[0x30];\n};                      /* size = 0x30 */\n\n"
        "s32 other(s32);                          /* extern */\n"
        "struct _m2c_stack_g {};   /* size 0x0 */\nvoid func_80000400(void) {\n}\n"
    )
    out = tmp_path / "draft.c"
    _create(_function(tmp_path, monkeypatch, noisy.encode()), "func_80000400", out)
    text = out.read_text()
    assert "/* size" not in text and "/* 0x00" not in text and "/* extern" not in text
    assert "_m2c_stack_f" not in text and "s32 other(s32);" in text  # a stack struct nothing uses is dropped


def test_draft_defines_the_function_with_the_return_type_the_project_declares(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n    func_80000400();\n}\n")
    monkeypatch.setattr(
        draft.signatures, "declarations", lambda s, names: {"func_80000400": "int func_80000400(void);"}
    )
    out = tmp_path / "draft.c"
    _create(snapshot, "func_80000400", out)
    assert "int func_80000400(void) {" in out.read_text() and "    func_80000400();" in out.read_text()


def _two_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """m2c answers differently with and without --stack-structs, so the stages are distinct drafts."""

    def run(argv: Any, cwd: Path, config: Any, **kw: Any) -> Any:
        body = "/* stack */\n" * ("--stack-structs" in argv) + "void func_80000400(void) {\n}\n"
        return fixture.native_result(0, body.encode(), b"")

    monkeypatch.setattr(host, "run", run)


def _serial_pool(monkeypatch: pytest.MonkeyPatch, check: Any) -> list[int]:
    dispatched: list[int] = []

    def checked(snapshot: Any, member: str, drafts: list[Any]) -> list[Any]:
        dispatched.append(len(drafts))
        return [check(d) for d in drafts]

    monkeypatch.setattr(draft, "_checked", checked)
    return dispatched


def test_draft_is_the_richest_stage_that_builds_from_one_screen(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n}\n")
    _two_outputs(monkeypatch)
    texts: list[str] = []

    def check(stage: Any) -> Any:
        texts.append(stage["text"])
        return {**stage, "reason": "" if len(texts) == 2 else "compile.error: stage broke"}

    dispatched = _serial_pool(monkeypatch, check)
    out = tmp_path / "draft.c"
    assert _create(snapshot, "func_80000400", out) == ""
    assert dispatched == [len(texts)] and len(set(texts)) == len(texts) and out.read_text() == texts[1]


def test_draft_that_never_compiles_keeps_the_richest_stage_and_names_why(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n}\n")
    _two_outputs(monkeypatch)
    texts: list[str] = []

    def check(stage: Any) -> Any:
        texts.append(stage["text"])
        return {**stage, "reason": f"compile.error: attempt {len(texts)}"}

    _serial_pool(monkeypatch, check)
    out = tmp_path / "draft.c"
    assert _create(snapshot, "func_80000400", out) == "compile.error: attempt 1"
    assert len(texts) > 1 and out.read_text() == texts[0]


def _fake_compiler(monkeypatch: pytest.MonkeyPatch, errors: list[str]) -> list[Any]:
    proofs: list[Any] = []
    pending = iter(errors)

    def screen(config: Any, rows: Any) -> list[Any]:
        out = []
        for row in rows:
            proofs.append(row)
            reason = next(pending)
            missing = (f"version a: compile.error: {reason}",) if reason else ()
            out.append(judge.Verdict((fixture.proof("u", "func_80000400", "a", not missing, missing),), False, ()))
        return out

    monkeypatch.setattr(draft, "_checked", REAL_CHECKED)
    monkeypatch.setattr(draft.judge, "score_many", screen)
    return proofs


def test_checked_reports_an_undeclared_stack_slot_and_adds_no_declaration_for_it(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"")
    proofs = _fake_compiler(monkeypatch, ["func_80000400.c:3: `sp30' undeclared (first use in this function)"])
    text = "void func_80000400(void) {\n    x = sp30[0];\n}\n"
    fixed = draft._checked(snapshot, "func_80000400", [{"text": text, "gbi_rewrites": 0}])[0]
    assert "sp30' undeclared" in fixed["reason"] and len(proofs) == 1 and fixed["text"] == text


def test_checked_arity_mismatch_is_reported_without_a_define(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"")
    proofs = _fake_compiler(monkeypatch, ["func_80000400.c:4: too many arguments to function `func_80000500'"])
    text = "void func_80000400(void) {\n    func_80000500(1);\n}\n"
    result = draft._checked(snapshot, "func_80000400", [{"text": text, "gbi_rewrites": 0}])[0]
    assert "too many arguments" in result["reason"] and len(proofs) == 1
    assert result["text"] == text and "#define func_80000500" not in result["text"]


def test_each_m2c_job_closes_a_stage_of_its_own_so_no_path_is_shared_by_two_slow_jobs(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unbake import effort

    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n}\n")
    _two_outputs(monkeypatch)
    with effort.capture() as records:
        for stack in (True, False):
            draft._function_text((snapshot, "func_80000400", "a", stack, Path("context.c")))
    paths = [r.path for r in records if r.path[-1].startswith("m2c ")]
    assert len(paths) == 2 and len(set(paths)) == 2


def test_jumps_hands_m2c_the_jump_tables_the_layout_holds_with_their_targets_labelled(
    tmp_path: Path, toolchains: Any
) -> None:
    words = (0x80000010).to_bytes(4, "big") + (0x80000020).to_bytes(4, "big")
    snapshot = _snapshot(tmp_path, data={"jtbl_800C0000": {"a": (0x1200, words)}}, versions=("a",))
    asm = tmp_path / "asm/a/f.s"
    asm.parent.mkdir(parents=True)
    asm.write_text(
        "glabel f\n lui $at, %lo(jtbl_800C0000)\n    /* 1 80000010 00000000 */ nop\n  .Lauto_80000020:\n"
        "    /* 2 80000020 00000000 */ nop\n"
    )
    files = draft._jumps(snapshot, "f", "a", asm)
    assert [Path(p).name for p in files] == ["f.s", "tables.s"]
    assert ".Lauto_80000010:\n    /* 1 80000010" in Path(files[0]).read_text()
    assert ".word .Lauto_80000010" in Path(files[1]).read_text()
    (tmp_path / "asm/a/g.s").write_text("glabel g\n")
    assert draft._jumps(snapshot, "g", "a", tmp_path / "asm/a/g.s") == [str(tmp_path / "asm/a/g.s")]


def test_jumps_reads_a_jump_table_inside_a_larger_data_member_up_to_the_next_symbol(
    tmp_path: Path, toolchains: Any
) -> None:
    words = b"".join(w.to_bytes(4, "big") for w in (7, 0x80000010, 0x80000020, 9))
    snapshot = _snapshot(tmp_path, data={"rodata_blob": {"a": (0x1200, words)}}, versions=("a",))
    place = snapshot.layout.members["rodata_blob"].placements[0]
    symbols = {"jtbl_800C0004": place.vram + 4, "after": place.vram + 12}
    record = replace(snapshot.versions["a"], symbols=symbols)
    snapshot = replace(snapshot, versions={**snapshot.versions, "a": record})
    asm = tmp_path / "asm/a/f.s"
    asm.parent.mkdir(parents=True)
    asm.write_text("glabel f\n lui $at, %lo(jtbl_800C0004)\n lui $at, %lo(jtbl_800C9999)\n")
    files = draft._jumps(snapshot, "f", "a", asm)
    tables = Path(files[1]).read_text()
    assert ".word .Lauto_80000010\n  .word .Lauto_80000020\n" in tables and "Lauto_00000007" not in tables
    assert "unresolved jtbl_800C9999: no address" in tables and draft._misaligned(snapshot.config, "f")


def test_the_function_label_is_the_one_the_assembly_defines() -> None:
    assert draft._label("glabel func_8022B1A8\n", "func_80228E60_eu") == "func_8022B1A8"
    assert draft._label("glabel func_80228E60_eu\nglabel x\n", "func_80228E60_eu") == "func_80228E60_eu"
    assert draft._label("", "f") == "f"


def test_draft_drops_a_store_to_a_saved_register_slot_nothing_reads(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"void func_80000400(void) {\n    unksp1C = -1;\n    other = 2;\n    other = 3;\n}\n"
    out = tmp_path / "draft.c"
    _create(_function(tmp_path, monkeypatch, body), "func_80000400", out)
    assert "unksp1C" not in out.read_text() and "other = 3;" in out.read_text()


def test_the_macro_header_defines_null_and_a_large_function_with_an_empty_body_is_no_draft():
    assert "#define NULL" in draft.with_macros("M2C_UNK x;\n")
    entry = Member("f", "function", "main", (Placement("a", ".text", 0, 0x40, 0x80000400),))
    empty = "void f(void) {\n}\n"
    assert "empty body for a 64 byte function" in draft._hollow(entry, "a", "f", empty)
    assert draft._hollow(entry, "a", "f", "void f(void) {\n    g();\n}\n") == ""
    small = Member("f", "function", "main", (Placement("a", ".text", 0, 8, 0x80000400),))
    assert draft._hollow(small, "a", "f", empty) == ""


def test_draft_collapses_a_division_helper_the_project_names_into_one_u64_expression(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        "extern s32 now(void);\nextern s32 func_80000500(s32, s32, s32, s32);\nextern s32 D_80145DA0;\n"
        "extern s32 D_80145DA4;\nextern s32 D_80327C70;\nextern s32 D_80327C74;\n\nvoid func_80000400(void) {\n"
        "    s32 temp_ret;\n    s32 temp_ret_2;\n    u32 temp_v1;\n\n    temp_ret = now();\n"
        "    temp_v1 = (u32) (u64) temp_ret;\n"
        "    temp_ret_2 = func_80000500(temp_ret * 0x3E8 + (u32) (((u64) temp_v1 * 0x3E8) >> 0x20), temp_v1 * 0x3E8, "
        "D_80145DA0, D_80145DA4);\n    D_80327C70 = temp_ret_2;\n    D_80327C74 = (s32) (u32) (u64) temp_ret_2;\n}\n"
    )
    snapshot = _function(tmp_path, monkeypatch, text.encode())
    monkeypatch.setattr(draft.api, "symbols", lambda config, version: {"__udivdi3": (0x80000500, 0, 8, "func")})
    out = tmp_path / "draft.c"
    _create(snapshot, "func_80000400", out)
    assert "D_80327C70 = now() * 1000ULL / D_80145DA0;" in out.read_text()


def _view(members: list[Member]) -> SimpleNamespace:
    return SimpleNamespace(
        layout=SimpleNamespace(members={m.name: m for m in members}),
        versions={"a": SimpleNamespace(id="a"), "b": SimpleNamespace(id="b")},
    )


def _span(name: str, version: str, vram: int, size: int, rom: int = 0) -> Member:
    return Member(name, "data", "main", (Placement(version, ".data", rom, rom + size, vram),))


def test_an_address_past_an_inner_span_still_lands_in_the_outer_span_that_holds_it() -> None:
    outer, inner = _span("outer", "a", 0x80000700, 0x100), _span("inner", "a", 0x80000720, 4)
    view, held = _view([outer, inner]), {}
    assert draft._into(view, held, "a", 0x80000740) == (outer, 0x40)
    assert draft._into(view, held, "a", 0x80000722) == (inner, 2)
    assert draft._into(view, held, "a", 0x80000800) is None and draft._into(view, held, "a", 0x7FFFFFFF) is None


def test_a_word_is_a_pointer_only_where_every_other_version_points_to_the_same_place(monkeypatch) -> None:
    here = Member("d", "data", "main", tuple(Placement(v, ".data", 0, 4, 0x80000600) for v in "ab"))
    target = Member(
        "t",
        "data",
        "main",
        (Placement("a", ".data", 0, 0x40, 0x80000700), Placement("b", ".data", 0, 0x40, 0x80000900)),
    )
    view = _view([here, target])
    words = {"a": (0x80000710).to_bytes(4, "big"), "b": (0x80000910).to_bytes(4, "big")}
    monkeypatch.setattr(draft.world, "rom_bytes", lambda record, start, end: words[record.id])
    held: dict = {}
    images: dict = {}
    hit = draft._into(view, held, "a", 0x80000710)
    assert hit == (target, 0x10) and draft._agrees(view, held, images, here, "a", 0, hit)
    images.clear()
    words["b"] = (0x80000A00).to_bytes(4, "big")  # a float-like number here, an address nowhere near there
    assert not draft._agrees(view, held, images, here, "a", 0, hit)
    images.clear()
    words["b"] = (0x80000914).to_bytes(4, "big")  # the same target at another offset
    assert not draft._agrees(view, held, images, here, "a", 0, hit)


def test_each_other_versions_member_image_is_read_once_however_many_words_there_are(monkeypatch) -> None:
    places = tuple(Placement(v, ".data", 0, 256, 0x80000600) for v in "abc")
    here = Member("d", "data", "main", places)
    target = Member("t", "data", "main", tuple(Placement(v, ".data", 0, 0x40, 0x80000700) for v in "abc"))
    view = SimpleNamespace(
        layout=SimpleNamespace(members={"d": here, "t": target}),
        versions={v: SimpleNamespace(id=v) for v in "abc"},
    )
    reads: list[str] = []
    word = (0x80000710).to_bytes(4, "big") * 64

    def rom_bytes(record, start, end):
        reads.append(record.id)
        return word

    monkeypatch.setattr(draft.world, "rom_bytes", rom_bytes)
    held: dict = {}
    images: dict = {}
    hit = draft._into(view, held, "a", 0x80000710)
    assert all(draft._agrees(view, held, images, here, "a", at, hit) for at in range(0, 256, 4))
    assert sorted(reads) == ["b", "c"]


def test_a_data_word_that_is_a_pointer_in_one_version_only_stays_a_number(tmp_path: Path, toolchains: Any) -> None:
    both, one = 0x80000608, 0x80000608
    data = {
        "d": {"a": (0x1200, _words(both, one)), "b": (0x1200, _words(both, 0x12345678))},
        "e": {"a": (0x1208, b"\0\0\0\0"), "b": (0x1208, b"\0\0\0\0")},
    }
    snapshot = _snapshot(tmp_path, None, data=data, versions=("a", "b"))
    text = draft.data_text(snapshot, "d")
    assert "&e, 0x80000608," in text  # the same word points to e in both versions, the second only in a


def test_jump_table_words_without_the_high_bit_point_at_the_labels_of_the_function() -> None:
    text = "glabel f\n    /* B3410 802B2810 01C84024 */ nop\n    /* B3414 802B2814 00000000 */ nop\n"
    table = "dlabel jtbl_1\n  .word .Lauto_002B2810\n  .word .Lauto_802B2814\n  .word .Lauto_00000099\nenddlabel jtbl_1"
    out = draft._retarget([table], text)[0]
    assert ".word .Lauto_802B2810\n" in out and ".word .Lauto_802B2814\n" in out
    assert ".word .Lauto_00000099\n" in out  # a word that is no label of the function stays


def test_draft_keeps_the_return_type_m2c_gave_a_function_the_project_never_declared(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"s32 func_80000400(void *arg0) {\n    return 1;\n}\n")
    out = tmp_path / "draft.c"
    _create(snapshot, "func_80000400", out)
    assert "s32 func_80000400(void *arg0) {" in out.read_text()


def test_draft_declares_the_stack_names_m2c_used_without_declaring(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n    M2C_UNK sp10;\n    g(sp14);\n}\n")
    out = tmp_path / "draft.c"
    _create(snapshot, "func_80000400", out)
    assert out.read_text().count("sp14;") == 1 and "s32 sp14;" in out.read_text()


def test_a_draft_that_fails_on_a_dereferenced_scalar_is_retyped_until_it_builds(
    tmp_path: Path, toolchains: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _function(tmp_path, monkeypatch, b"void func_80000400(void) {\n    g(*var);\n}\n")
    seen: list[str] = []

    def checked(snapshot: Any, member: str, drafts: list[Any]) -> list[Any]:
        seen.append(drafts[0]["text"])
        bad = "s32 *var" not in drafts[0]["text"]
        return [{**drafts[0], "reason": "x.c:40: invalid type argument of `unary *'" if bad else ""}]

    monkeypatch.setattr(draft, "_checked", checked)
    skew = draft.with_macros("M2C_x").count("\n")
    text = "M2C_UNK z;\ns32 var;\nvoid f(void) {\n    g(*var);\n}\n"
    results = [{"text": text, "reason": f"x.c:{skew + 4}: invalid type argument of `unary *'"}]
    got = draft._dereferenced(snapshot, "func_80000400", "a", results)
    assert got["reason"] == "" and "s32 *var;" in got["text"] and len(seen) == 1
    other = [{"text": "void f(void) {}\n", "reason": "compile.error: other"}]
    assert draft._dereferenced(snapshot, "func_80000400", "a", other) is other[0] and len(seen) == 1


def test_text_like_takes_strings_not_integer_words_or_pointers() -> None:
    assert draft.text_like(b"EXPANSION PAK FOUND\0")
    assert draft.text_like(b"anim object index\0")
    assert draft.text_like(b"one line\0\0\0\0second line\0")
    assert not draft.text_like(_words(0x41, 0x42, 0x4E45494C))  # small integers read 00 00 00 41
    assert not draft.text_like(b"O\0\0\0N\0\0\0NEIL")  # one-character runs between NULs
    assert not draft.text_like(_words(0x80000400, 0x80000600))
    assert not draft.text_like(b"ab\0")  # too short


def test_text_literal_keeps_bytes_with_escapes() -> None:
    assert draft.text_literal(b'say "hi"\\\n\0\0') == '"say \\"hi\\"\\\\\\n"'
    assert draft.text_literal(b"a\x012b\0") == '"a\\001" "2b"'  # a digit after an octal escape starts a new literal


def test_data_draft_types_text_as_char_array(tmp_path: Path, toolchains: Any) -> None:
    blob = b"controller pak\0\0"
    snapshot = _data_snapshot(tmp_path, blob, {"d": "rodata"})
    out = tmp_path / "d.c"
    USES["d"] = (("f", "addiu", 0),)
    _create(snapshot, "d", out)
    assert out.read_text() == 'const char d[16] = "controller pak";\n'
    USES["d"] = (("f", "lw", 0),)  # read as words: stays a word array
    _create(snapshot, "d", out)
    assert "unsigned int d[]" in out.read_text()


def _kuseg(tmp: Path, here: bytes, other: bytes | None = None, moved: bool = False) -> Snapshot:
    """d holds `here` in a; with `other`, b holds another member o with those bytes, and func_80000400 sits 0x10
    later in b when `moved` (its word there follows it)."""
    code = {"func_80000400": {"a": (0x1000, CODE), "b": (0x1010 if moved else 0x1000, CODE)}}
    data = {"d": {"a": (0x1200, here)}, "e": {"a": (0x1300, b"\0\0\0\0"), "b": (0x1300, b"\0\0\0\0")}}
    if other is not None:
        data["o"] = {"b": (0x1400, other)}
    return _snapshot(tmp, None, functions=code, data=data, versions=("a", "b"))


def test_a_kuseg_word_beside_another_pointer_names_its_function(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _kuseg(tmp_path, _words(0x80000700, 0x00000400))
    words = [0x80000700, 0x00000400]
    assert draft.code_alias(snapshot, {}, "a", 0x80000604, words, 1).name == "func_80000400"  # type: ignore[union-attr]


def test_a_lone_kuseg_word_stays_a_number_unless_it_moves_with_its_function(tmp_path: Path, toolchains: Any) -> None:
    lone = _kuseg(tmp_path / "1", _words(0x5, 0x00000400, 0x7))
    assert draft.code_alias(lone, {}, "a", 0x80000604, [0x5, 0x00000400, 0x7], 1) is None
    effort._memo.clear()  # the fakes share one digest: a new snapshot needs a new command
    moved = _kuseg(tmp_path / "2", _words(0x5, 0x00000400, 0x7), _words(0x00000410), moved=True)
    assert draft.code_alias(moved, {}, "a", 0x80000604, [0x5, 0x00000400, 0x7], 1) is not None  # b's data follows it


def test_the_second_word_of_a_display_list_command_is_no_code_pointer(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _kuseg(tmp_path, _words(0xDE000000, 0x00000400, 0x80000700))
    assert draft.code_alias(snapshot, {}, "a", 0x80000604, [0xDE000000, 0x00000400, 0x80000700], 1) is None
    assert draft.code_alias(snapshot, {}, "a", 0x80000600, [0x00000400, 0x80000700], 0) is not None


def test_alias_name_reuses_a_shared_row_and_refuses_a_name_taken_for_another_address(
    tmp_path: Path, toolchains: Any
) -> None:
    snapshot = _kuseg(tmp_path, _words(0x80000700, 0x00000400))
    assert draft.alias_name(snapshot, ("a", "b"), 0x400) == "D_00000400"
    snapshot.versions["a"].symbols["D_00000400"] = 0x404
    assert draft.alias_name(snapshot, ("a", "b"), 0x400) is None
    snapshot.versions["a"].symbols["D_old"] = 0x400
    snapshot.versions["b"].symbols["D_old"] = 0x400
    assert draft.alias_name(snapshot, ("a", "b"), 0x400) == "D_old"


def test_data_draft_spells_a_kuseg_code_word_by_the_alias_row_the_version_has(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _kuseg(tmp_path, _words(0x80000700, 0x00000400))
    assert "0x00000400" in draft.data_text(snapshot, "d")  # no row yet: the number stays
    snapshot.versions["a"].symbols["D_00000400"] = 0x400
    text = draft.data_text(snapshot, "d")
    assert "extern char D_00000400[];" in text and "(unsigned int)D_00000400" in text


PREVIOUS = _words(0x27BDFFE0, 0x03E00008, 0x27BD0020)  # ends at 0x8000040C with a return and its delay slot


def _padded(tmp_path: Path, second: bytes) -> Snapshot:
    code = {"func_80000400": {"a": (0x1000, PREVIOUS)}, "func_8000040C": {"a": (0x100C, second)}}
    return _snapshot(tmp_path, functions=code, versions=("a",))


def test_a_word_before_a_prologue_after_a_return_is_a_leading_pad_and_work_refuses_with_the_split(
    tmp_path: Path, toolchains: Any
) -> None:
    snapshot = _padded(tmp_path / "a", _words(0x000510C0, 0x27BDFFC0, 0x03E00008, 0x27BD0040))
    assert draft.leading_pad(snapshot, "func_8000040C") == [("a", 0x100C, 0x8000040C, 0x000510C0)]
    assert draft.leading_pad(snapshot, "func_80000400") == []
    with pytest.raises(Refusal) as caught:
        draft.refuse_fragment(snapshot, "func_8000040C")
    (finding,) = caught.value.findings
    assert finding.key == "member.leading-pad"
    assert "0x000510C0 at 0x8000040C (rom 0x100C)" in finding.missing[0]
    manifest = snapshot.config.project.root / ".unbake/recipe/work/func_8000040C.pads.json"
    assert json.loads(manifest.read_text()) == [
        {"member": "func_8000040C", "version": "a", "rom": 0x100C, "vram": 0x8000040C}
    ]
    assert finding.action.startswith(f"unbake submit --pad-split {manifest} --patch ")


def test_a_function_that_opens_with_its_prologue_owns_no_pad(tmp_path: Path, toolchains: Any) -> None:
    snapshot = _padded(tmp_path, _words(0x27BDFFC0, 0x27BD0040, 0x03E00008, 0x27BD0040))
    assert draft.leading_pad(snapshot, "func_8000040C") == []
    draft.refuse_fragment(snapshot, "func_8000040C")
