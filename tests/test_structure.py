"""Module structure: the proposal from a map document, the move stream and the batches sent to `unbake move -`."""

import json
from collections import Counter
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
from fakes import native_result
from unbake.contracts import Finding, Refusal

from recipemaker import clash, state, structure, tree

DATA = Path(__file__).parent / "data" / "map.json"
SETTINGS = state.Structure()
OPS = ["symbol", "member", "module", "source", "header", "file"]
MOVE = {
    "type": "object",
    "required": ["op", "subject", "target"],
    "additionalProperties": False,
    "properties": {
        "op": {"enum": OPS},
        "subject": {"type": "string", "minLength": 1},
        "target": {"type": "string", "minLength": 1},
    },
}


@pytest.fixture(autouse=True)
def calm(monkeypatch):
    """No sleeping, and a clean git tree unless a test says otherwise."""
    slept = []
    monkeypatch.setattr(structure.time, "sleep", slept.append)
    monkeypatch.setattr(structure, "_dirty", lambda root: set())
    return slept


@pytest.fixture
def doc():
    return json.loads(DATA.read_text(encoding="utf-8"))


def names(proposal):
    return {m["name"]: m["atoms"] for m in proposal["modules"]}


def everything(proposal):
    return [atom for atoms in names(proposal).values() for atom in atoms]


def test_a_cut_needs_two_signals_and_hard_cuts_end_runs(doc):
    got = structure.propose(doc, SETTINGS)
    assert names(got) == {
        "code_80001000": ["f1", "f2", "f3"],  # the call chain and the shared load hold f1..f3 together
        "code_800010C0": ["f4", "f5", "f6"],  # the padded edge f5|f6 has only the padding signal, so it is joined
        "code_80001180": ["f7", "f8"],  # toolchain change before it; f8 is held by one version and joins by a call
        "code_80001200": ["f9"],  # an sdk edge
        "code_80002000": ["g1", "g2", "g3", "g4", "g5"],  # another segment, one cluster
    }
    cuts = [(b["left"], b["right"]) for b in got["boundaries"]]
    assert cuts == [("f3", "f4")] and "padding" in got["boundaries"][0]["signals"]
    assert len(got["boundaries"][0]["signals"]) >= 2


def test_the_authored_module_stays_unless_its_segment_is_asked_for(doc):
    assert "fa" not in everything(structure.propose(doc, SETTINGS))
    got = structure.propose(doc, SETTINGS, "span_1000")
    assert names(got)["code_80001240"] == ["fa"] and "g1" not in everything(got)
    assert got["scope"] == ["span_1000"]


def test_a_long_module_is_cut_at_its_weakest_join(doc):
    got = structure.propose(doc, state.Structure(split_over=4), "span_2000")
    assert names(got) == {"code_80002000": ["g1", "g2", "g3"], "code_800020C0": ["g4", "g5"]}
    assert [(b["left"], b["right"], b["signals"][-1]) for b in got["boundaries"]] == [("g3", "g4", "chain")]


def _own_unit(doc, member, **change):
    """Give one member a unit of its own, as a lone BT function with its own options or compiler."""
    old = next(u for u in doc["units"] if member in u["members"])
    old["members"].remove(member)
    doc["units"].append({**old, "path": f"src/{member}.c", "members": [member], **change})
    next(m for m in doc["members"] if m["name"] == member)["unit"] = f"src/{member}.c"


def _one_unit(doc, *members):
    """Put functions into the unit of the first, as a decompiled C file that holds several of them."""
    path = next(m["unit"] for m in doc["members"] if m["name"] == members[0])
    for u in doc["units"]:
        u["members"] = [n for n in u["members"] if n not in members[1:]]
    doc["units"] = [u for u in doc["units"] if u["members"]]
    next(u for u in doc["units"] if u["path"] == path)["members"] = list(members)
    for m in doc["members"]:
        m["unit"] = path if m["name"] in members else m["unit"]


def test_a_cut_never_falls_inside_one_c_unit(doc):
    # BT src/func_800E3460.c: func_800E3460 and func_800E34B4 cut apart were refused as move.not_exact
    _one_unit(doc, "f3", "f4")
    got = structure.propose(doc, SETTINGS)
    assert [m for m in names(got).values() if {"f3", "f4"} <= set(m)]
    assert ("f3", "f4") not in [(b["left"], b["right"]) for b in got["boundaries"]]


def test_asm_between_two_functions_of_one_c_unit_stays_in_its_module(doc):
    _one_unit(doc, "f2", "f4")
    next(m for m in doc["members"] if m["name"] == "f3")["unit"] = None  # asm f3 joins f2, left of the f3|f4 cut
    assert [m for m in names(structure.propose(doc, SETTINGS)).values() if {"f2", "f3", "f4"} <= set(m)]


def test_a_long_module_is_cut_at_its_weakest_join_outside_a_c_unit(doc):
    _one_unit(doc, "g3", "g4")
    got = names(structure.propose(doc, state.Structure(split_over=4), "span_2000"))
    assert len(got) == 2 and [m for m in got.values() if {"g3", "g4"} <= set(m)]


def test_units_with_other_compile_options_never_share_a_module(doc):
    # BT func_800B1520: one unit carries -UF3DEX_GBI_2 -G8 that its neighbours lack
    _own_unit(doc, "f2", options={"add": ["-UF3DEX_GBI_2", "-G8"], "omit": []})
    got = names(structure.propose(doc, SETTINGS))
    assert ["f2"] in got.values() and ["f1"] in got.values()


def test_units_of_other_toolchains_never_share_a_module(doc):
    # BT code_80076000: gcc-2.7.2-kmc, ido-7.1 and gcc-2.8.1-sn64 in one group
    _own_unit(doc, "f2", toolchain="ido-7.1")
    got = names(structure.propose(doc, SETTINGS))
    assert ["f2"] in got.values() and ["f1"] in got.values()


def test_an_asm_only_function_joins_the_nearest_c_function_instead_of_its_own_module(doc):
    base = structure.propose(doc, SETTINGS)
    for member in doc["members"]:
        if member["name"] == "f9":
            member["unit"] = None
    got = structure.propose(doc, SETTINGS)
    assert names(got)["code_80001180"] == ["f7", "f8", "f9"] and "code_80001200" not in names(got)
    assert got["estimate"]["modules"][1] == base["estimate"]["modules"][1] - 1
    assert got["estimate"]["headers"][1] <= base["estimate"]["headers"][1]


def test_asm_between_two_c_functions_joins_a_c_neighbour_and_never_crosses_options(doc):
    for name in ("f2", "f3"):
        next(m for m in doc["members"] if m["name"] == name)["unit"] = None
    got = names(structure.propose(doc, SETTINGS))
    assert {"f1", "f2", "f3"} <= set(got["code_80001000"])
    _own_unit(doc, "f4", options={"add": ["-G8"], "omit": []})
    assert not [m for m in names(structure.propose(doc, SETTINGS)).values() if {"f1", "f4"} <= set(m)]


def test_headers_used_by_the_same_modules_merge_into_one_header_or_the_modules_own(doc):
    doc["units"].append({**doc["units"][0], "path": "src/uA.c", "members": ["f1"]})
    doc["units"].append({**doc["units"][0], "path": "src/uB.c", "members": ["g1"]})
    own, both = "include/span_1000/code_f1.h", "include/span_2000/code_g1.h"
    row = {"modules": [], "declares": [], "includes": []}
    doc["headers"] += [
        {**row, "path": "include/solo.h", "declares": ["S"], "included_by": ["src/uA.c"]},
        {**row, "path": "include/via.h", "declares": ["V"], "included_by": ["include/solo.h"]},
        {**row, "path": "include/two.h", "declares": ["X"], "included_by": ["src/uA.c", "src/uB.c"]},
        {**row, "path": "include/two2.h", "declares": ["Y"], "included_by": ["src/uB.c", "src/uA.c"]},
    ]
    home = {"f1": own, "g1": both}
    got = {m["subject"]: m["target"] for m in structure.headers(doc, 0.8, home)}
    assert got["include/solo.h"] == own and got["include/via.h"] == own
    assert got["include/two2.h"] == "include/types.h" and got["include/two.h"] == "include/types.h"  # two subsystems


def test_a_shared_header_reaching_a_game_header_never_moves_into_an_sdk_header(doc):
    doc["units"].append({**doc["units"][0], "path": "src/lib.c", "members": ["f1"]})
    row = {"modules": [], "declares": [], "includes": []}
    game = "include/span_1000/code_f9.h"
    doc["headers"] += [
        {**row, "path": game, "modules": ["m"], "included_by": []},
        {**row, "path": "include/ok.h", "declares": ["K"], "included_by": ["src/lib.c"]},
        {**row, "path": "include/mid.h", "declares": ["M"], "includes": [game], "included_by": ["src/lib.c"]},
        {
            **row,
            "path": "include/top.h",
            "declares": ["T"],
            "includes": ["include/mid.h"],
            "included_by": ["src/lib.c"],
        },
    ]
    got = {m["subject"]: m["target"] for m in structure.headers(doc, 0.8, {"f1": "include/sdk/libgultra.h"})}
    assert got["include/ok.h"] == "include/sdk/libgultra.h"
    assert "include/mid.h" not in got and "include/top.h" not in got


def test_the_game_header_walk_visits_each_header_once_on_a_diamond_of_headers(doc):
    class Counted(list):
        reads = 0

        def __iter__(self):
            Counted.reads += 1
            return super().__iter__()

    doc["units"].append({**doc["units"][0], "path": "src/lib.c", "members": ["f1"]})
    game, row = "include/span_1000/code_f9.h", {"modules": [], "declares": []}
    names = [f"include/d{i}{side}.h" for i in range(10) for side in "ab"]
    doc["headers"].append({**row, "path": game, "modules": ["m"], "includes": [], "included_by": []})
    for n, path in enumerate(names):
        below = names[n // 2 * 2 + 2 : n // 2 * 2 + 4] or [game]
        by = ["src/lib.c"] if n < 2 else names[n // 2 * 2 - 2 : n // 2 * 2]
        doc["headers"].append({**row, "path": path, "includes": Counted(below), "included_by": by})
    structure.headers(doc, 0.8, {"f1": "include/sdk/libgultra.h"})
    assert Counted.reads <= 3 * len(names)  # without the memo this is 2**10 walks


def _load(doc, name, start, end):
    place = [{"version": v, "section": ".rodata", "rom_start": start, "rom_end": end, "vram": 0} for v in ("us", "eu")]
    doc["members"].append(
        {
            "name": name,
            "kind": "data",
            "state": "c",
            "module": "code_old1",
            "unit": None,
            "fuzzy": None,
            "sdk": False,
            "padded": False,
            "placements": place,
            "calls": [],
            "loads": [],
            "strings": [],
        }
    )


def _loads(doc, f4, f5):
    for member in doc["members"]:
        member["loads"] = {"f4": [f4], "f5": [f5], "f6": [f5]}.get(member["name"], member["loads"])


def test_a_data_adjacency_break_splits_a_module_and_adjacent_data_keeps_it(doc):
    _load(doc, "D6", 0x9000, 0x9010)
    _load(doc, "D7", 0x9100, 0x9110)
    _loads(doc, "D6", "D7")  # f4|f5 has only the loads signal; the data of the two sides is apart
    assert names(structure.propose(doc, SETTINGS))["code_800010C0"] == ["f4"]
    assert names(structure.propose(doc, SETTINGS))["code_80001100"] == ["f5", "f6"]
    for place in doc["members"][-1]["placements"]:
        place.update(rom_start=0x9010, rom_end=0x9020)  # D7 touches D6
    assert names(structure.propose(doc, SETTINGS))["code_800010C0"] == ["f4", "f5", "f6"]


def test_headers_give_the_identical_merge_the_jaccard_merge_and_the_base_header(doc):
    moves = structure.headers(doc, 0.8)
    assert Counter(m["op"] for m in moves) == {"file": 5}
    assert {(m["subject"], m["target"]) for m in moves} == {
        ("include/a.h", "include/types.h"),
        ("include/b.h", "include/types.h"),
        ("include/c.h", "include/types.h"),
        ("include/d.h", "include/types.h"),
        ("include/e.h", "include/types.h"),
    }
    assert structure.headers(doc, 0.4)[0]["target"] == "include/types.h"


def test_the_base_headers_move_into_no_other_and_record_headers_merge_into_the_types_header(doc):
    users = ["src/u1.c", "src/u2.c", "src/u3.c", "src/u4.c", "src/u5.c"]
    doc["headers"] += [
        {"path": "include/types.h", "modules": [], "declares": ["f1", "s32"], "includes": [], "included_by": users},
        {"path": "include/game/common.h", "modules": [], "declares": ["g1"], "includes": [], "included_by": users},
    ]
    moves = {m["subject"]: m["target"] for m in structure.headers(doc, 0.8)}
    assert not {"include/types.h", "include/game/common.h"} & set(moves)
    assert moves["include/c.h"] == "include/types.h"  # never chained on into game/common.h


def test_module_headers_never_merge(doc):
    assert not [m for m in structure.headers(doc, 0.0) if "code_old" in m["subject"]]


def test_moves_are_changes_only_leaf_segment_first_and_valid(doc):
    got = structure.propose(doc, SETTINGS)
    flat = [m for _, group in structure._batches(got) for m in group]
    assert Counter(m["op"] for m in flat) == {"member": 9, "source": 5, "header": 5, "module": 1, "file": 5}
    for move in flat:
        jsonschema.validate(move, MOVE)
    schema = resources.files("unbake.resources").joinpath("schemas/move.schema.json")
    if schema.is_file():
        jsonschema.validate(flat, json.loads(schema.read_text(encoding="utf-8")))
    segments = [s for s, _ in structure._batches(got)]
    assert segments == [
        "src/game/main/sys_80001000",
        "src/game/ovl_80002000/sys_80002000",
        "headers",
    ]  # the overlay calls into main, so main is the leaf
    assert {"op": "module", "subject": "code_old2", "target": "code_80002000"} in flat
    assert {"op": "source", "subject": "code_80001000", "target": "src/game/main/sys_80001000/code_80001000.c"} in flat
    assert {"op": "header", "subject": "code_80001000", "target": "include/game/main/sys_80001000.h"} in flat
    assert {"op": "member", "subject": "f1", "target": "code_80001000"} in flat


def test_a_module_that_already_has_the_proposed_name_and_paths_needs_no_move(doc):
    for module in doc["modules"]:
        if module["name"] == "code_old2":
            module.update(name="code_80002000", source="src/game/ovl_80002000/sys_80002000/code_80002000.c")
            module["header"] = "include/game/ovl_80002000/sys_80002000.h"
    for member in doc["members"]:
        member["module"] = "code_80002000" if member["module"] == "code_old2" else member["module"]
    flat = [m for _, group in structure._batches(structure.propose(doc, SETTINGS, "span_2000")) for m in group]
    assert Counter(m["op"] for m in flat) == {"file": 5}


class Unbake:
    """The unbake command line at the process boundary: `map` writes the fixture, `move -` answers from a script."""

    def __init__(self, root, answers):
        self.root, self.answers, self.calls, self.argv = root, list(answers), [], []

    def __call__(self, name, argv, cwd, **kwargs):
        verb = argv[argv.index("--project") + 2]
        stdin = kwargs.get("stdin", b"").decode()
        self.argv.append(argv)
        self.calls.append((verb, [json.loads(x) for x in stdin.splitlines()]))
        if verb == "map":
            (self.root / ".unbake").mkdir(parents=True, exist_ok=True)
            (self.root / ".unbake" / "map.json").write_bytes(DATA.read_bytes())
            return native_result(
                stdout=json.dumps({"ok": True, "result": {"path": ".unbake/map.json"}, "findings": []}).encode()
            )
        document, git = self.answers.pop(0)
        return native_result(stdout=json.dumps(document).encode(), stderr=git.encode())

    def moves(self):
        return [c for c in self.calls if c[0] == "move"]


def answer(refused=(), units=3, ok=True):
    result = {
        "moves": 0,
        "applied": {},
        "units": {"before": 5, "after": units},
        "headers": {"before": 7, "after": 6},
        "files": ["Makefile"],
        "refused": [{"index": i, "move": {}, "findings": [FINDING]} for i in refused],
        "message": "move: done",
    }
    return {
        "ok": ok and not refused,
        "result": result,
        "findings": [],
    }, "git add -A -- Makefile\ngit commit -m 'move: done'\n"


FINDING = {
    "key": "move.not_exact",
    "reason": "unit differs",
    "path": "",
    "line": 0,
    "unit": "",
    "versions": [],
    "missing": [],
    "origin": None,
    "blocking": True,
    "action": "",
    "symptoms": {},
}


def project(tmp_path, monkeypatch, answers):
    root = tmp_path / "project"
    root.mkdir()
    fake = Unbake(root, answers)
    monkeypatch.setattr(structure.process, "run", fake)
    config = SimpleNamespace(project=SimpleNamespace(root=root))
    result = structure.run(config, {"host": None, "segment": None})
    return config, fake, result, root / ".unbake" / "recipe" / "structure"


def test_structure_writes_the_stream_the_proposal_and_a_zero_cursor(tmp_path, monkeypatch):
    _, fake, result, saved = project(tmp_path, monkeypatch, [])
    assert [c[0] for c in fake.calls] == ["map"]
    assert result["moves"] == 25 and len(result["stream"].splitlines()) == 25
    assert (saved / "moves.jsonl").read_text(encoding="utf-8") == result["stream"]
    assert (saved / "cursor.json").read_text(encoding="utf-8") == "0"
    assert json.loads((saved / "proposal.json").read_text(encoding="utf-8"))["runs"] == [
        {"segment": "src/game/main/sys_80001000", "count": 17},
        {"segment": "src/game/ovl_80002000/sys_80002000", "count": 3},
        {"segment": "headers", "count": 5},
    ]
    assert result["modules"] == {"before": 3, "after": 6} and result["next"] == "recipe apply"


def test_apply_records_the_moves_unbake_refused_and_never_sends_the_rest_again(tmp_path, monkeypatch):
    config, fake, result, saved = project(tmp_path, monkeypatch, [answer(refused=(3, 7)), answer()])
    stream = [json.loads(x) for x in result["stream"].splitlines()]
    got = structure.apply(config, {"host": None})
    (first,) = [c[1] for c in fake.moves()]  # partial: unbake proved each move and applied the rest itself
    assert first == stream[:17]
    held = [json.loads(x) for x in (saved / "refused.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [h["move"] for h in held] == [stream[3], stream[7]] and held[0]["findings"][0]["key"] == "move.not_exact"
    assert (saved / "cursor.json").read_text(encoding="utf-8") == "17"
    assert (got["moves"], got["applied"], got["remaining"], got["batch"]) == (17, 15, 8, "src/game/main/sys_80001000")
    assert got["first_refused"] == stream[3]["subject"]
    assert [r["key"] for r in got["refused"]] == ["move.not_exact"] * 2 and got["files"] == ["Makefile"]
    assert got["next"] == "git add -A -- Makefile && git commit -m 'move: done' && recipe apply"


def test_apply_takes_the_next_segment_batch_and_ends_with_a_new_structure(tmp_path, monkeypatch):
    config, fake, _, _saved = project(tmp_path, monkeypatch, [answer(), answer(), answer()])
    batches = [structure.apply(config, {"host": None}) for _ in range(3)]
    assert [(b["batch"], b["moves"]) for b in batches] == [
        ("src/game/main/sys_80001000", 17),
        ("src/game/ovl_80002000/sys_80002000", 3),
        ("headers", 5),
    ]
    assert [len(c[1]) for c in fake.moves()] == [17, 3, 5] and batches[-1]["next"].endswith("recipe structure")
    again = structure.apply(config, {"host": None})
    assert (again["moves"], again["remaining"], len(fake.moves())) == (0, 0, 3) and again["next"] == "recipe structure"


def test_batch_moves_bounds_a_batch(tmp_path, monkeypatch):
    path = tmp_path / "recipe.toml"
    path.write_text(
        "[budget]\nstall_tries = 4\nidle_minutes = 15\npermute_seconds = 120\nskip_days = 7\n"
        "[structure]\nbatch_moves = 5\n"
    )
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    config, fake, _, _saved = project(tmp_path, monkeypatch, [answer()])
    got = structure.apply(config, {"host": None})
    assert (got["moves"], len(fake.moves()[0][1]), got["remaining"]) == (5, 5, 20)


def test_a_lock_that_stays_busy_waits_5_15_45_s_then_leaves_the_cursor(tmp_path, monkeypatch, calm):
    busy = {
        "ok": False,
        "result": None,
        "findings": [{**FINDING, "key": "setup.busy", "reason": "a landing holds the lock"}],
    }
    config, fake, _, saved = project(tmp_path, monkeypatch, [(busy, "")] * 4)
    with pytest.raises(Refusal) as caught:
        structure.apply(config, {"host": None})
    assert caught.value.findings[0] == Finding("setup.busy", "a landing holds the lock")
    assert calm == [5, 15, 45]
    assert len(fake.moves()) == 4 and (saved / "cursor.json").read_text(encoding="utf-8") == "0"
    assert (saved / "refused.jsonl").read_text(encoding="utf-8") == ""


def test_a_batch_where_every_move_is_refused_still_advances(tmp_path, monkeypatch):
    config, fake, _, saved = project(tmp_path, monkeypatch, [answer(refused=range(17))])
    got = structure.apply(config, {"host": None})
    assert (got["applied"], len(got["refused"]), len(fake.moves())) == (0, 17, 1)
    assert (saved / "cursor.json").read_text(encoding="utf-8") == "17" and got["next"] == "recipe apply"


def test_apply_without_a_proposal_says_to_run_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(structure.process, "run", Unbake(tmp_path, []))
    with pytest.raises(Refusal) as caught:
        structure.apply(SimpleNamespace(project=SimpleNamespace(root=tmp_path)), {"host": None})
    assert caught.value.findings[0].action == "recipe structure"


def test_a_failing_map_is_refused_with_unbakes_findings(tmp_path, monkeypatch):
    bad = {"ok": False, "result": None, "findings": [{**FINDING, "key": "config.schema", "reason": "schema 3"}]}
    monkeypatch.setattr(structure.process, "run", lambda *a, **k: native_result(stdout=json.dumps(bad).encode()))
    with pytest.raises(Refusal) as caught:
        structure.run(SimpleNamespace(project=SimpleNamespace(root=tmp_path)), {"host": None, "segment": None})
    assert caught.value.findings[0].key == "config.schema"


def _plain(doc):
    """Every function without a relation to another: no call, no load, no alignment padding."""
    for m in doc["members"]:
        m.update(calls=[], loads=[], padded=False)


def test_an_absent_relation_is_no_cut_signal(doc):
    _plain(doc)
    got = names(structure.propose(doc, SETTINGS))
    assert got["code_80001000"] == ["f1", "f2", "f3", "f4", "f5", "f6"]  # nothing relates them, so nothing parts them
    assert not structure.propose(doc, SETTINGS)["boundaries"]


def test_a_present_relation_that_does_not_join_two_atoms_still_cuts(doc):
    _plain(doc)
    calls, loads = {"f1": ["f2"], "f4": ["f5"]}, {"f3": ["D1"], "f4": ["D2"]}
    for m in doc["members"]:
        m.update(calls=calls.get(m["name"], []), loads=loads.get(m["name"], []), padded=True)
    got = names(structure.propose(doc, SETTINGS))
    assert got["code_80001000"] == ["f1", "f2", "f3"] and got["code_800010C0"] == ["f4", "f5", "f6"]


def _data(doc, name, start, size, unit="src/data.c", section=".data"):
    place = [
        {"version": v, "section": section, "rom_start": start, "rom_end": start + size, "vram": start}
        for v in ("us", "eu")
    ]
    doc["members"].append(
        {
            "name": name,
            "kind": "data",
            "state": "c",
            "module": "code_old1",
            "unit": unit,
            "fuzzy": None,
            "sdk": False,
            "padded": False,
            "placements": place,
            "calls": [],
            "loads": [],
            "strings": [],
        }
    )


def _data_unit(doc, members, path="src/data.c", **change):
    doc["units"].append(
        {
            "path": path,
            "kind": "data",
            "module": "code_old1",
            "members": members,
            "toolchain": "gcc-a",
            "options": {"add": [], "omit": []},
            "withheld": [],
            **change,
        }
    )


def test_data_units_get_modules_of_their_own_and_padding_joins_the_unit_before(doc):
    _data(doc, "d1", 0xA000, 16)
    _data(doc, "d2", 0xA010, 16)
    _data(doc, "x_padding_1", 0xA020, 4, unit="src/pad.c")
    _data_unit(doc, ["d1", "d2"])
    _data_unit(doc, ["x_padding_1"], path="src/pad.c", toolchain="other")  # a run edge of its own
    got = structure.propose(doc, SETTINGS)
    data = {n: a for n, a in names(got).items() if n.startswith("data_")}
    assert list(data.values()) == [["d1", "d2", "x_padding_1"]]
    assert got["estimate"]["padding_units"] == (1, 0)
    assert "d1" not in names(got).get("code_80001000", [])


def test_a_padding_unit_with_nothing_before_it_stays(doc):
    _data(doc, "x_padding_1", 0xA020, 4, unit="src/pad.c")
    _data_unit(doc, ["x_padding_1"], path="src/pad.c")
    assert structure.propose(doc, SETTINGS)["estimate"]["padding_units"] == (1, 1)


def test_the_scope_is_the_segments_the_modules_sit_in_not_the_maps_list(doc):
    doc["segments"] = []
    got = structure.propose(doc, SETTINGS)
    assert set(got["scope"]) == {"span_1000", "span_2000"}
    assert structure.propose(doc, SETTINGS, "span_1000,span_2000")["scope"] == ["span_1000", "span_2000"]


def test_module_headers_are_estimated_from_co_usage_not_one_per_module(doc):
    quiet = structure.propose(doc, SETTINGS)
    _plain(doc)
    alone = structure.propose(doc, SETTINGS)
    assert alone["estimate"]["headers"][1] <= quiet["estimate"]["headers"][1]
    assert alone["estimate"]["headers"][1] < len(alone["modules"]) + len(doc["headers"])
    assert alone["estimate"]["headers"][1] == len([h for h in doc["headers"] if not h["modules"]]) - len(
        alone["merges"]
    )


def test_one_run_parses_the_map_once_and_writes_one_proposal(tmp_path, monkeypatch):
    writes = []
    real = structure.store.write
    monkeypatch.setattr(structure.store, "write", lambda path, data: (writes.append(path.name), real(path, data))[1])
    root = tmp_path / "project"
    root.mkdir()
    fake = Unbake(root, [])
    monkeypatch.setattr(structure.process, "run", fake)
    config = SimpleNamespace(project=SimpleNamespace(root=root))
    reads = []
    real_loads = structure.json.loads
    monkeypatch.setattr(
        structure.json, "loads", lambda text, *a, **k: (reads.append(len(text)), real_loads(text, *a, **k))[1]
    )
    structure.run(config, {"host": None, "segment": "span_1000,span_2000"})
    assert [c[0] for c in fake.calls] == ["map"]
    assert writes.count("proposal.json") == 1
    assert len([n for n in reads if n > 1000]) == 1  # the map document is parsed once


def _clusters(doc, subsystems, shared):
    """Units uN of subsystem N (member mN, home include/game/<folder>/<sub>.h) and shared headers including them."""
    row = {"modules": [], "includes": []}
    doc["headers"] = [h for h in doc["headers"] if h["modules"]]
    doc["units"] = [{**doc["units"][0], "path": f"src/{s}.c", "members": [f"m_{s}"]} for s in subsystems]
    doc["headers"] += [
        {**row, "path": f"include/{n}.h", "declares": [n.upper()], "included_by": [f"src/{s}.c" for s in users]}
        for n, users in shared.items()
    ]
    return {f"m_{s}": f"include/game/{s}.h" for s in subsystems}


def test_two_subsystem_pairs_with_disjoint_shared_declarations_get_two_headers_not_game_common(doc):
    subs = ["main/a", "main/b", "ovl/c", "ovl/d", "ovl/e", "main/f", "main/g", "ovl/h"]
    home = _clusters(doc, subs, {"left": ["main/a", "main/b"], "right": ["ovl/c", "ovl/d"]})
    got = {m["subject"]: m["target"] for m in structure.headers(doc, 0.8, home)}
    assert got["include/left.h"] == "include/game/main/common.h"
    assert got["include/right.h"] == "include/game/ovl/common.h"
    assert "include/game/common.h" not in got.values()  # 2 of 8 subsystems use each: under the shared threshold
    assert len({got["include/left.h"], got["include/right.h"]}) == 2


def test_a_cluster_inside_one_folder_shares_that_folders_header_and_a_wide_one_the_base(doc):
    subs = ["main/a", "main/b", "main/c", "ovl/d", "ovl/e", "ovl/f"]
    home = _clusters(doc, subs, {"x": ["main/a", "main/b"], "wide": subs[:4], "all": subs})
    got = {m["subject"]: m["target"] for m in structure.headers(doc, 0.99, home)}
    assert got["include/x.h"] == "include/game/main/common.h"
    assert got["include/wide.h"] == "include/types.h"  # 4 of 6 subsystems use it
    assert got["include/all.h"] == "include/types.h"


def test_the_shared_threshold_comes_from_recipe_knowledge(doc, monkeypatch):
    subs = ["main/a", "main/b", "ovl/c", "ovl/d"]
    home = _clusters(doc, subs, {"pair": ["main/a", "ovl/c"]})
    assert structure.headers(doc, 0.8, home)[0]["target"] == "include/types.h"  # 2 of 4 is 0.5: base
    monkeypatch.setattr(structure, "table", lambda name: {"shared_threshold": 0.75})
    assert (
        structure.headers(doc, 0.8, home)[0]["target"] == "include/types.h"
    )  # a record pair: the base, not game/common


def test_a_flat_subsystem_cluster_takes_the_base_split_not_game_common_for_everything(doc):
    subs = ["a", "b", "c", "d"]  # homes include/game/<sub>.h: the lowest common folder is include/game
    home = _clusters(doc, subs, {"calls": ["a", "b"], "record": ["c", "d"]})
    doc["members"].append({**doc["members"][0], "name": "fn_x", "kind": "function"})
    next(h for h in doc["headers"] if h["path"] == "include/calls.h")["declares"] = ["fn_x"]
    got = {m["subject"]: m["target"] for m in structure.headers(doc, 0.99, home)}
    assert got["include/calls.h"] == "include/game/common.h"  # declares a function: the declarations header
    assert got["include/record.h"] == "include/types.h"  # declares only a record: the types header


def _move(op, subject, target):
    return {"op": op, "subject": subject, "target": target}


def test_moves_that_follow_a_refused_rename_or_a_module_left_without_members_are_dead():
    stream = [
        _move("symbol", "func_a", "osYield"),
        _move("member", "osYield", "yield"),
        _move("source", "yield", "src/yield.c"),
        _move("header", "yield", "include/y.h"),
        _move("member", "func_b", "kept"),
        _move("source", "kept", "src/kept.c"),
        _move("source", "old", "src/old.c"),
    ]
    dead = structure._dead(stream, {"old": {}}, [stream[0]])
    assert dead == [stream[1], stream[2], stream[3]]
    assert structure._dead(stream, {}, [stream[4]]) == [stream[5]]  # a refused member leaves its module empty


def test_a_source_or_header_waits_with_the_deferred_members_of_its_module():
    members = [_move("member", "f1", "m"), _move("member", "f2", "m")]
    source, header = _move("source", "m", "src/m.c"), _move("header", "m", "include/m.h")
    other = _move("source", "n", "src/n.c")
    batch, deferred = structure._follow([members[1], source, header, other], [members[0]])
    assert batch == [members[1], other] and deferred == [members[0], source, header]


def _spell(**by_unit):
    """`types(unit)` as the clash reader gives it, from the C text each unit defines."""
    return lambda unit: clash.definitions(by_unit.get(unit.removeprefix("src/").removesuffix(".c"), ""))


def _paths(got, name):
    entry = next(m for m in got["modules"] if name in m["atoms"])
    return entry["source"].rsplit("/", 1)[0], entry["header"], entry["name"]


def test_units_that_define_one_type_differently_stay_separate_files_of_one_subsystem(doc):
    types = _spell(code_old1="struct S {int a;}; void f(struct S *);", f2="struct S {char b[4];}; void g(struct S *);")
    got = structure.propose(doc, SETTINGS, types=types)
    assert names(got)["code_80001000"] == ["f1"] and ["f2", "f3"] in names(got).values()
    assert _paths(got, "f1")[:2] == _paths(got, "f2")[:2] == _paths(got, "f3")[:2]
    assert _paths(got, "f1")[2] != _paths(got, "f2")[2]


def test_units_that_define_one_type_the_same_way_or_only_differ_in_prototypes_stay_one_module(doc):
    same = "struct S {int a;};"
    got = structure.propose(doc, SETTINGS, types=_spell(code_old1=same, f2=same + " /* x */", f3="void f(int);"))
    assert names(got)["code_80001000"] == ["f1", "f2", "f3"]
    got = structure.propose(doc, SETTINGS, types=_spell(code_old1="void f(int);", f2="void f(char *);"))
    assert names(got)["code_80001000"] == ["f1", "f2", "f3"]


def test_a_type_clash_never_raises_a_folder_over_the_cap(doc):
    types = _spell(code_old1="struct S {int a;};", f2="struct S {int b;};", f3="struct S {int c;};")
    got = structure.propose(doc, state.Structure(dir_max=3), types=types)
    assert got["gates"]["dir_max_ok"] and got["tree"]["dir_max"] <= 3
    assert len({_paths(got, f)[0] for f in ("f1", "f2", "f3")}) == 1


def _files(*pairs):
    return [{"op": "file", "subject": a, "target": b} for a, b in pairs]


def test_a_header_that_is_a_file_target_is_never_a_chain_link(doc):
    target = {"include/a.h": "include/b.h", "include/b.h": "include/c.h", "include/c.h": "include/c.h"}
    assert structure._final(target, "include/b.h") == "include/c.h"
    assert structure._final({"x": "y", "y": "x"}, "y") == "x"  # a cycle stops
    moves = structure.headers(doc, 0.8)
    assert not {m["target"] for m in moves} & {m["subject"] for m in moves}


def test_apply_all_is_one_partial_move_of_the_whole_stream_and_records_each_refusal(tmp_path, monkeypatch):
    config, fake, result, saved = project(tmp_path, monkeypatch, [answer(refused=(3, 20))])
    got = structure.apply(config, {"host": None, "all": True})
    stream = [json.loads(x) for x in result["stream"].splitlines()]
    moves = [a for a in fake.argv if "move" in a]
    assert len(moves) == 1 and moves[0][-2:] == ["--partial", "-"] and fake.moves()[0][1] == stream
    held = [json.loads(x) for x in (saved / "refused.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [h["move"] for h in held] == [stream[3], stream[20]] and len(got["unbake_calls"]) == 1
    assert (got["batch"], got["moves"], got["applied"], got["remaining"]) == ("all", 25, 23, 0)
    assert (saved / "cursor.json").read_text(encoding="utf-8") == "25" and got["next"].endswith("recipe structure")


def test_apply_all_never_resends_what_unbake_applied_after_a_refusal(tmp_path, monkeypatch):
    config, fake, _, _ = project(tmp_path, monkeypatch, [answer(refused=range(25))])
    got = structure.apply(config, {"host": None, "all": True})
    assert len(fake.moves()) == 1 and (got["applied"], len(got["refused"]), got["remaining"]) == (0, 25, 0)
    again = structure.apply(config, {"host": None, "all": True})
    assert len(fake.moves()) == 1 and (again["moves"], again["remaining"]) == (0, 0)


def test_apply_without_all_proves_each_move_and_never_passes_unproved(tmp_path, monkeypatch):
    config, fake, _, _ = project(tmp_path, monkeypatch, [answer()])
    structure.apply(config, {"host": None})
    assert not [a for a in fake.argv if "--unproved" in a] and [a for a in fake.argv if "--partial" in a]


def _edges(doc, classes, default="unal"):
    """The map's per-version edge class of each function (`unbake map` schema 2): `classes` per name, a class or a
    {version: class} dict; every other function's edge is `default`, a version's first function a segment start."""
    for m in doc["members"]:
        if m["kind"] == "function":
            held = [p["version"] for p in m["placements"] if p["section"] == ".text"]
            given = classes.get(m["name"], default)
            m["edge"] = {v: given.get(v, default) if isinstance(given, dict) else given for v in held}


def test_a_map_without_edge_classes_has_no_verdicts(doc):
    assert structure.verdicts(doc) is None


def test_verdicts_merge_the_versions_where_the_left_function_precedes_the_right(doc):
    _edges(doc, {"f2": {"us": "pad", "eu": "unal"}, "f9": {"us": "unal", "eu": "pad"}, "f4": "al0"})
    got = structure.verdicts(doc)
    assert got["f1", "f2"] == "contradiction" and got["f3", "f4"] == "open"
    assert got["f8", "f9"] == "join" and got["f7", "f9"] == "cut"  # f8 is us only: eu pairs f7 with f9


def test_proven_cuts_always_cut_proven_joins_never_and_no_chain_is_cut(doc):
    _edges(doc, {"f4": "pad", "f6": "pad", "f7": "gap"})
    got = structure.propose(doc, state.Structure(split_over=2))
    assert names(got) == {
        "code_80001000": ["f1", "f2", "f3"],
        "code_800010C0": ["f4", "f5"],
        "code_80001140": ["f6"],  # the padded edge f5|f6 is a proven cut, whatever the signals say
        "code_80001180": ["f7", "f8"],
        "code_80001200": ["f9"],
        "code_80002000": ["g1", "g2", "g3", "g4", "g5"],  # unal edges throughout: one object, never chained apart
    }
    assert [(b["left"], b["right"], b["signals"]) for b in got["boundaries"]] == [
        ("f3", "f4", ["cut"]),
        ("f5", "f6", ["cut"]),
    ]


def test_a_proven_join_holds_where_the_signals_would_cut(doc):
    _plain(doc)
    calls, loads = {"f1": ["f2"], "f4": ["f5"]}, {"f3": ["D1"], "f4": ["D2"]}
    for m in doc["members"]:
        m.update(calls=calls.get(m["name"], []), loads=loads.get(m["name"], []), padded=True)
    _edges(doc, {})
    got = names(structure.propose(doc, SETTINGS))
    assert got["code_80001000"] == ["f1", "f2", "f3", "f4", "f5", "f6"]
    _edges(doc, {}, default="al0")  # open everywhere: the signals decide, but padding is no signal any more
    got = structure.propose(doc, SETTINGS)
    assert names(got)["code_80001000"] == ["f1", "f2", "f3", "f4", "f5", "f6"] and not got["boundaries"]
    _edges(doc, {"f4": "al0"})  # the old padded+loads cut f3|f4 needs a second live signal on an open pair
    doc["members"][[m["name"] for m in doc["members"]].index("f4")]["calls"] = ["f5", "f1"]
    assert "f4" in names(structure.propose(doc, SETTINGS))["code_80001000"]


def test_a_single_near_caller_joins_an_open_pair():
    run = [{"name": n, "calls": c} for n, c in (("a", ["c"]), ("b", []), ("c", []), ("d", []), ("e", []))]
    assert structure._near_callers(run) == {0, 1}  # c's only caller a sits two atoms back: a, b and c are one object
    run[3]["calls"] = ["c"]
    assert structure._near_callers(run) == set()  # two callers: no locality join


def test_an_evidence_row_makes_its_object_one_module_in_the_file_it_names(doc):
    _edges(doc, {"f2": "pad", "f3": "al0", "f4": "pad"})  # f2|f3 is open by the ROM alone
    row = {"first": "f2", "last": "f3", "source": "GAME/ACTORS/PLAYER.CPP", "origin": "debug stream"}
    got = structure.propose(doc, SETTINGS, evidence=[row])
    module = next(m for m in got["modules"] if "f2" in m["atoms"])
    assert module["atoms"] == ["f2", "f3"]  # the row joins what it holds, and cuts at f1|f2 and f3|f4
    assert (module["source"], module["header"]) == ("src/game/game/actors/player.c", "include/game/game/actors.h")
    moves = [m for _, batch in structure._batches(got) for m in batch]
    assert {"op": "source", "subject": module["name"], "target": "src/game/game/actors/player.c"} in moves


def test_an_evidence_path_names_a_folder_and_a_file():
    assert structure.evidenced_path("GAME/WORLD/CAMERA.CPP") == ("game/world", "src/game/game/world/camera.c")
    assert structure.evidenced_path("MAIN.C") == ("", "src/game/main.c")


UPPER_83 = tree.Naming(
    case="upper",
    stem_max=8,
    source_ext=".CPP",
    stem="{vram}",
    subsystem="{label}/{vram}",
    shared_dir="src/DATA/{label}",
    data_dir="src/DATA/{label}",
    evidence="keep",
    subsystem_source="src/{subsystem}",
    subsystem_header="include/{subsystem}.H",
)


def test_one_proposal_under_two_naming_styles(doc):
    plain = structure.propose(doc, SETTINGS)
    styled = structure.propose(doc, SETTINGS, naming=UPPER_83)
    assert names(plain) == names(styled)  # the style names files, never cuts objects
    module = next(m for m in styled["modules"] if m["name"] == "code_80001000")
    assert (module["source"], module["header"]) == ("src/MAIN/80001000/80001000.CPP", "include/MAIN/80001000.H")
    module = next(m for m in plain["modules"] if m["name"] == "code_80001000")
    assert module["source"] == "src/game/main/sys_80001000/code_80001000.c"


def test_evidence_casing_survives_when_the_style_keeps_it(doc):
    _edges(doc, {"f2": "pad", "f3": "al0", "f4": "pad"})
    row = {"first": "f2", "last": "f3", "source": "GAME/ACTORS/PLAYER.CPP", "origin": "debug stream"}
    got = structure.propose(doc, SETTINGS, evidence=[row], naming=UPPER_83)
    module = next(m for m in got["modules"] if "f2" in m["atoms"])
    assert (module["source"], module["header"]) == ("src/GAME/ACTORS/PLAYER.CPP", "include/GAME/ACTORS.H")
    lower = tree.Naming(**{**vars(UPPER_83), "evidence": "lower"})
    assert structure.evidenced_path("GAME/ACTORS/PLAYER.CPP", lower) == ("game/actors", "src/game/actors/player.CPP")


def test_a_stem_over_the_styles_limit_is_refused():
    with pytest.raises(ValueError, match="stem_max"):
        tree.Naming(stem_max=8).file("src/game/x", {"name": "code_80001000", "vram": 0x80001000})


def test_the_project_config_chooses_the_style(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[layout]\nsubsystem_source = "src/{subsystem}"\n[naming]\ncase = "upper"\nstem_max = 8\n'
    )
    got = tree.style(tmp_path)
    assert (got.case, got.stem_max, got.subsystem_source, got.source_ext) == ("upper", 8, "src/{subsystem}", ".c")
    assert tree.style(tmp_path / "missing") == tree.Naming()
    (tmp_path / "config.toml").write_text("[naming]\nstem_limit = 8\n")
    with pytest.raises(ValueError, match="stem_limit"):
        tree.style(tmp_path)


def test_tied_origin_votes_choose_the_same_module_by_name(doc):
    next(m for m in doc["modules"] if m["name"] == "code_old2")["segment"] = "span_1000"
    _plain(doc)
    _edges(doc, {})
    members = {m["name"]: m for m in doc["members"]}
    for name in ("f1", "f2", "f3"):
        members[name]["module"] = "code_old2"
    for name in ("f4", "f5", "f6"):
        members[name]["module"] = "code_old1"
    proposal = structure.propose(doc, SETTINGS)
    module = next(m for m in proposal["modules"] if "f1" in m["atoms"])
    assert module["origin"] == "code_old1"
