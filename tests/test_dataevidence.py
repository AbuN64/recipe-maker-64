"""Bounded alias and own-string regressions from the four-float BARREL pool shape."""

from types import SimpleNamespace

import pytest
from test_naming import project  # noqa: F401
from test_slicepack import names  # noqa: F401

from recipemaker import dataevidence, namebook, naming, slicepack


def record(name="D_pool", address=0x800C21D8, start=0x100, size=16):
    return {
        "name": name,
        "kind": "data",
        "module": "src/BARREL_3.CPP",
        "placements": [
            {"version": v, "section": ".rodata", "vram": address, "rom_start": start, "rom_end": start + size}
            for v in ("us", "eu")
        ],
    }


def tables(offset=12, width=4):
    return {v: {"D_alias": (0x800C21D8 + offset, 0x100 + offset, width, "data")} for v in ("us", "eu")}


def test_four_float_pool_accepts_final_complete_float():
    assert dataevidence.Storage({"D_pool": record()}, tables()).alias("D_alias") == "D_pool"


@pytest.mark.parametrize("offset,width", [(12, 8), (16, 4), (-1, 1), (4, None), (4, 0)])
def test_unknown_width_and_out_of_range_are_unproved(offset, width):
    assert dataevidence.Storage({"D_pool": record()}, tables(offset, width)).alias("D_alias") == ""


def test_direct_memory_operand_supplies_missing_width_and_overrules_short_table_size():
    members = {"D_pool": record()}
    widths = {v: {"D_alias": 4} for v in ("us", "eu")}
    assert dataevidence.Storage(members, tables(width=None), widths).alias("D_alias") == "D_pool"
    widths["eu"]["D_alias"] = 8
    assert dataevidence.Storage(members, tables(width=1), widths).alias("D_alias") == ""


@pytest.mark.parametrize("defect", ["offset", "owner", "missing", "rom-vram", "kind"])
def test_all_version_relation_must_agree(defect):
    members, rows = {"D_pool": record()}, tables()
    if defect == "offset":
        rows["eu"]["D_alias"] = (0x800C21DC, 0x104, 4, "data")
    elif defect == "owner":
        members["D_other"] = record("D_other", 0x90000000, 0x200)
        members["D_other"]["placements"] = [members["D_other"]["placements"][1]]
        members["D_pool"]["placements"] = [members["D_pool"]["placements"][0]]
        rows["eu"]["D_alias"] = (0x9000000C, 0x20C, 4, "data")
    elif defect == "missing":
        rows["eu"].clear()
    elif defect == "rom-vram":
        rows["eu"]["D_alias"] = (0x9000000C, 0x10C, 4, "data")
    else:
        rows["eu"]["D_alias"] = (0x800C21E4, 0x10C, 4, "func")
    assert dataevidence.Storage(members, rows).alias("D_alias") == ""


def test_overlapping_owner_is_refused_even_when_only_one_contains_full_width():
    members = {"D_pool": record(), "D_other": record("D_other", 0x800C21E4, 0x10C, 1)}
    assert dataevidence.Storage(members, tables()).alias("D_alias") == ""


def test_string_bytes_are_own_bounded_and_common_to_all_versions(tmp_path, monkeypatch):
    members = {"D_pool": record(size=12)}
    versions = {}
    for v in ("us", "eu"):
        rom = tmp_path / f"{v}.z64"
        rom.write_bytes(b"\0" * 0x100 + b"GAME OVER\0\0\0" + b"NEIGHBOUR\0")
        versions[v] = SimpleNamespace(rom=rom)
    ctx = SimpleNamespace(members=members, canon=lambda n: n, storage=dataevidence.Storage(members, {}))
    monkeypatch.setattr(slicepack.api, "snapshot", lambda config: SimpleNamespace(versions=versions))
    strings = slicepack.Strings(None, ctx)
    assert strings.of("D_pool") == ["GAME OVER"]
    assert strings.of("D_neighbour") == []
    versions["eu"].rom.write_bytes(b"\0" * 0x100 + b"DIFFERENT\0\0\0")
    assert slicepack.Strings(None, ctx).of("D_pool") == []
    versions["eu"].rom.write_bytes(b"\0" * 0x100 + b"GAME OVER")
    assert slicepack.Strings(None, ctx).of("D_pool") == []


def test_datum_own_string_needs_no_reader_and_cannot_borrow_another_datums_string(names):  # noqa: F811
    ctx = naming.Map(names, naming.structure.evidence(names), {"Actor_Update": ([], ["D_80002000", "D_80005000"])})
    assert ctx.verifies("string Score", ["D_80005000"])
    assert not ctx.verifies("string Score", ["D_80002000"])
    ctx.callers.clear()
    assert ctx.verifies("string Score", ["D_80005000"])
    assert ctx.callers == {}


def test_explicit_ledger_directory_is_scoped_and_restored(tmp_path):
    before = namebook.store()
    private = tmp_path / "private"
    with namebook.namespace(private):
        assert namebook.store() == private
        with namebook.namespace(None):
            assert namebook.store() == private
    assert namebook.store() == before
    with pytest.raises(ValueError), namebook.namespace(private):
        raise ValueError("refusal")
    assert namebook.store() == before


def test_sdk_names_must_agree_in_every_holding_version(names):  # noqa: F811
    ctx = naming.Map(names, naming.structure.evidence(names), {})
    target = "func_80004010"
    first = ctx.members[target]["placements"][0]
    ctx.members[target]["placements"].append({**first, "version": "eu"})
    ctx._sdk = {("us", first["vram"]): "osYield"}
    assert ctx.sdk_name(target) == ""
    ctx._sdk[("eu", first["vram"])] = "osCreateThread"
    assert ctx.sdk_name(target) == ""
    ctx._sdk[("eu", first["vram"])] = "osYield"
    assert ctx.sdk_name(target) == "osYield"


def test_extracted_operand_records_real_width_not_address_construction(tmp_path, monkeypatch):
    member = {
        "name": "Reader",
        "kind": "function",
        "segment": "main",
        "placements": [{"version": "us", "vram": 0x802170A0}],
    }
    folder = tmp_path / "asm"
    folder.mkdir()
    (folder / "Reader.s").write_text(
        "/* 1 */ lwc1 $f12, %lo(D_alias)($at)\n"
        "/* 2 */ addiu $s0, $s0, %lo(D_unknown)\n"
        "/* 3 */ ldc1 $f14, %lo(D_wide)($at)\n"
    )
    monkeypatch.setattr(naming, "_directory", lambda *args: "asm")
    graph = naming.reads(SimpleNamespace(project=SimpleNamespace(root=tmp_path)), {"members": [member]})
    assert graph.widths == {"us": {"D_alias": 4, "D_wide": 8}}


def test_version_specific_pool_uses_only_its_version_of_the_reader():
    pool = record()
    pool["placements"] = pool["placements"][:1]
    other = record("D_other")
    other["placements"] = other["placements"][1:]
    reader = {"kind": "function", "placements": [{"version": v} for v in ("us", "eu")]}
    members = {"D_pool": pool, "D_other": other, "Reader": reader}
    storage = dataevidence.Storage(members, tables())
    assert storage.alias("D_alias") == ""  # no context-free identity is invented
    assert storage.loaded({("us", "Reader"): ["D_alias"], ("eu", "Reader"): ["D_alias"]}) == {
        "Reader": ["D_other", "D_pool"]
    }
    members["D_pool"]["placements"].extend(other["placements"])
    # Pool now overlaps another owner in EU: US evidence cannot certify the whole pool.
    assert dataevidence.Storage(members, tables()).loaded({("us", "Reader"): ["D_alias"]}) == {}
