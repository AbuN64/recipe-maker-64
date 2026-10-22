"""The verbs: what each returns, each refusal, and the work each one costs."""

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import proof
from fakes import unit as make_unit
from unbake import effort
from unbake.contracts import Refusal

from recipemaker import cli, errors, host, index, ladder, state, work, world
from recipemaker.state import SKIP_REASONS
from recipemaker.world import Group

pytestmark = pytest.mark.usefixtures("toolchains")
NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(work, "_now", lambda: NOW)


def row(member, size=100, kind="function", address=0x80000400):
    return {"member": member, "kind": kind, "size": size, "address": address}


def candidate(fakes, text="int func_a(void) { return 0; }\n"):
    path = ladder.candidate_path(fakes.config, "func_a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def lease(fakes, item="func_a"):
    return state.take(fakes.config, item, NOW)


def exact_result(member="func_a", commit="abc1234"):
    proofs = [asdict(proof(member, v, 1.0)) for v in ("a", "b")]
    landed = [{"id": "x", "member": member, "operation": "publish", "commit": commit}]
    return {
        "exact": True,
        "score": {"a": 1.0, "b": 1.0},
        "proofs": proofs,
        "gaps": [],
        "drain": {"running": False, "landed": landed, "refused": []},
    }


def failing_result(text, score=0.4):
    proofs = [asdict(proof("func_a", v, score, [text])) for v in ("a", "b")]
    return {"exact": False, "score": {"a": score, "b": score}, "proofs": proofs, "gaps": [], "drain": None}


# -- next
def test_next_leases_the_largest_open_item_and_ends_with_the_work_command(fakes):
    fakes.items = [row("func_a", 40), row("func_b", 300)]
    got = work.next_item(fakes.config, {})
    assert (got["item"], got["size"], got["next"]) == ("func_b", 300, "recipe work func_b")
    assert [x.item for x in state.leases(fakes.config, NOW)] == ["func_b"]


def test_next_takes_a_member_with_no_group_and_refuses_a_row_the_layout_lacks(fakes):
    members = fakes.layout.members
    members["data_d"] = replace(members["data_d"], group="")
    fakes.items = [row("data_d", kind="data")]
    assert work.next_item(fakes.config, {})["item"] == "data_d"
    fakes.items = [row("ghost")]
    effort.forget("work.items")
    with pytest.raises(Refusal) as refused:
        work.next_item(fakes.config, {})
    assert refused.value.findings[0].key == "recipe.unknown-member"


def test_next_never_returns_a_leased_item_and_runs_out(fakes):
    fakes.items = [row("func_a"), row("func_b", 50)]
    assert work.next_item(fakes.config, {})["item"] == "func_a"
    assert work.next_item(fakes.config, {})["item"] == "func_b"
    assert work.next_item(fakes.config, {}) == {"item": "", "why": "no open item matches", "next": "recipe status"}


def test_next_grants_the_second_row_when_another_session_leases_the_top_one_while_it_ranks(fakes, monkeypatch):
    fakes.items = [row("func_a", 40), row("func_b", 300)]
    real = work.twin.ready

    def slow(snapshot):
        monkeypatch.setenv("RECIPE_SESSION", "s2")
        state.take(fakes.config, "func_b", NOW)
        monkeypatch.setenv("RECIPE_SESSION", "s1")
        return real(snapshot)

    monkeypatch.setattr(work.twin, "ready", slow)
    assert work.next_item(fakes.config, {})["item"] == "func_a"


def in_sdk_group(fakes, *names):
    """Move the named members into a library group that the layout marks as sdk."""
    lib = Group("lib", "main", names, True, "lib")
    members = {k: replace(m, group="lib") if k in names else m for k, m in fakes.layout.members.items()}
    groups = {**fakes.layout.groups, "lib": lib}
    fakes.layout = replace(fakes.layout, groups=groups, members=members)


def test_next_ranks_twin_ready_then_size_and_fragments_last_and_an_sdk_group_after_all_of_them(fakes):
    fakes.items = [row("func_a", 500), row("func_b", 8), row("data_d", 900, "data")]
    in_sdk_group(fakes, "data_d")
    assert [work.next_item(fakes.config, {})["item"] for _ in range(3)] == ["func_a", "func_b", "data_d"]


def test_next_prefers_a_twin_ready_item_over_a_bigger_one_and_twin_filters_split_ready_from_the_rest(fakes):
    fakes.items = [row("func_a", 20), row("func_b", 900)]
    fakes.with_units({"src/b.c": make_unit("src/b.c", ("func_x",))})
    fakes.shapes = {"s": ("func_a", "func_x")}
    got = work.next_item(fakes.config, {})
    assert got["item"] == "func_a" and "func_x in src/b.c" in got["why"] and "func_x in src/b.c" in got["twin"]
    state.release(fakes.config, "func_a")
    twinless = work.next_item(fakes.config, {"twin": "no"})
    assert (twinless["item"], twinless["twin"]) == ("func_b", "none")
    state.release(fakes.config, "func_b")
    assert work.next_item(fakes.config, {"twin": "yes"})["item"] == "func_a"


def test_next_ranks_an_item_open_only_in_another_version_behind_one_live_in_the_target_version(fakes):
    fakes.items = [row("func_a", 20), row("func_b", 900)]
    members = world.of(fakes.snapshot).members
    members["func_a"] = replace(members["func_a"], standing=("a",))
    members["func_b"] = replace(members["func_b"], standing=("b",))  # names_from is "a": b's work is elsewhere
    assert work.next_item(fakes.config, {})["item"] == "func_a"


def test_next_filters_by_kind(fakes):
    fakes.items = [row("func_a"), row("data_d", 10, "data")]
    assert work.next_item(fakes.config, {"kind": "data"})["item"] == "data_d"
    assert work.next_item(fakes.config, {"kind": "function"})["item"] == "func_a"


def test_next_hides_a_skipped_item_until_its_dependencies_change(fakes, monkeypatch):
    fakes.items = [row("func_a")]
    state.skip(fakes.config, "func_a", "other", "", 0.0, NOW - 10, work._deps(fakes.snapshot, "func_a"))
    assert work.next_item(fakes.config, {})["item"] == ""
    monkeypatch.setattr(
        index,
        "get",
        lambda s: SimpleNamespace(
            declarations={"callee": ("h.h", 1, "")}, identities=(), callers={}, shapes={}, extents={}
        ),
    )
    assert work.next_item(fakes.config, {})["item"] == "func_a"


# -- work
def test_work_returns_rungs_candidate_brief_and_a_try_command(fakes):
    lease(fakes)
    got = work.work(fakes.config, {"item": "func_a"})
    assert [r["name"] for r in got["rungs"]] == ["clone", "draft"]
    assert got["result"] == "fuzzy" and got["next"].endswith("toward a byte-exact match, then run: recipe try func_a")
    assert got["candidate"].endswith("func_a.c")
    text = (fakes.root / ".unbake/recipe/work/func_a/brief.md").read_text()
    assert "# func_a" in text and text.rstrip().endswith("next: recipe try func_a")
    assert got["budget"]["stall_tries"] == 4 and got["landed"] == "id-func_a" and got["score"] == {"a": 0.5, "b": 0.5}


def test_an_exact_work_lands_releases_the_lease_and_sends_the_agent_on(fakes):
    lease(fakes)
    fakes.scores.append(1.0)
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["result"] == "exact" and got["next"] == "recipe next" and got["brief"] == ""
    assert state.leases(fakes.config, NOW) == []


def test_work_takes_the_lease_itself_and_refuses_one_another_session_holds(fakes, monkeypatch):
    work.work(fakes.config, {"item": "func_a", "no_submit": True})
    assert [x.item for x in state.leases(fakes.config, NOW)] == ["func_a"]
    monkeypatch.setenv("RECIPE_SESSION", "s2")
    with pytest.raises(Refusal) as caught:
        work.work(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.leased"


def test_work_skips_a_fragment_as_split_symbol_without_any_measure(fakes):
    fakes.split["func_a"] = "func_b"
    got = work.work(fakes.config, {"item": "func_a"})
    assert (got["result"], got["reason"], got["next"]) == ("skipped", "split-symbol", "recipe next")
    assert fakes.calls["judge.score"] == 0 and state.leases(fakes.config, NOW) == []


def test_work_on_an_item_m2c_cannot_draft_starts_try_from_a_skeleton_and_says_why(fakes):
    fakes.m2c_fail = "/*\nDecompilation failure in function f:\n\nCannot find branch target\n*/"
    got = work.work(fakes.config, {"item": "func_a", "no_submit": True})
    text = (fakes.root / ".unbake/recipe/work/func_a.c").read_text()
    assert got["result"] == "open" and "func_a(" in text and text.rstrip().endswith("{\n}")
    assert "target assembly:" in text and "Cannot find branch target" in got["auto_fixes"][0]
    assert state.status(fakes.config, NOW)["skip_reasons"] == {}


def test_try_refuses_an_item_that_is_not_leased_and_never_calls_compare(fakes):
    candidate(fakes)
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.not-leased" and fakes.calls["judge.compare"] == 0


def test_try_refuses_when_the_budget_is_spent_skips_with_reason_budget_and_costs_no_compare(fakes):
    lease(fakes)
    candidate(fakes)
    for _ in range(4):
        state.count_try(fakes.config, "func_a", NOW, False)
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert (
        caught.value.findings[0].key == "recipe.budget"
        and "4 tries without progress" in caught.value.findings[0].reason
    )
    assert fakes.calls["judge.compare"] == 0 and state.status(fakes.config, NOW)["skip_reasons"] == {"budget": 1}


def test_a_spent_budget_names_the_fenced_fallback_and_the_behave_proof(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("bytes")
    for _ in range(5):  # the first raises the score, the next four do not
        last = work.try_(fakes.config, {"item": "func_a"})
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    for text in (last["next"], caught.value.findings[0].reason):
        assert "#ifdef NON_MATCHING" in text and "INCLUDE_ASM" in text and "owes a match" in text
        assert f"behave --function func_a --candidate {last['file']}" in text
    assert last["next"].startswith("match budget spent")


def test_try_takes_over_a_lease_whose_owner_process_has_exited(fakes, monkeypatch):
    monkeypatch.setenv("RECIPE_SESSION", "4194999")
    lease(fakes)
    candidate(fakes)
    monkeypatch.setenv("RECIPE_SESSION", "4194998")
    monkeypatch.setattr(state, "_dead", lambda owner: owner == "4194999")
    fakes.run_result = failing_result("bytes")
    assert work.try_(fakes.config, {"item": "func_a"})["brief"]
    assert state.lease(fakes.config, "func_a").session == "4194998"


def test_try_renews_this_sessions_lease_after_idle_time_instead_of_spending_the_budget(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("bytes")
    monkeypatch.setattr(work, "_now", lambda: NOW + 901)
    assert work.try_(fakes.config, {"item": "func_a"})["brief"]
    assert state.lease(fakes.config, "func_a").expires > NOW + 901


def test_try_refuses_when_the_tries_without_progress_are_spent(fakes):
    lease(fakes)
    candidate(fakes)
    for _ in range(4):
        state.count_try(fakes.config, "func_a", NOW, False)
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.budget" and fakes.calls["judge.compare"] == 0


def test_try_passes_a_header_another_writer_left_dirty(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.dirty = " M include/actor.h\n"
    fakes.run_result = failing_result("bytes")
    assert work.try_(fakes.config, {"item": "func_a"})["brief"]


def test_try_refuses_a_tracked_file_it_wrote_itself_and_never_suggests_a_checkout(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    real = work._prove

    def writes(config, item, path):
        fakes.dirty = " M include/actor.h\n"
        return real(config, item, path)

    fakes.run_result = failing_result("bytes")
    monkeypatch.setattr(work, "_prove", writes)
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    found = caught.value.findings[0]
    assert found.key == "recipe.dirty" and "checkout" not in found.action and "include/actor.h" in found.reason


def test_try_refuses_m2c_text_left_in_the_candidate_and_names_the_lines(fakes):
    lease(fakes)
    candidate(fakes, "void f(void) {\n    x = M2C_ERROR(bad);\n}\n")
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.placeholder" and "lines 2" in caught.value.findings[0].reason
    assert fakes.calls["judge.compare"] == 0


def test_try_refuses_when_there_is_no_candidate(fakes):
    lease(fakes)
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.no-candidate"


@pytest.mark.parametrize(
    "line",
    ["    x = (s32 *)arg0;", "    y = (struct Other *) &z;", "    ((s32 (*)(s32)) *fn)(1);"],
)
def test_try_refuses_a_cast_to_another_type_or_view_by_name_before_comparing(fakes, line):
    lease(fakes)
    candidate(fakes, f"void f(void) {{\n{line}\n}}\n")
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    found = caught.value.findings[0]
    assert found.key == "recipe.cast" and "a cast to another type or view (lines 2)" in found.reason
    assert fakes.calls["judge.compare"] == 0


def test_try_allows_value_casts_and_pointer_types_that_are_not_casts(fakes):
    lease(fakes)
    candidate(fakes, "void f(void *p) {\n    x = (s32)y + sizeof(void *);\n}\n")
    fakes.run_result = failing_result("bytes")
    assert work.try_(fakes.config, {"item": "func_a"})["result"] == "fuzzy"


def test_a_candidate_that_does_not_compile_gets_the_compiler_message_and_never_a_not_exact_error(fakes):
    lease(fakes)
    candidate(fakes)
    text = "compile.error: src/f.c:15: called object is not a function"
    fakes.run_result = failing_result(text)
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["compiled"] is False and got["compiler"] == ["src/f.c:15: called object is not a function"]
    assert got["error"]["class"] != "not-exact"


def test_a_fuzzy_landing_is_saved_as_nonmatching_not_landed_and_carries_no_error(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.operation = "fuzzy"
    fakes.landed.append("func_a")
    fakes.run_result = {**failing_result("bytes"), "drain": fakes.drain(fakes.config)}
    got = work.try_(fakes.config, {"item": "func_a"})
    assert (got["result"], got["landed"], got["saved"]) == ("fuzzy", "", "id-func_a") and "error" not in got
    text = cli.render("try", got)
    assert "saved as nonmatching a 40.0%, b 40.0% (id-func_a)" in text and "landed" not in text
    assert "error:" not in text


def test_the_brief_is_rewritten_after_every_try_that_is_not_exact(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("bytes")
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["brief"] == str(fakes.root / ".unbake/recipe/work/func_a/brief.md")
    assert "# func_a" in (fakes.root / ".unbake/recipe/work/func_a/brief.md").read_text()
    fakes.run_result = exact_result()
    assert work.try_(fakes.config, {"item": "func_a"})["brief"] == ""


def test_an_exact_try_lands_and_releases_the_lease(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = exact_result()
    got = work.try_(fakes.config, {"item": "func_a"})
    assert (got["result"], got["stalled"], got["landed"], got["next"]) == ("exact", 0, "abc1234", "recipe next")
    assert fakes.calls["judge.compare"] == 1 and state.leases(fakes.config, NOW) == []
    assert len(state.attempts(fakes.config, "func_a")) == 1
    assert state.status(fakes.config, NOW)["today"]["exact"] == 1


def test_a_failed_try_translates_the_error_and_points_at_the_line(fakes, monkeypatch):
    monkeypatch.setattr(
        errors,
        "classes",
        lambda: {
            "call-arity": {
                "match": "too many arguments to function [`'](?P<name>\\w+)'",
                "what": "{name} takes fewer",
                "fix": "drop one",
            }
        },
    )
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("compile.error: f.c:41: too many arguments to function `func_80012345'", 0.0)
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["result"] == "failed" and got["compiled"] is False and (got["stalled"], got["stall_tries"]) == (1, 4)
    assert got["error"] == {"class": "call-arity", "line": 41, "what": "func_80012345 takes fewer", "fix": "drop one"}
    assert got["next"] == "edit line 41, then run: recipe try func_a"
    assert state.status(fakes.config, NOW)["error_classes"] == {"call-arity": 1}


def test_an_unknown_error_is_reported_as_unknown(fakes, monkeypatch):
    monkeypatch.setattr(errors, "classes", lambda: {})
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("compile.error: f.c:3: strange", 0.0)
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["error"]["class"] == "unknown"


def test_the_previous_errors_auto_fix_runs_before_the_next_try(fakes, monkeypatch):
    monkeypatch.setattr(
        index,
        "get",
        lambda s: SimpleNamespace(
            declarations={"Actor": ("include/actor.h", 14, "")}, identities=(), callers={}, shapes={}
        ),
    )
    lease(fakes)
    path = candidate(fakes, "typedef struct {\n  int a;\n} Actor;\nint func_a(void) { return 0; }\n")
    host.append(
        fakes.config,
        "recipe/tries",
        {
            "time": NOW,
            "item": "func_a",
            "result": "failed",
            "error_class": "decl-conflict",
            "autofix": "drop-local-definition",
            "name": "Actor",
        },
    )
    fakes.run_result = failing_result("bytes")
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["auto_fixes"] == ["Removed local Actor (include/actor.h:14 defines it)"]
    assert "typedef" not in path.read_text()


# -- skip, look, status
def test_skip_releases_the_lease_records_the_reason_and_sends_on(fakes):
    lease(fakes)
    got = work.skip(fakes.config, {"item": "func_a", "reason": "jump-table", "note": "switch"})
    assert got == {"item": "func_a", "reason": "jump-table", "next": "recipe next"}
    assert state.leases(fakes.config, NOW) == [] and state.skipped(fakes.config, NOW) == {"func_a"}


def test_skip_refuses_a_reason_outside_the_list(fakes):
    with pytest.raises(Refusal):
        work.skip(fakes.config, {"item": "func_a", "reason": "bored"})
    assert "budget" in SKIP_REASONS


def test_look_points_back_at_the_leased_item(fakes, monkeypatch):
    lease(fakes)
    monkeypatch.setattr(work.look, "look", lambda config, name, asm="": {"name": name, "kind": "unknown"})
    assert work.look_(fakes.config, {"name": "x"})["next"] == "use it in your file, then run: recipe try func_a"


def test_status_adds_the_head_the_open_and_twin_counts_and_the_top_three_conflicts(fakes, monkeypatch):
    fakes.items = [row("func_a"), row("func_b")]
    rows = [{"name": f"g{i}", "spellings": [("a.h", 1, "x")] * 2, "items": ["f"] * i} for i in range(5)]
    monkeypatch.setattr(work.declare, "conflicts", lambda config, snapshot: rows)
    got = work.status(fakes.config, {})
    assert (got["head"], got["open"], got["twin_ready"]) == ("000000000", 2, 0)
    assert [d["name"] for d in got["debt"]] == ["g0", "g1", "g2"] and got["debt"][1] == {
        "name": "g1",
        "blocks": 1,
        "spellings": 2,
    }


# -- header patches wait for review
# -- refusal and budget cases added at the swap review
def test_try_budget_uses_the_configured_number_of_tries(fakes, tmp_path, monkeypatch):
    path = tmp_path / "recipe.toml"
    path.write_text("[budget]\nstall_tries = 2\nidle_minutes = 15\npermute_seconds = 120\nskip_days = 7\n")
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("bytes")
    for _ in range(3):  # the first try raises the score from nothing; the next two do not
        work.try_(fakes.config, {"item": "func_a"})
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert "2 tries without progress" in caught.value.findings[0].reason and fakes.calls["judge.compare"] == 3


def test_a_budget_refusal_skips_once_and_a_second_try_is_refused_as_not_leased(fakes):
    lease(fakes)
    candidate(fakes)
    for _ in range(4):
        state.count_try(fakes.config, "func_a", NOW, False)
    with pytest.raises(Refusal):
        work.try_(fakes.config, {"item": "func_a"})
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.not-leased"
    assert state.status(fakes.config, NOW)["skip_reasons"] == {"budget": 1} and fakes.calls["judge.compare"] == 0


def test_a_landing_unbake_refused_is_translated_and_keeps_the_lease(fakes, monkeypatch):
    monkeypatch.setattr(
        errors,
        "classes",
        lambda: {
            "decl-conflict": {"key": "headers.conflict", "what": "declared two ways", "fix": "use the header's type"}
        },
    )
    lease(fakes)
    candidate(fakes)
    refused = {
        "id": "x",
        "member": "func_a",
        "findings": [
            {
                "key": "headers.conflict",
                "reason": "Actor differs",
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
        ],
    }
    result = exact_result()
    result["drain"] = {"running": False, "landed": [], "refused": [refused]}
    fakes.run_result = result
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["result"] == "refused"
    assert got["error"]["class"] == "decl-conflict" and got["landed"] == ""
    assert [x.item for x in state.leases(fakes.config, NOW)] == ["func_a"]
    assert state.lease(fakes.config, "func_a").tries == 0  # exact but refused by a check: no try is spent


def test_work_on_an_item_whose_source_exists_is_refused_and_costs_no_try_or_measure(fakes):
    fakes.with_units({"src/a.c": make_unit("src/a.c", ("func_a",))})
    lease(fakes)
    with pytest.raises(Refusal) as caught:
        work.work(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.landed" and fakes.calls["judge.score"] == 0
    assert state.lease(fakes.config, "func_a").tries == 0


def test_no_submit_reaches_the_ladder(fakes):
    lease(fakes)
    fakes.scores.append(1.0)
    got = work.work(fakes.config, {"item": "func_a", "no_submit": True})
    assert got["submitted"] is None and fakes.calls["judge.submit"] == 0


def test_skip_of_an_item_nobody_leased_still_records_the_reason(fakes):
    work.skip(fakes.config, {"item": "func_b", "reason": "other"})
    assert state.status(fakes.config, NOW)["skip_reasons"] == {"other": 1}


# -- a rung that cannot run, and leases that do not strand the user
def test_work_with_every_rung_refused_still_returns_and_keeps_the_lease(fakes, monkeypatch):
    from unbake.contracts import Finding

    def no_tool(*args, **kwargs):
        raise Refusal(Finding("recipe.tool", "no path for m2c"))

    monkeypatch.setattr(ladder.draft, "drafted", no_tool)
    lease(fakes)
    got = work.work(fakes.config, {"item": "func_a", "no_submit": True})
    assert got["result"] == "open" and "skeleton" in got["auto_fixes"][0]
    assert [r["result"] for r in got["rungs"] if r["name"] == "draft"] == ["skipped"]
    assert [x.item for x in state.leases(fakes.config, NOW)] == ["func_a"]


def test_an_unexpected_error_releases_a_lease_the_command_took_and_keeps_an_older_one(fakes, monkeypatch):
    monkeypatch.setattr(ladder, "run", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        work.work(fakes.config, {"item": "func_a"})
    assert state.leases(fakes.config, NOW) == []
    lease(fakes, "func_b")
    with pytest.raises(ZeroDivisionError):
        work.work(fakes.config, {"item": "func_b"})
    assert [x.item for x in state.leases(fakes.config, NOW)] == ["func_b"]


def test_next_leaves_out_items_over_the_asked_size_and_ranks_an_sdk_group_last(fakes):
    fakes.items = [row("func_a", 500), row("func_b", 100), row("data_d", 150, "data")]
    in_sdk_group(fakes, "func_b")
    assert work.next_item(fakes.config, {"max_size": 200})["item"] == "data_d"  # func_a is too big; func_b ranks last
    assert work.next_item(fakes.config, {"max_size": 200})["item"] == "func_b"
    assert work.next_item(fakes.config, {"max_size": 200})["item"] == ""
    state.release(fakes.config, "func_b")
    assert work.next_item(fakes.config, {"max_size": 100})["item"] == "func_b"
    assert work.next_item(fakes.config, {"max_size": 10})["item"] == ""


def test_the_retired_next_table_of_the_config_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "recipe.toml"
    path.write_text(
        "[budget]\nstall_tries = 4\nidle_minutes = 15\npermute_seconds = 120\nskip_days = 7\n[next]\nmax_size = 200\n"
    )
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    with pytest.raises(Refusal) as caught:
        state.settings()
    assert "next" in caught.value.findings[0].reason


def test_work_on_a_twin_ready_function_m2c_cannot_draft_keeps_the_twin_result_and_skips_nothing(fakes):
    unit = make_unit("src/b.c", ("func_b",))
    fakes.with_units({"src/b.c": unit})
    fakes.snapshot = replace(fakes.snapshot, overlays={"src/b.c": b"int func_b(void) {\n    return 0;\n}\n"})
    fakes.shapes = {"s": ("func_a", "func_b")}
    fakes.m2c_fail = "/*\nDecompilation failure in function f:\n\nsomething else\n*/"
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["result"] == "fuzzy" and [r["name"] for r in got["rungs"]][:2] == ["clone", "draft"]
    assert state.status(fakes.config, NOW)["skip_reasons"] == {}


def test_an_exact_result_that_unbake_refused_to_land_says_so_and_keeps_the_lease(fakes, monkeypatch):
    finding = {"reason": "'' should be non-empty", "path": "layout.toml:unit.1.group"}
    refused = {"id": "x", "member": "func_a", "findings": [finding]}
    monkeypatch.setattr(fakes, "drain", lambda config: {"running": False, "landed": [], "refused": [refused]})
    fakes.scores.append(1.0)
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["result"] == "refused" and got["landed"] == "" and "should be non-empty" in got["refused"]
    assert [x.item for x in state.leases(fakes.config, NOW)] == ["func_a"] and got["next"] == "recipe try func_a"


def test_an_exact_result_is_said_to_be_not_landed_when_nothing_was_submitted(fakes):
    fakes.scores.append(1.0)
    got = work.work(fakes.config, {"item": "func_a", "no_submit": True})
    text = cli.render("work", {**got, "wall_s": 1})
    assert got["result"] == "exact" and got["landed"] == "" and "not landed: not submitted" in text
    assert got["next"] == "recipe next" and state.leases(fakes.config, NOW) == []


def test_an_exact_result_that_was_not_sent_keeps_its_lease_and_points_at_try(fakes):
    fakes.m2c_text = "void {name}(void) {{\n    M2C_ERROR(1);\n}}\n"
    fakes.scores.append(1.0)
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["result"] == "refused" and got["submitted"] is None and got["next"] == "recipe try func_a"
    assert "M2C_ text on line 3" in got["refused"] and [x.item for x in state.leases(fakes.config, NOW)] == ["func_a"]


def test_an_item_work_skipped_is_never_handed_out_again_by_next(fakes):
    fakes.items = [row("func_a")]
    fakes.split["func_a"] = "func_b"
    assert work.work(fakes.config, {"item": "func_a"})["result"] == "skipped"
    assert work.next_item(fakes.config, {})["item"] == ""


def test_a_try_that_raises_the_score_does_not_count_and_one_that_does_not_does(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    for _ in range(3):
        state.count_try(fakes.config, "func_a", NOW, False)
    fakes.run_result = failing_result("bytes", 0.4)
    monkeypatch.setattr(work, "_best", lambda config, item: 0.1)
    assert work.try_(fakes.config, {"item": "func_a"})["stalled"] == 0
    for _ in range(3):
        state.count_try(fakes.config, "func_a", NOW, False)
    monkeypatch.setattr(work, "_best", lambda config, item: 0.4)
    assert work.try_(fakes.config, {"item": "func_a"})["stalled"] == 4


def test_a_skip_for_a_header_change_is_offered_again_ahead_of_fresh_items_once_its_blocker_is_declared_differently(
    fakes, monkeypatch
):
    record = fakes.snapshot.versions["a"]
    fakes.snapshot = replace(
        fakes.snapshot, versions={**fakes.snapshot.versions, "a": replace(record, symbols={"D_B": 0x100})}
    )
    fakes.items = [row("func_a", size=50), row("func_b", size=500)]
    candidate(fakes, "void func_a(void) {\n    M2C_FIELD(&D_B, s32 *, 4) = 1;\n}\n")
    lease(fakes)
    declared = {"D_B": ("h.h", 1, "extern s32 D_B;")}
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations=declared, identities=(), callers={}, shapes={}, extents={})
    )
    work.skip(fakes.config, {"item": "func_a", "reason": "needs-header-change"})
    assert state.blocked(fakes.config) == {"func_a": {"D_B": "extern s32 D_B;"}}
    assert work.next_item(fakes.config, {})["item"] == "func_b"  # still blocked: the bigger fresh item
    state.release(fakes.config, "func_b")
    declared["D_B"] = ("h.h", 1, "extern struct Cup D_B;")
    got = work.next_item(fakes.config, {})
    assert got["item"] == "func_a" and "waiting on" in got["why"]


def test_the_hi_and_lo_symbols_of_a_function_are_part_of_what_a_skip_depends_on(fakes, monkeypatch):
    path = fakes.root / "asm/a/func_a.s"
    path.write_text("glabel func_a\n lui $t0, %hi(D_1)\n lw $t1, %lo(D_1)($t0)\n")
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations={}, identities=(), callers={}, shapes={}, extents={})
    )
    bare = work._deps(fakes.snapshot, "func_a")
    monkeypatch.setattr(
        index,
        "get",
        lambda s: SimpleNamespace(
            declarations={"D_1": ("h.h", 1, "")}, identities=(), callers={}, shapes={}, extents={}
        ),
    )
    assert work._deps(fakes.snapshot, "func_a") != bare


def test_a_landing_names_the_open_functions_of_the_same_shape(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = exact_result()
    monkeypatch.setattr(work.twin, "alike", lambda snapshot, item: ["func_b"])
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["alike"] == ["func_b"]
    assert "same shape, still open: func_b" in cli.render("try", got)


def runs(fakes, *results):
    """Proving the candidate answers in turn; every call is counted."""
    queue = list(results)
    fakes.run_calls = []

    def run(config, item, path):
        fakes.run_calls.append(str(path))
        return queue.pop(0)

    return run


def test_perm_marks_the_permuter_could_not_resolve_are_refused_and_never_landed(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes, "int func_a(void) { PERM_LINESWAP(a = 1;\nb = 2;) }\n")
    with pytest.raises(Refusal) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.findings[0].key == "recipe.perm" and fakes.calls["judge.compare"] == 0


def area(kind, start=0):
    return {"start": start, "size": 8, "kind": kind, "anchors": "calls func_x", "lines": []}


def test_try_says_how_many_regions_match_and_points_at_the_first_that_does_not(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.regions = [area("match"), area("match", 8), area("order", 0x1A4)]
    fakes.run_result = failing_result("bytes", 0.4)
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["regions"] == {"matched": 2, "total": 3, "gained": None}
    assert got["next"] == "region 3 at +0x1A4 (calls func_x): order; edit the file, then run: recipe try func_a"
    assert "regions matching 2 of 3" in cli.render("try", got)


def test_a_try_that_matches_more_regions_counts_as_progress_even_when_the_score_did_not_move(fakes):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("bytes", 0.4)
    fakes.regions = [area("match"), area("other", 8), area("other", 16)]
    work.try_(fakes.config, {"item": "func_a"})
    work.try_(fakes.config, {"item": "func_a"})  # same score, same regions: a stalled try
    assert state.lease(fakes.config, "func_a").tries == 1
    fakes.regions = [area("match"), area("match", 8), area("other", 16)]
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["regions"]["gained"] == 1 and state.lease(fakes.config, "func_a").tries == 0
    assert "(+1)" in cli.render("try", got)


def test_next_prints_the_region_count_so_a_weak_model_can_decline_a_big_function(fakes):
    fakes.items = [row("func_a", 40)]
    (fakes.root / "asm/a/func_a.s").write_text(
        "    /* 0 80000400 03E00008 */  jr         $ra\n    /* 4 80000404 00000000 */   nop\n"
        "  .Lauto_80000408:\n    /* 8 80000408 03E00008 */  jr         $ra\n    /* C 8000040C 00000000 */   nop\n"
    )
    assert work.next_item(fakes.config, {})["regions"] == 2


def test_a_failure_after_a_landing_is_raised_as_after_landing_with_the_commit_and_frees_the_lease(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = exact_result()
    monkeypatch.setattr(work.twin, "alike", lambda snapshot, item: 1 / 0)
    with pytest.raises(work.AfterLanding) as caught:
        work.try_(fakes.config, {"item": "func_a"})
    assert caught.value.landed == "abc1234" and isinstance(caught.value.cause, ZeroDivisionError)
    assert state.leases(fakes.config, NOW) == []


def test_a_failure_before_any_landing_is_raised_as_it_is(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("bytes")
    monkeypatch.setattr(work.crack, "packet", lambda snapshot, item: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        work.try_(fakes.config, {"item": "func_a"})


def test_a_type_unresolved_skip_records_the_global_the_asm_uses_as_a_pointer_as_its_blocker(fakes, monkeypatch):
    fakes.items = [row("func_a", size=50)]
    candidate(fakes, "void func_a(void) {\n}\n")
    lease(fakes)
    (fakes.root / "asm/a/func_a.s").write_text(
        "    /* 0 80000400 8DCE28BC */  lw         $t6, %lo(D_B)($t6)\n"
        "    /* 4 80000404 8DC20004 */  lw         $v0, 0x4($t6)\n"
    )
    record = fakes.snapshot.versions["a"]
    fakes.snapshot = replace(
        fakes.snapshot, versions={**fakes.snapshot.versions, "a": replace(record, symbols={"D_B": 0x100})}
    )
    declared = {"D_B": ("h.h", 1, "extern s32 D_B;")}
    monkeypatch.setattr(
        index, "get", lambda s: SimpleNamespace(declarations=declared, identities=(), callers={}, shapes={}, extents={})
    )
    work.skip(fakes.config, {"item": "func_a", "reason": "type-unresolved"})
    assert state.blocked(fakes.config) == {"func_a": {"D_B": "extern s32 D_B;"}}


def test_next_order_small_takes_the_smallest_first_and_says_so_and_no_flag_keeps_the_largest_first(fakes):
    fakes.items = [row("func_a", 20), row("func_b", 300)]
    got = work.next_item(fakes.config, {"order": "small"})
    assert (got["item"], got["why"]) == ("func_a", "smallest open item")
    state.release(fakes.config, "func_a")
    assert work.next_item(fakes.config, {})["item"] == "func_b"


def test_work_says_how_many_lines_a_resumed_seed_holds_that_would_be_refused(fakes, monkeypatch):
    path = candidate(fakes, "int func_a(void) {\n    return (u8 *) 0;\n}\nint x = M2C_FIELD;\n")
    lease(fakes)
    seeded = ladder.Outcome("func_a", (), {"a": 0.5}, False, str(path), None, None, ("resumed from x at 50.0%",))
    monkeypatch.setattr(work.ladder, "run", lambda *a, **k: seeded)
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["seed"].startswith("resumed seed holds 2 refusable lines") and "lines 2, 4" in got["seed"]
    assert got["seed"] in cli.render("work", {**got, "wall_s": 1})


def test_a_refused_work_result_prints_the_file_and_the_brief(fakes, monkeypatch):
    path = candidate(fakes, "int func_a(void) {\n    return (u8 *) 0;\n}\n")
    lease(fakes)
    done = ladder.Outcome("func_a", (), {"a": 1.0}, True, str(path), None, None)
    monkeypatch.setattr(work.ladder, "run", lambda *a, **k: done)
    got = work.work(fakes.config, {"item": "func_a"})
    text = cli.render("work", {**got, "wall_s": 1})
    assert got["result"] == "refused" and f"file: {path}" in text and "brief: " + got["brief"] in text and got["brief"]


def test_work_on_a_conflicting_declaration_name_leases_the_declaration_item(fakes, monkeypatch):
    found = {
        "name": "func_f",
        "spellings": [("include/f.h", 1, "s32 func_f(void);")],
        "items": ["func_x"],
        "definition": None,
    }
    monkeypatch.setattr(work.declare, "conflicts", lambda config, snapshot: [found])
    monkeypatch.setattr(work.declare.state, "skipped", lambda config, now: frozenset())
    fakes.items = [row("func_a")]
    got = work.work(fakes.config, {"item": "func_f"})
    assert got["kind"] == "declaration" and got["item"] == "func_f"
    assert "kind: declaration" in cli.render("work", {**got, "wall_s": 1})


def test_kind_declaration_reaches_the_declaration_item_of_a_name_that_is_also_an_open_function(fakes, monkeypatch):
    found = {"name": "func_a", "spellings": [("include/f.h", 1, "s32 func_a(void);")], "items": [], "definition": None}
    monkeypatch.setattr(work.declare, "conflicts", lambda config, snapshot: [found])
    fakes.items = [row("func_a")]
    got = work.work(fakes.config, {"item": "func_a", "kind": "declaration"})
    assert got["kind"] == "declaration"
    monkeypatch.setattr(work.declare, "conflicts", lambda config, snapshot: [])
    with pytest.raises(Refusal) as caught:
        work.work(fakes.config, {"item": "func_a", "kind": "declaration"})
    assert caught.value.findings[0].key == "recipe.no-declaration"


def test_work_leases_a_census_conflict_with_an_empty_recipe_state(fakes, monkeypatch):
    from unbake.contracts import Finding

    found = Finding(
        "types.conflict", "gX", unit="gX", missing=("gX: include/a.h:9 extern f32 gX;", "gX: src/a.c:3 s32 gX;")
    )
    monkeypatch.setattr(work.declare.headers, "disagreements", lambda snapshot: ({}, [found]))
    got = work.work(fakes.config, {"item": "gX"})
    assert (got["kind"], got["item"]) == ("declaration", "gX") and not store_rows(fakes)


def store_rows(fakes):
    return [r for r in work.host.rows(fakes.config, "recipe/conflicts")]


def test_a_declaration_conflict_names_the_canonical_declaration_and_points_at_work(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = failing_result("compile.error: f.c:11: conflicting types for `func_f'", 0.0)
    proto = {
        "name": "func_f",
        "definition": ("src/f.c", 1, "s32 func_f(s32 a, s32 *b)"),
        "spellings": [("src/f.c", 1, "s32 func_f(s32 a, s32 *b)"), ("include/f.h", 4, "s32 func_f(void);")],
    }
    monkeypatch.setattr(work.declare, "conflicts", lambda config, snapshot: [proto])
    got = work.try_(fakes.config, {"item": "func_a"})
    assert "canonical: src/f.c defines `s32 func_f(s32 a, s32 *b)`" in got["why"]
    assert "declared: include/f.h:4 s32 func_f(void);" in got["why"] and got["next"] == (
        "fix the declaration item func_f first: recipe work func_f --kind declaration (leases it; "
        "you keep func_a), then recipe try func_f, then recipe try func_a"
    )


def test_try_and_work_on_a_landed_item_say_which_commit_landed_it(fakes, monkeypatch):
    fakes.items = []
    fakes.with_units({"src/a.c": make_unit("src/a.c", ("func_a",))})
    monkeypatch.setattr(work.host, "git", lambda config, *a: SimpleNamespace(stdout=b"c0ffee1\n"))
    for verb in (work.try_, work.work):
        with pytest.raises(Refusal) as caught:
            verb(fakes.config, {"item": "func_a"})
        assert caught.value.findings[0].reason == "func_a is already landed (commit c0ffee1)"


def test_a_landing_whose_lease_is_already_gone_does_not_fail_the_try(fakes, monkeypatch):
    lease(fakes)
    candidate(fakes)
    fakes.run_result = exact_result()
    prove = work._prove

    def landing(config, item, path):
        state.release(config, item)
        return prove(config, item, path)

    monkeypatch.setattr(work, "_prove", landing)
    got = work.try_(fakes.config, {"item": "func_a"})
    assert got["result"] == "exact" and got["landed"] == "abc1234"


def test_an_exact_candidate_that_needs_a_cast_or_an_unused_local_points_at_the_refused_form_skip(fakes, monkeypatch):
    path = candidate(fakes, "int func_a(void) {\n    s32 x = 0;\n    return (u8 *) 0;\n}\n")
    lease(fakes)
    exact = ladder.Outcome("func_a", (), {"a": 1.0, "b": 1.0}, True, str(path), None, None)
    monkeypatch.setattr(work.ladder, "run", lambda *a, **k: exact)
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["result"] == "refused" and got["brief"]
    assert got["next"] == "the exact form needs refused text: recipe skip func_a --reason other --note refused-form"


def test_work_on_data_never_lands_and_ends_open_with_a_brief(fakes, monkeypatch):
    fakes.scores.append(1.0)
    monkeypatch.setattr(work.brief, "data_facts", lambda snapshot, member: ["## Data", "symbol: data_d"])
    got = work.work(fakes.config, {"item": "data_d"})
    assert (got["result"], got["submitted"], got["landed"]) == ("open", None, "")
    assert (
        "symbol: data_d" in Path(got["brief"]).read_text()
        and got["next"] == "read the brief, edit the file toward a byte-exact match, then run: recipe try data_d"
    )


def test_a_leased_data_item_with_no_assembly_never_breaks_look_next_status_or_skip(fakes, monkeypatch):
    seen = []
    monkeypatch.setattr(work.look, "look", lambda config, name, item="": seen.append(item) or {"name": name})
    fakes.items = [row("data_d", kind="data"), row("func_a")]
    assert work.next_item(fakes.config, {"kind": "data"})["item"] == "data_d"
    assert work.look_(fakes.config, {"name": "x"})["next"] == "use it in your file, then run: recipe try data_d"
    assert seen == [""] and work.status(fakes.config, {})["open"] == 2
    assert work.next_item(fakes.config, {})["item"] == "func_a"
    got = work.skip(fakes.config, {"item": "data_d", "reason": "other", "note": "no bytes to type"})
    assert got["next"] == "recipe next" and state.lease(fakes.config, "data_d").expires == 0.0


# -- keep only if not worse
GOOD = "int func_a(void) { return 1; }\n"


@pytest.mark.parametrize(
    "text,score,restored",
    [
        ("int func_a(void) { return 2; }\n", 0.3, True),  # scores lower in both versions
        ("int other(void) { return 2; }\n", 0.9, True),  # the function is gone from the file
        ("int func_a(void) { return 3; }\n", 0.5, False),  # equal
        ("int func_a(void) { return 4; }\n", 0.7, False),  # better
    ],
)
def test_try_keeps_the_lanes_text_and_saves_the_previous_content_beside_it_when_the_candidate_is_worse(
    fakes, text, score, restored
):
    lease(fakes)
    path = candidate(fakes, GOOD)
    fakes.run_result = failing_result("bytes", 0.5)
    work.try_(fakes.config, {"item": "func_a"})
    path.write_text(text)
    fakes.run_result = failing_result("bytes", score)
    got = work.try_(fakes.config, {"item": "func_a"})
    assert path.read_text() == text  # the lane's own text is never rewritten under it
    assert ("restored" in got) == restored
    if restored:
        assert "previous content" in got["restored"] and "REGRESSED:" in cli.render("try", got)
        assert path.with_name(path.name + ".prev").read_text() == GOOD


def test_a_landing_row_without_a_commit_is_named_by_its_submission_id():
    drain = {"landed": [{"id": "sub1", "member": "func_a", "operation": "publish"}]}
    assert work._commits(drain, "func_a") == ("sub1", "")


def test_next_leases_the_first_item_whose_draft_compiles_and_names_the_ones_it_passed(fakes, monkeypatch):
    fakes.items = [row("func_a", 40), row("func_b", 300)]
    probed = []

    def create(snapshot, member, out):
        probed.append(member)
        return "version a: compile.error: NULL undeclared" if member == "func_b" else ""

    monkeypatch.setattr(work.draft, "create", create)
    got = work.next_item(fakes.config, {})
    assert (got["item"], got["draft_failed"]) == ("func_a", ["func_b"])
    assert probed == ["func_b", "func_a"]  # one draft per ranked item up to the first that compiles
    state.release(fakes.config, "func_a")
    work.next_item(fakes.config, {})  # the verdicts are in the content cache: no second draft
    assert probed == ["func_b", "func_a"]


def test_next_without_the_probe_and_for_items_that_start_from_a_candidate_never_drafts(fakes, monkeypatch):
    fakes.items = [row("func_a", 40), row("func_b", 300)]
    monkeypatch.setattr(work.draft, "create", lambda *a: pytest.fail("no draft expected"))
    assert work.next_item(fakes.config, {"probe": False})["item"] == "func_b"
    path = candidate(fakes, "int func_a(void) { return 0; }\n")
    assert path.exists()
    state.release(fakes.config, "func_b")
    fakes.items = [row("func_a", 40)]
    effort.forget("work.items")
    assert work.next_item(fakes.config, {})["item"] == "func_a"


def test_next_ends_the_probe_after_its_cap_and_still_returns_an_item(fakes, monkeypatch):
    fakes.items = [row("func_a", 100), row("func_b", 200)]
    monkeypatch.setattr(work, "_PROBES", 1)
    monkeypatch.setattr(work.draft, "create", lambda *a: "never compiles")
    got = work.next_item(fakes.config, {})
    assert got["item"] == "func_a" and got["draft_failed"] == ["func_b"]


def test_the_twin_memos_are_read_from_the_content_cache_by_the_next_command(fakes):
    fakes.items = [row("func_a", 40)]
    fakes.shapes = {"s": ("func_a", "func_x")}
    work.twin.ready(fakes.snapshot)
    first = fakes.calls["infer.pairs"]
    effort.forget("twin.ready")  # a new command: the in-process memo is empty
    assert work.twin.ready(fakes.snapshot) == frozenset() and fakes.calls["infer.pairs"] == first
    fakes.pairs = {}
    assert index.persisted(fakes.snapshot, "recipe.x", lambda: 1, extra="k") == 1
    assert index.persisted(fakes.snapshot, "recipe.x", lambda: 2, extra="k") == 1
    assert index.persisted(fakes.snapshot, "recipe.x", lambda: 3, extra="other") == 3


def _four_members(fakes):
    members = fakes.layout.members
    more = {n: replace(members["func_a"], name=n) for n in ("func_c", "func_d")}
    fakes.layout = replace(fakes.layout, members={**members, **more})


def test_next_probes_one_draft_first_then_a_wider_batch_and_takes_the_first_that_compiles(fakes, monkeypatch):
    _four_members(fakes)
    fakes.items = [row("func_b", 900), row("func_c", 800), row("func_d", 700), row("func_a", 600)]
    monkeypatch.setattr(work.draft, "create", lambda s, m, out: "" if m == "func_a" else "does not compile")
    got = work.next_item(fakes.config, {})
    assert got["item"] == "func_a" and got["draft_failed"] == ["func_b", "func_c", "func_d"]
    assert [j for j in fakes.pool_jobs if j[0] == "recipe.probe"] == [("recipe.probe", 1), ("recipe.probe", 3)]


def test_next_never_dispatches_a_probe_the_cache_holds(fakes, monkeypatch):
    _four_members(fakes)
    fakes.items = [row("func_b", 900), row("func_c", 800), row("func_d", 700), row("func_a", 600)]
    monkeypatch.setattr(work.draft, "create", lambda s, m, out: "" if m == "func_a" else "does not compile")
    work.next_item(fakes.config, {})
    state.release(fakes.config, "func_a")
    fakes.pool_jobs.clear()
    got = work.next_item(fakes.config, {})
    assert got["item"] == "func_a" and got["draft_failed"] == ["func_b", "func_c", "func_d"]
    assert [j for j in fakes.pool_jobs if j[0] == "recipe.probe"] == []  # every verdict came from the cache


def test_a_probe_verdict_is_keyed_on_the_project_and_the_member_and_read_without_the_pool(fakes):
    assert work._verdict(fakes.config, fakes.snapshot, "func_a") is None
    index.persisted(fakes.snapshot, "recipe.draftable", lambda: "why not", extra="func_a", sources=True)
    assert work._verdict(fakes.config, fakes.snapshot, "func_a") == "why not"
    assert work._verdict(fakes.config, fakes.snapshot, "func_b") is None


def test_the_headers_digest_behind_a_probe_key_is_read_once_per_snapshot(fakes, monkeypatch):
    reads = []
    monkeypatch.setattr(index.headers, "sources", lambda s: reads.append(1) or [])
    index.key(fakes.snapshot, "recipe.k", extra="a", sources=True)
    index.key(fakes.snapshot, "recipe.k", extra="b", sources=True)
    assert len(reads) == 1


def test_work_names_the_offsets_no_declared_record_could_name_and_the_skip_that_stops_on_them(fakes, monkeypatch):
    from recipemaker.ledger import Left

    path = candidate(fakes, "int func_a(void) {\n    return 0;\n}\n")
    lease(fakes)
    left = (Left("arg0", 0x9C, "", (), False), Left("arg0", 0x9C, "", (), False), Left("arg1", 0x20, "", (), False))
    open_ = ladder.Outcome("func_a", (), {"a": 0.5}, False, str(path), None, None, (), left)
    monkeypatch.setattr(work.ladder, "run", lambda *a, **k: open_)
    got = work.work(fakes.config, {"item": "func_a"})
    assert got["unresolved"] == ["arg0 at 0x9C", "arg1 at 0x20"]
    assert got["next"].endswith('recipe skip func_a --reason type-unresolved --note "arg0 at 0x9C, arg1 at 0x20"')
    none = ladder.Outcome("func_a", (), {"a": 0.5}, False, str(path), None, None)
    monkeypatch.setattr(work.ladder, "run", lambda *a, **k: none)
    assert work.work(fakes.config, {"item": "func_a"})["unresolved"] == []
