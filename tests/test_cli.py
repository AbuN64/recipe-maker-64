"""The command line: every text result ends in one next line, --json prints one object, refusals exit 1."""

import json
from contextlib import contextmanager

import pytest
from r1_fake_api import fake_api  # noqa: F401
from unbake import api
from unbake.contracts import Finding, Refusal

from recipemaker import cli, state, work, world

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
        "next": "recipe status",
    },
    "next": {
        "item": "func_a",
        "kind": "function",
        "size": 212,
        "regions": 12,
        "why": "a twin exists",
        "twin": "func_b in src/b.c (de)",
        "aims": [],
        "idle_minutes": 15,
        "stall_tries": 4,
        "next": "recipe work func_a",
    },
    "work": {
        "item": "func_a",
        "result": "fuzzy",
        "score": {"eu": 0.968},
        "landed": "",
        "alike": [],
        "saved": "c0ffee",
        "refused": "",
        "seed": "",
        "auto_fixes": ["fixed"],
        "candidate": "/work/project/c.c",
        "brief": "/work/project/b.md",
        "twin": "func_b in src/b.c (de)",
        "rungs": [
            {"name": "twin", "result": "none", "score": 0, "note": ""},
            {"name": "permute", "result": "better", "score": 0.968, "note": "120 s"},
        ],
        "budget": {"stall_tries": 4, "stalled": 1, "minutes_left": 14.0},
        "wall_s": 171,
        "next": "recipe try func_a",
    },
    "try": {
        "item": "func_a",
        "result": "failed",
        "compiled": False,
        "stalled": 2,
        "regions": {"matched": 9, "total": 12, "gained": 2},
        "stall_tries": 4,
        "minutes_left": 9.0,
        "score": {"eu": 0.0},
        "landed": "",
        "alike": [],
        "saved": "",
        "auto_fixes": [],
        "file": "/work/project/c.c",
        "why": ["built is 16 bytes longer than the target", "size 64 != 48"],
        "compiler": [],
        "brief": "/work/project/b.md",
        "error": {"class": "call-arity", "line": 41, "what": "w", "fix": "f"},
        "next": "edit line 41",
    },
    "skip": {"item": "func_a", "reason": "budget", "next": "recipe next"},
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
    "status": {
        "head": "abc",
        "open": 3,
        "twin_ready": 1,
        "debt": [{"name": "gX", "blocks": 2, "spellings": 3}],
        "sessions": [],
        "skip_reasons": {"budget": 1},
        "error_classes": {},
        "today": {"exact": 1, "fuzzy": 0, "skipped": 0, "tries": 2},
        "next": "recipe next",
    },
}


@pytest.mark.parametrize("verb", sorted(RESULTS))
def test_every_text_result_ends_with_exactly_one_next_line(verb):
    lines = cli.render(verb, RESULTS[verb]).splitlines()
    assert lines[-1] == f"next: {RESULTS[verb]['next']}" and sum(x.startswith("next:") for x in lines) == 1


def test_a_failed_try_says_it_does_not_compile_with_the_budget_left():
    assert "result: does not compile (2 of 4 tries without progress, 9 idle minutes left)" in cli.render(
        "try", RESULTS["try"]
    )


def test_try_text_says_why_the_score_is_not_exact():
    text = cli.render("try", RESULTS["try"])
    assert "why: built is 16 bytes longer than the target\nwhy: size 64 != 48\nfile:" in text
    assert "regions matching 9 of 12 (+2)\n" in text


def test_a_try_that_does_not_compile_prints_the_compilers_own_message():
    got = cli.render("try", {**RESULTS["try"], "compiler": ["src/f.c:15: called object is not a function"]})
    assert "compiler: src/f.c:15: called object is not a function" in got


def test_next_text_says_which_twin_it_has_or_none():
    assert "twin: func_b in src/b.c (de)" in cli.render("next", RESULTS["next"])
    assert "twin: none" in cli.render("next", {**RESULTS["next"], "twin": "none"})


def test_work_text_is_the_landing_result_score_file_brief_budget_and_wall_time_without_the_rungs():
    assert cli.render("work", RESULTS["work"]).splitlines() == [
        "saved as nonmatching eu 96.8% (c0ffee)",
        "result: fuzzy",
        "score: eu 96.8%",
        "twin: func_b in src/b.c (de)",
        "auto-fixes: fixed",
        "file: /work/project/c.c",
        "brief: /work/project/b.md",
        "budget: 3 tries without progress left, 14 idle minutes left",
        "wall: 171 s",
        "next: recipe try func_a",
    ]


def test_work_text_counts_the_placeholders_left_by_class_most_first():
    classes = {"expression base": 1, "untyped parameter, no caller evidence": 3}
    lines = cli.render("work", {**RESULTS["work"], "unresolved_classes": classes}).splitlines()
    assert "placeholders left: 3 untyped parameter, no caller evidence; 1 expression base" in lines


def test_try_says_landed_only_for_an_exact_landing_and_saved_as_nonmatching_for_a_fuzzy_one():
    exact = cli.render("try", {**RESULTS["try"], "result": "exact", "compiled": True, "landed": "abc"})
    fuzzy = cli.render("try", {**RESULTS["try"], "result": "fuzzy", "compiled": True, "saved": "def"})
    assert "landed (abc)" in exact and "saved" not in exact
    assert "saved as nonmatching eu 0.0% (def)" in fuzzy and "landed" not in fuzzy


@pytest.fixture
def wired(fakes, monkeypatch):
    monkeypatch.setattr(api, "load", lambda root, host: fakes.config)
    return fakes


def test_main_prints_the_text_and_returns_zero(wired, monkeypatch, capsys):
    monkeypatch.setitem(
        cli._VERBS,
        "skip",
        lambda config, params: {"item": params["item"], "reason": params["reason"], "next": "recipe next"},
    )
    assert cli.main(["--host", "/work/host.toml", "skip", "func_a", "--reason", "budget"]) == 0
    assert capsys.readouterr().out == "skipped func_a as budget\nnext: recipe next\n"


def test_main_json_prints_one_object_with_ok_true(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "status", lambda config, params: dict(RESULTS["status"]))
    assert cli.main(["--host", "/work/host.toml", "status", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is True and body["open"] == 3
    assert isinstance(body["wall_s"], int)


def test_a_refusal_exits_one_and_prints_its_reason_and_one_next_line(wired, monkeypatch, capsys):
    def refuse(config, params):
        raise Refusal(Finding("recipe.budget", "budget spent", action="recipe next"))

    monkeypatch.setitem(cli._VERBS, "try", refuse)
    assert cli.main(["--host", "/work/host.toml", "try", "func_a"]) == 1
    assert capsys.readouterr().out == "refused: budget spent\nnext: recipe next\n"


def test_a_refusal_in_json_carries_the_findings(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "try", lambda c, p: (_ for _ in ()).throw(Refusal(Finding("recipe.dirty", "x"))))
    assert cli.main(["--host", "/work/host.toml", "try", "func_a", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["findings"][0]["key"] == "recipe.dirty"


def test_an_unexpected_error_exits_three_and_still_ends_in_a_next_line(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "status", lambda c, p: 1 / 0)
    assert cli.main(["--host", "/work/host.toml", "status"]) == 3
    assert capsys.readouterr().out.splitlines()[-1] == "next: recipe next"


def test_no_host_config_is_a_refusal_naming_the_fix(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("UNBAKE_HOST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "no host config" in out and "~/.config/unbake/unbake.toml" in out


def test_skip_refuses_an_unlisted_reason_with_the_list():
    with pytest.raises(Refusal) as refused:
        state.skip(object(), "func_a", "tired", "", 0.0, 0.0)  # type: ignore[arg-type]
    (finding,) = refused.value.findings
    assert "valid: m2c-failed" in finding.reason and finding.action.startswith("recipe skip func_a --reason other")


def _refused_next(monkeypatch, capsys, action, verb="try", *more):
    def refuse(config, params):
        raise Refusal(Finding("config.missing", "a file is missing", action=action))

    monkeypatch.setitem(cli._VERBS, verb, refuse)
    cli.main(["--host", "/work/host.toml", verb, "func_a", *more])
    return capsys.readouterr().out.splitlines()[-1]


def test_an_unbake_action_becomes_the_recipe_verb_that_does_it(wired, monkeypatch, capsys):
    assert _refused_next(monkeypatch, capsys, "unbake setup") == "next: recipe skip func_a --reason other"
    assert _refused_next(monkeypatch, capsys, "unbake compare src/a.c --function func_a", "work") == (
        "next: recipe try func_a"
    )


def test_a_refusal_never_sends_the_agent_back_to_the_command_it_refused(wired, monkeypatch, capsys):
    skip = ("skip", "--reason", "other")
    assert _refused_next(monkeypatch, capsys, "unbake setup", *skip) == "next: recipe status"
    assert _refused_next(monkeypatch, capsys, "unbake compare src/a.c --function func_a") == "next: recipe status"


def test_the_landing_is_the_first_line_of_work_and_try():
    for verb in ("work", "try"):
        done = {**RESULTS[verb], "result": "exact", "compiled": True, "landed": "abc", "saved": ""}
        assert cli.render(verb, done).splitlines()[0] == "landed (abc)"


def test_a_failure_after_a_landing_still_says_landed_first_and_ends_in_a_concrete_next(wired, monkeypatch, capsys):
    def late(config, params):
        raise work.AfterLanding("abc1234", "", KeyError("brief"))

    monkeypatch.setitem(cli._VERBS, "try", late)
    assert cli.main(["--host", "/work/host.toml", "try", "func_a"]) == 3
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "landed (abc1234)" and "KeyError" in "\n".join(lines) and lines[-1] == "next: recipe next"


def test_an_internal_error_before_any_landing_ends_in_a_retry_never_a_skip(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "work", lambda config, params: 1 / 0)
    assert cli.main(["--host", "/work/host.toml", "work", "func_a"]) == 3
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == "next: recipe status" and "skip" not in last  # a refusal is never sent back to itself


def test_a_missing_assembly_file_names_the_race_and_says_retry(wired, monkeypatch, capsys):
    def gone(config, params):
        raise world.AsmMissing("asm/us/x.s is missing: an extraction replacing asm/ was still running")

    monkeypatch.setitem(cli._VERBS, "work", gone)
    assert cli.main(["--host", "/work/host.toml", "work", "func_a"]) == 3
    out = capsys.readouterr().out
    assert "retry the same command" in out and out.splitlines()[-1] == "next: recipe work func_a"


def test_read_asm_waits_for_a_file_an_extraction_is_putting_back(tmp_path, monkeypatch):
    path = tmp_path / "x.s"
    naps = []

    def nap(seconds):
        naps.append(seconds)
        path.write_text("nop\n")  # the fresh asm/ appears while the reader waits

    monkeypatch.setattr(world, "ASM_WAIT", 5.0)
    monkeypatch.setattr(world.time, "sleep", nap)
    assert world.read_asm(path) == "nop\n" and len(naps) == 1


def test_read_asm_gives_up_with_the_race_named(tmp_path, monkeypatch):
    monkeypatch.setattr(world, "ASM_WAIT", 0.0)
    with pytest.raises(world.AsmMissing, match="extraction"):
        world.read_asm(tmp_path / "absent.s")


def test_status_names_the_top_declaration_conflicts():
    assert "debt: gX blocks 2 items, 3 spellings" in cli.render("status", RESULTS["status"]).splitlines()


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
    assert lines[2].startswith("fix: fix the stream") and lines[-1] == "next: recipe status"


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


def test_style_exits_one_when_a_finding_is_an_error_and_zero_otherwise(wired, monkeypatch, capsys):
    monkeypatch.setitem(cli._VERBS, "style", lambda c, p: {**RESULTS["style"], "ok": False, "exit": 1})
    assert cli.main(["--host", "/work/host.toml", "style", "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and len(body["findings"]) == 2 and "exit" not in body
    monkeypatch.setitem(cli._VERBS, "style", lambda c, p: {**RESULTS["style"], "findings": []})
    assert cli.main(["--host", "/work/host.toml", "style", "src/a.c"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "next: recipe status"


def test_every_verb_runs_inside_the_lifecycle_guard_so_a_dead_caller_ends_it(wired, monkeypatch):
    seen = []

    @contextmanager
    def bound():
        seen.append("enter")
        yield
        seen.append("exit")

    monkeypatch.setattr(cli.lifecycle, "bound", bound)
    monkeypatch.setitem(cli._VERBS, "status", lambda config, params: seen.append("verb") or dict(RESULTS["status"]))
    assert cli.main(["--host", "/work/host.toml", "status"]) == 0
    assert seen == ["enter", "verb", "exit"]


def test_only_apply_holds_the_tree_lock_for_its_run(wired, monkeypatch, capsys):
    held = []

    @contextmanager
    def lock(config, **kw):
        held.append((kw["exclusive"], kw["who"]))
        yield 0.0

    monkeypatch.setattr(api, "tree_lock", lock)
    monkeypatch.setitem(cli._VERBS, "try", lambda config, params: dict(RESULTS["try"]))
    monkeypatch.setitem(cli._VERBS, "apply", lambda config, params: dict(RESULTS["apply"]))
    cli.main(["--host", "/work/host.toml", "try", "func_a", "--json"])
    cli.main(["--host", "/work/host.toml", "apply", "--json"])
    assert held == [(True, "apply")]  # a try holds none: its unbake commands take the shared lock one by one
