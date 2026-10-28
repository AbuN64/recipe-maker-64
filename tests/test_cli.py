"""The command line: every text result ends in one next line, --json prints one object, refusals exit 1."""

import json
from contextlib import contextmanager

import pytest
from r1_fake_api import fake_api  # noqa: F401
from unbake import api
from unbake.contracts import Finding, Refusal

from recipemaker import cli, world

RESULTS = {
    "structure": {
        "modules": {"before": 3, "after": 6},
        "units_estimate": {"before": 5, "after": 6},
        "boundaries": [{"left": "f3", "right": "f4", "signals": ["padding"]}],
        "moves": 2,
        "patch": "diff --git a/x b/x\n",
        "files": ["x"],
        "path": ".unbake/recipe/patches/patch-1.diff",
        "next": "recipe apply",
    },
    "apply": {"patch": "patch-1.diff", "left": 0, "changed": ["Makefile"], "next": "recipe structure"},
    "name": {
        "action": "next",
        "step": "N2",
        "pack": ".unbake/recipe/name/pack-1.json",
        "targets": 7,
        "moves": 4,
        "path": ".unbake/recipe/patches/patch-2.diff",
        "next": "recipe apply",
    },
    "style": {
        "files": 12,
        "parsed": 3,
        "findings": [{"rule": "function-name", "path": "src/a.c", "detail": "f"}] * 2,
        "next": "recipe name next",
    },
    "look": {
        "name": "T",
        "kind": "type",
        "text": "t",
        "where": "h.h",
        "fields": [{"offset": "0x0", "size": 4, "kind": "i", "name": "a"}],
        "seen_in": ["h.h"],
        "facts": [],
        "next": "recipe next",
    },
}


@pytest.mark.parametrize("verb", sorted(RESULTS))
def test_every_text_result_ends_with_exactly_one_next_line(verb):
    lines = cli.render(verb, RESULTS[verb]).splitlines()
    assert lines[-1] == f"next: {RESULTS[verb]['next']}" and sum(x.startswith("next:") for x in lines) == 1


@pytest.fixture
def wired(fakes, monkeypatch):
    monkeypatch.setattr(api, "load", lambda root, host: fakes.config)
    return fakes


def _style(monkeypatch, fn):
    monkeypatch.setitem(cli._VERBS, "style", fn)


def test_main_prints_the_text_and_returns_zero(wired, monkeypatch, capsys):
    _style(monkeypatch, lambda config, params: {**RESULTS["style"], "findings": []})
    assert cli.main(["--host", "/work/host.toml", "style"]) == 0
    assert capsys.readouterr().out.endswith("12 files, 3 parsed: no findings\nnext: recipe name next\n")


def test_main_json_prints_one_object_with_ok_true(wired, monkeypatch, capsys):
    _style(monkeypatch, lambda config, params: dict(RESULTS["style"]))
    assert cli.main(["--host", "/work/host.toml", "style", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and body["files"] == 12 and isinstance(body["wall_s"], int)


@pytest.mark.parametrize("explicit", [False, True])
def test_reviewed_return_uses_ledger_tree_unless_project_is_explicit(wired, monkeypatch, capsys, explicit):
    roots = []
    monkeypatch.setattr(cli.namebook, "default_tree", lambda: cli.Path("/ledger/tree"))
    monkeypatch.setattr(api, "load", lambda root, host: roots.append(root) or wired.config)
    monkeypatch.setitem(
        cli._VERBS,
        "name",
        lambda config, params: {
            "action": "return",
            "returned": 1,
            "reason": params["reason"],
            "repeated": False,
            "next": "recipe name status",
        },
    )
    project = ["--project", "/explicit/tree"] if explicit else []
    assert (
        cli.main(
            [
                "--host",
                "/work/host.toml",
                *project,
                "name",
                "return",
                "selection.json",
                "--reason",
                "Reviewed meaning",
                "--json",
            ]
        )
        == 0
    )
    assert str(roots[0]) == ("/explicit/tree" if explicit else "/ledger/tree")
    assert json.loads(capsys.readouterr().out)["returned"] == 1


def test_a_refusal_exits_one_and_prints_its_reason_and_one_next_line(wired, monkeypatch, capsys):
    def refuse(config, params):
        raise Refusal(Finding("recipe.budget", "budget spent", action="recipe name next"))

    _style(monkeypatch, refuse)
    assert cli.main(["--host", "/work/host.toml", "style"]) == 1
    assert capsys.readouterr().out == "refused: budget spent\nnext: recipe name next\n"


def test_a_refusal_in_json_carries_the_findings(wired, monkeypatch, capsys):
    _style(monkeypatch, lambda c, p: (_ for _ in ()).throw(Refusal(Finding("recipe.dirty", "x"))))
    assert cli.main(["--host", "/work/host.toml", "style", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["findings"][0]["key"] == "recipe.dirty"


def test_an_unexpected_error_exits_three_and_still_ends_in_a_next_line(wired, monkeypatch, capsys):
    _style(monkeypatch, lambda c, p: 1 / 0)
    assert cli.main(["--host", "/work/host.toml", "style"]) == 3
    assert capsys.readouterr().out.splitlines()[-1] == "next: recipe name next"


def test_no_host_config_is_a_refusal_naming_the_fix(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("UNBAKE_HOST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["style"]) == 1
    out = capsys.readouterr().out
    assert "no host config" in out and "~/.config/unbake/unbake.toml" in out


def test_an_unbake_action_is_no_recipe_verb_and_a_refusal_is_never_sent_back_to_itself(wired, monkeypatch, capsys):
    def refuse(action):
        def go(config, params):
            raise Refusal(Finding("config.missing", "a file is missing", action=action))

        return go

    for action in ("unbake setup", "recipe style"):
        _style(monkeypatch, refuse(action))
        cli.main(["--host", "/work/host.toml", "style"])
        assert capsys.readouterr().out.splitlines()[-1] == "next: recipe name next"


def test_a_missing_assembly_file_names_the_race_and_says_retry(wired, monkeypatch, capsys):
    def gone(config, params):
        raise world.AsmMissing("asm/us/x.s is missing: an extraction replacing asm/ was still running")

    _style(monkeypatch, gone)
    assert cli.main(["--host", "/work/host.toml", "style"]) == 3
    out = capsys.readouterr().out
    assert "retry the same command" in out and out.splitlines()[-1] == "next: recipe style"


def test_structure_prints_one_patch_object_to_stdout_and_the_summary_with_one_next_line_to_stderr(
    wired, monkeypatch, capsys
):
    stream = json.dumps({"patch": RESULTS["structure"]["patch"], "files": ["x"], "moves": 2}) + "\n"
    seen = []

    def structure(config, params):
        seen.append(params)
        return {**RESULTS["structure"], "stream": stream}

    monkeypatch.setitem(cli._VERBS, "structure", structure)
    assert cli.main(["--host", "/work/host.toml", "structure", "--segment", "span_1000"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"patch": "diff --git a/x b/x\n", "files": ["x"], "moves": 2}
    assert err.splitlines()[-1] == "next: recipe apply" and err.count("next:") == 1 and "proposed 2 moves" in err
    assert seen[0]["segment"] == "span_1000" and str(seen[0]["host"]) == "/work/host.toml"


def test_structure_json_prints_one_object_with_the_patch_and_without_the_stream(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "structure", lambda c, p: {**RESULTS["structure"], "stream": "{}\n"})
    assert cli.main(["--host", "/work/host.toml", "structure", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and body["moves"] == 2 and "stream" not in body and body["patch"].startswith("diff")


def test_apply_text_ends_with_one_next_line_and_names_the_patch():
    text = cli.render("apply", RESULTS["apply"])
    assert text.splitlines()[-1] == "next: recipe structure" and text.count("next:") == 1
    assert "applied patch-1.diff, 0 patches left" in text


def test_apply_takes_an_optional_patch_and_gets_the_host(wired, monkeypatch, capsys):
    seen = []
    monkeypatch.setitem(cli._VERBS, "apply", lambda c, p: (seen.append(p), dict(RESULTS["apply"]))[1])
    assert cli.main(["--host", "/work/host.toml", "apply", "x.diff"]) == 0
    assert seen[0]["patch"] == "x.diff" and str(seen[0]["host"]) == "/work/host.toml"


def test_a_move_refusal_renders_its_own_sentence_and_fix(wired, monkeypatch, capsys):
    monkeypatch.setitem(
        cli._VERBS, "apply", lambda c, p: (_ for _ in ()).throw(Refusal(Finding("move.syntax", "bad line 3")))
    )
    assert cli.main(["--host", "/work/host.toml", "apply"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "refused: the move stream is not valid moves" and lines[1] == "because: bad line 3"
    assert lines[2].startswith("fix: fix the stream") and lines[-1] == "next: recipe name next"


def test_the_parser_lists_the_naming_and_style_verbs():
    help_ = cli._parser().format_help()
    assert "name" in help_ and "style" in help_ and set(cli._RENDER) == set(cli._VERBS)


def test_name_submit_and_banners_texts_end_in_one_next_line():
    submit = {"action": "submit", "rows": 5, "accepted": 3, "refused": 2, "by_rule": {"banned": 2}, "moves": 4}
    text = cli.render("name", {**submit, "path": "p.diff", "next": "recipe apply"})
    assert "3 of 5 names accepted as 4 moves in p.diff" in text and "refused 2: banned 2" in text
    assert text.splitlines()[-1] == "next: recipe apply" and text.count("next:") == 1
    banners = cli.render(
        "name", {"action": "banners", "files": ["a.c"], "skipped": 1, "next": "unbake submit --bulk a.c"}
    )
    assert banners.splitlines() == ["1 banner candidates, 1 skipped", "next: unbake submit --bulk a.c"]


def test_name_json_prints_one_object_and_gets_the_host(wired, monkeypatch, capsys):
    seen = []
    monkeypatch.setitem(cli._VERBS, "name", lambda c, p: (seen.append(p), dict(RESULTS["name"]))[1])
    assert cli.main(["--host", "/work/host.toml", "name", "next", "--step", "N2", "--count", "7", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and body["moves"] == 4 and body["next"] == "recipe apply"
    assert seen[0]["step"] == "N2" and seen[0]["count"] == 7 and str(seen[0]["host"]) == "/work/host.toml"


def test_style_takes_paths_as_arguments_or_as_the_paths_option_structure_has(wired, monkeypatch):
    seen = []
    monkeypatch.setitem(cli._VERBS, "style", lambda c, p: seen.append(p["paths"]) or RESULTS["style"])
    assert cli.main(["--host", "/work/host.toml", "style", "src/a.c", "--paths", "src/b.c", "src/c.c"]) == 0
    assert seen == [["src/a.c", "src/b.c", "src/c.c"]]


def test_style_exits_one_when_a_finding_is_an_error_and_zero_otherwise(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "style", lambda c, p: {**RESULTS["style"], "ok": False, "exit": 1})
    assert cli.main(["--host", "/work/host.toml", "style", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and len(body["findings"]) == 2 and "exit" not in body
    monkeypatch.setitem(cli._VERBS, "style", lambda c, p: {**RESULTS["style"], "findings": []})
    assert cli.main(["--host", "/work/host.toml", "style", "src/a.c"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "next: recipe name next"


def test_every_verb_runs_inside_the_lifecycle_guard_so_a_dead_caller_ends_it(wired, monkeypatch):
    seen = []

    @contextmanager
    def bound():
        seen.append("enter")
        yield
        seen.append("exit")

    monkeypatch.setattr(cli.lifecycle, "bound", bound)
    _style(monkeypatch, lambda config, params: seen.append("verb") or dict(RESULTS["style"]))
    assert cli.main(["--host", "/work/host.toml", "style"]) == 0
    assert seen == ["enter", "verb", "exit"]


def test_only_apply_holds_the_tree_lock_for_its_run(wired, monkeypatch, capsys):
    held = []

    @contextmanager
    def lock(config, **kw):
        held.append((kw["exclusive"], kw["who"]))
        yield 0.0

    monkeypatch.setattr(api, "tree_lock", lock)
    _style(monkeypatch, lambda config, params: dict(RESULTS["style"]))
    monkeypatch.setitem(cli._VERBS, "apply", lambda config, params: dict(RESULTS["apply"]))
    cli.main(["--host", "/work/host.toml", "style", "--json"])
    cli.main(["--host", "/work/host.toml", "apply", "--json"])
    assert held == [(True, "apply")]  # the others hold none: their unbake commands take the shared lock one by one


def test_a_ship_parked_notice_is_said_until_its_patch_is_gone(tmp_path):
    patch = tmp_path / "p.patch"
    patch.write_text("diff")
    rows = [
        {"patch": str(patch), "files": ["a.c"]},
        {"patch": str(tmp_path / "gone"), "files": []},
    ]
    (tmp_path / ".unbake").mkdir()
    (tmp_path / ".unbake/ship-parked.json").write_text(json.dumps(rows))
    assert cli._parked(tmp_path) == [f"ship.parked: a.c no longer apply; git apply --3way {patch} then delete it"]
    patch.unlink()
    assert cli._parked(tmp_path) == []


def test_the_slice_pack_and_auto_results_are_said_in_a_line_each():
    slices = {
        "action": "slices",
        "summary": ["98 slices"],
        "path": "/n/slices.json",
        "lane_files": ["/n/slices/a.json"],
    }
    assert cli.render("name", {**slices, "next": "n"}).splitlines() == [
        "98 slices",
        "written /n/slices.json",
        "lane file /n/slices/a.json",
        "next: n",
    ]
    pack = {
        "action": "next",
        "step": "S",
        "slice": ["S010"],
        "pack": "/p/pack.json",
        "answer": "/p/names.jsonl",
        "functions": 20,
        "slots": 70,
        "data": 0,
        "functions_left": 44,
        "next": "recipe name submit --ledger L /p/names.jsonl",
    }
    said = cli.render("name", pack).splitlines()
    assert said[0] == "S S010: pack /p/pack.json holds 20 functions (70 slots) and 0 data, 44 functions left"
    auto = {
        "action": "auto",
        "rows": 3,
        "by_kind": {"sdk": 1, "strings": 2},
        "path": "/a.jsonl",
        "refused": {"strings": {"duplicate": 2}},
        "next": "n",
    }
    assert cli.render("name", auto).splitlines()[:2] == [
        "3 rows (sdk 1, strings 2) in /a.jsonl",
        "judged out: {'strings': 2}",
    ]
