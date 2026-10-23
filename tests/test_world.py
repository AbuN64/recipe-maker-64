"""world: the members, modules and units Recipe Maker reads from unbake's captured tree."""

from pathlib import Path
from types import SimpleNamespace

from recipemaker import world


def module(path, owns, fuzzy=(), stand_ins=None, sections=None, segment="main"):
    return SimpleNamespace(
        path=path,
        segment=segment,
        owns=owns,
        fuzzy=tuple(fuzzy),
        stand_ins=stand_ins or {},
        sections=sections or {},
    )


def snapshot(tmp_path: Path, modules, symbols, files=None):
    for name, text in (files or {}).items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(text)
    project = SimpleNamespace(root=tmp_path, names_from="a", libraries=("src/lib/",), toolchain="gcc-test")
    tree = SimpleNamespace(modules=tuple(modules), symbols=symbols, digest="t")
    peek = lambda p: (tmp_path / p).read_bytes() if (tmp_path / p).is_file() else None  # noqa: E731
    return SimpleNamespace(files=tree, config=SimpleNamespace(project=project), digest="s", peek=peek)


SYMBOLS = {
    "a": {
        "f": (0x80000400, 0x1000, 8, "func"),
        "g": (0x80000408, 0x1008, 8, "func"),
        "d": (0x80001000, 0x2000, 4, "u32"),
    },
    "b": {"f": (0x80000500, 0x1100, 8, "func")},
}


def test_members_carry_placements_per_version_kind_and_whether_they_landed(tmp_path):
    files = {"src/m.c": b'#include "m.h"\n', "src/lib/l.c": b""}
    mods = [
        module(
            "src/m.c", {"a": ("f", "g", "d"), "b": ("f",)}, stand_ins={"a": ("g",)}, sections={"a": {"d": ".rodata"}}
        ),
        module("src/lib/l.c", {}),
    ]
    layout = world._build(snapshot(tmp_path, mods, SYMBOLS, files))
    f, g, d = (layout.members[n] for n in ("f", "g", "d"))
    assert f.kind == "function" and f.landed and f.holders() == ("a", "b") and f.reference("b") == "b"
    assert f.placements[0] == world.Placement("a", ".text", 0x1000, 0x1008, 0x80000400)
    assert not g.landed and d.kind == "data" and d.placements[0].section == ".rodata"
    group = layout.groups["src/m.c"]
    assert group.members == ("f", "g", "d") and group.header == "include/m.h" and not group.sdk
    assert set(layout.units) == {"src/m.c"} and "src/lib/l.c" not in layout.groups  # a module that owns nothing


def test_a_fuzzy_body_is_not_landed_and_names_its_module(tmp_path):
    mods = [module("src/m.c", {"a": ("f",), "b": ("f",)}, fuzzy=("f",))]
    layout = world._build(snapshot(tmp_path, mods, SYMBOLS))
    assert layout.fuzzy == {"f": "src/m.c"} and not layout.members["f"].landed and layout.units == {}


def test_a_bss_placement_has_no_rom_bytes_and_keeps_its_size(tmp_path):
    symbols = {"a": {"b": (0x80002000, 0x3000, 0x40, "u8")}}
    mods = [module("src/m.c", {"a": ("b",)}, sections={"a": {"b": ".bss"}})]
    place = world._build(snapshot(tmp_path, mods, symbols)).members["b"].placements[0]
    assert (place.rom_start, place.rom_end, place.size) == (0x3000, 0x3000, 0x40)


def test_rom_bytes_reads_a_slice_of_the_versions_image(tmp_path):
    rom = tmp_path / "rom.z64"
    rom.write_bytes(bytes(range(16)))
    version = SimpleNamespace(rom=rom, rom_sha256="x")
    assert world.rom_bytes(version, 4, 8) == bytes([4, 5, 6, 7])


def test_the_work_list_holds_what_no_file_defines_largest_first(tmp_path):
    mods = [module("src/m.c", {"a": ("f", "g", "d"), "b": ("f",)}, stand_ins={"a": ("g", "d")})]
    snap = snapshot(tmp_path, mods, SYMBOLS)
    snap.layout = world._build(snap)
    rows = world.open_items(snap)
    assert [(r["member"], r["kind"], r["size"]) for r in rows] == [("g", "function", 8), ("d", "data", 4)]
    assert rows[0] == {"member": "g", "kind": "function", "segment": "main", "size": 8, "address": 0x80000408}


def test_a_symbol_without_a_size_spans_to_the_next_symbol(tmp_path):
    symbols = {"a": {"f": (0x80000400, 0x1000, None, "func"), "g": (0x80000420, 0x1020, 8, "func")}}
    mods = [module("src/m.c", {"a": ("f", "g")})]
    assert world._build(snapshot(tmp_path, mods, symbols)).members["f"].placements[0].size == 0x20


def test_an_empty_write_and_a_deletion_are_different_overlays(tmp_path):
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class Snap:
        digest: str = "s"
        overlays: dict = field(default_factory=dict)
        files: object = None
        reads: dict = field(default_factory=dict)

    empty, gone = world.overlay(Snap(), {"include/a.h": b""}), world.overlay(Snap(), {"include/a.h": None})
    assert empty.digest != gone.digest and empty.overlays == {"include/a.h": b""}


def test_asm_path_reads_the_fact_files_once_for_any_number_of_members_and_again_when_they_change(tmp_path, monkeypatch):
    facts = {"f": (0x80000400, 0x1100, 8, "func"), "d": (0x80001000, 0x2100, 4, "u32")}
    reads = []

    def symbols(config, version):
        reads.append(version)
        return dict(facts)

    monkeypatch.setattr(world.api, "symbols", symbols)
    (tmp_path / "versions/a").mkdir(parents=True)
    table = tmp_path / "versions/a/symbol_addrs.txt"
    table.write_text("f = 0x80000400;\n")
    segments = [SimpleNamespace(rom=0x1000, name="s1"), SimpleNamespace(rom=0x2000, name="s2")]
    project = SimpleNamespace(root=tmp_path, version={"a": SimpleNamespace(segments=segments)})
    config = SimpleNamespace(project=project)
    assert world.asm_path(config, "a", "f") == tmp_path / "asm/a/nonmatchings/s1/f.s"
    assert world.asm_path(config, "a", "d") == tmp_path / "asm/a/data/s2/d.s"
    assert [world.asm_path(config, "a", "f") for _ in range(50)] and reads == ["a"]
    table.write_text("f = 0x80000400; // changed\n")
    world.asm_path(config, "a", "f")
    assert reads == ["a", "a"]


def test_a_symbol_no_module_owns_is_no_extent_boundary():
    table = {
        "D": (0x2000, 0x2000, None, "data"),
        "alias": (0x2002, 0x2002, None, "data"),
        "E": (0x2010, 0x2010, 4, "data"),
    }
    assert world.extents(table) == {"D": 2, "alias": 14}
    assert world.extents(table, {"D", "E"}) == {"D": 16}
