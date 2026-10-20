"""Leases, budgets, skips and tries, over the real store in a scratch project."""

from dataclasses import replace

import pytest
from unbake import store
from unbake.contracts import Refusal

from recipemaker import state

NOW = 1_000_000.0


def test_a_missing_config_file_is_refused_by_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RECIPE_CONFIG", str(tmp_path / "gone.toml"))
    with pytest.raises(Refusal) as caught:
        state.settings()
    assert "gone.toml does not exist" in caught.value.findings[0].reason


@pytest.mark.parametrize(
    "key",
    ["budget.stall_tries", "budget.idle_minutes", "budget.permute_seconds", "budget.skip_days"],
)
def test_a_missing_key_is_refused_by_name(tmp_path, monkeypatch, key):
    path = tmp_path / "recipe.toml"
    table, _, name = key.rpartition(".")
    full = {"budget": {"stall_tries": 4, "idle_minutes": 15, "permute_seconds": 120, "skip_days": 7}}
    del (full[table] if table else full)[name]
    path.write_text("".join(f"[{t}]\n" + "".join(f"{k} = {v}\n" for k, v in rows.items()) for t, rows in full.items()))
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    with pytest.raises(Refusal) as caught:
        state.settings()
    assert f"recipe.toml:{key} is missing" in caught.value.findings[0].reason


def test_settings_read_the_budget_table(tmp_path, monkeypatch):
    path = tmp_path / "recipe.toml"
    path.write_text("[budget]\nstall_tries = 2\nidle_minutes = 5\npermute_seconds = 30\nskip_days = 7\n")
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    rules = state.settings()
    assert (rules.stall_tries, rules.idle_minutes, rules.permute_seconds) == (2, 5, 30)


def test_take_leases_for_the_idle_minutes_and_a_command_renews_the_same_session_lease(fakes):
    first = state.take(fakes.config, "func_a", NOW)
    assert (first.session, first.expires - first.started, first.tries) == ("s1", 900, 0)
    assert state.take(fakes.config, "func_a", NOW + 60) == replace(first, expires=NOW + 60 + 900)
    assert [lease.item for lease in state.leases(fakes.config, NOW + 60)] == ["func_a"]


def test_take_refuses_an_item_another_session_holds_until_the_lease_expires(fakes, monkeypatch):
    state.take(fakes.config, "func_a", NOW)
    monkeypatch.setenv("RECIPE_SESSION", "s2")
    with pytest.raises(Refusal) as caught:
        state.take(fakes.config, "func_a", NOW + 1)
    assert caught.value.findings[0].key == "recipe.leased"
    assert state.take(fakes.config, "func_a", NOW + 901).session == "s2"


def test_held_refuses_a_free_an_expired_and_another_sessions_lease(fakes, monkeypatch):
    with pytest.raises(Refusal) as caught:
        state.held(fakes.config, "func_a", NOW)
    assert caught.value.findings[0].key == "recipe.not-leased"
    state.take(fakes.config, "func_a", NOW)
    assert state.held(fakes.config, "func_a", NOW + 10).item == "func_a"
    with pytest.raises(Refusal):
        state.held(fakes.config, "func_a", NOW + 901)
    monkeypatch.setenv("RECIPE_SESSION", "s2")
    with pytest.raises(Refusal):
        state.held(fakes.config, "func_a", NOW + 10)


def test_a_stalled_try_adds_one_a_try_that_made_progress_refunds_and_release_frees_the_item(fakes):
    state.take(fakes.config, "func_a", NOW)
    assert state.count_try(fakes.config, "func_a", NOW, False).tries == 1
    assert state.count_try(fakes.config, "func_a", NOW, False).tries == 2
    assert state.count_try(fakes.config, "func_a", NOW, True).tries == 0
    state.release(fakes.config, "func_a")
    assert state.leases(fakes.config, NOW) == []


def test_skip_writes_a_row_releases_the_lease_and_hides_the_item_for_the_return_days(fakes):
    state.take(fakes.config, "func_a", NOW)
    state.skip(fakes.config, "func_a", "jump-table", "switch", 0.4, NOW, "d1")
    assert state.leases(fakes.config, NOW) == []
    assert state.skipped(fakes.config, NOW + 86400 * 6) == {"func_a"}
    assert state.skipped(fakes.config, NOW + 86400 * 8) == frozenset()


def test_a_skipped_item_reopens_when_what_it_depends_on_changes(fakes):
    state.skip(fakes.config, "func_a", "type-unresolved", "", 0.0, NOW, "d1")
    assert state.skipped(fakes.config, NOW + 1, {"func_a": "d1"}) == {"func_a"}
    assert state.skipped(fakes.config, NOW + 1, {"func_a": "d2"}) == frozenset()


def test_skip_refuses_a_reason_that_is_not_listed(fakes):
    with pytest.raises(Refusal) as caught:
        state.skip(fakes.config, "func_a", "tired", "", 0.0, NOW)
    assert caught.value.findings[0].key == "recipe.reason"


def test_tries_and_attempt_rows_round_trip(fakes):
    store.append(fakes.config, "recipe/tries", {"time": NOW, "item": "func_a", "result": "exact"})
    assert state.attempts(fakes.config, "func_a") == []
    assert state.status(fakes.config, NOW)["today"]["exact"] == 1


def test_status_counts_the_day_skip_reasons_error_classes_and_sessions(fakes):
    state.take(fakes.config, "func_b", NOW)
    rows = [("exact", ""), ("failed", "call-arity"), ("failed", "call-arity")]
    for result, cls in rows:
        store.append(
            fakes.config, "recipe/tries", {"time": NOW, "item": "func_a", "result": result, "error_class": cls}
        )
    state.skip(fakes.config, "func_a", "budget", "", 0.0, NOW)
    body = state.status(fakes.config, NOW)
    assert body["today"] == {"exact": 1, "fuzzy": 0, "skipped": 1, "tries": 3}
    assert body["skip_reasons"] == {"budget": 1} and body["error_classes"] == {"call-arity": 2}
    assert body["sessions"][0]["item"] == "func_b"


def test_tools_come_from_the_recipe_config_tools_table(tmp_path, monkeypatch):
    path = tmp_path / "recipe.toml"
    path.write_text('[tools]\nm2c = "/work/tools/m2c"\npermuter = "/work/tools/permuter"\n')
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    assert state.tool("m2c").as_posix() == "/work/tools/m2c" and state.tool("permuter").name == "permuter"


def test_a_tool_the_config_does_not_name_is_refused_with_the_line_to_add():
    with pytest.raises(Refusal) as caught:
        state.tool("m2c")
    found = caught.value.findings[0]
    assert found.key == "recipe.tool" and "[tools]" in found.action


@pytest.mark.parametrize(
    "body",
    ['[tools]\nmystery = "x"\n', "[tools]\nm2c = 3\n", "[budget]\nstall_tries = 0\n", "[extra]\na = 1\n", "not toml ["],
)
def test_the_recipe_config_is_checked_against_its_schema(tmp_path, monkeypatch, body):
    path = tmp_path / "recipe.toml"
    path.write_text(body)
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    with pytest.raises(Refusal) as caught:
        state.settings()
    assert caught.value.findings[0].key == "recipe.config"


def test_a_held_lease_lock_blocks_the_next_command_until_it_is_released(fakes):
    import threading
    import time

    from unbake import store

    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with store.exclusive(fakes.config, "recipe", wait=True):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    threading.Timer(0.2, release.set).start()
    started = time.monotonic()
    state.take(fakes.config, "func_a", NOW)
    holder.join()
    assert time.monotonic() - started >= 0.15 and [x.item for x in state.leases(fakes.config, NOW)] == ["func_a"]


def test_a_try_cannot_be_counted_on_an_item_that_is_not_leased(fakes):
    with pytest.raises(Refusal) as caught:
        state.count_try(fakes.config, "func_a", NOW, False)
    assert caught.value.findings[0].key == "recipe.not-leased"


def test_the_budget_follows_the_config_not_a_built_in_number(tmp_path, monkeypatch, fakes):
    path = tmp_path / "recipe.toml"
    path.write_text("[budget]\nstall_tries = 2\nidle_minutes = 1\npermute_seconds = 120\nskip_days = 7\n")
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    lease = state.take(fakes.config, "func_a", NOW)
    assert lease.expires - lease.started == 60


def test_a_live_lease_is_never_granted_to_another_session_even_when_its_process_has_exited(fakes, monkeypatch):
    first = state.take(fakes.config, "func_a", NOW)
    monkeypatch.setenv("RECIPE_SESSION", "s2")
    with pytest.raises(Refusal) as refused:
        state.take(fakes.config, "func_a", NOW + 30)
    assert "leased to session" in refused.value.findings[0].reason
    assert state.take(fakes.config, "func_a", first.expires + 1).session == "s2"  # only expiry frees it


def test_two_sessions_racing_for_one_item_grant_exactly_one(fakes, monkeypatch):
    import contextlib
    import threading

    monkeypatch.setattr(state, "session", lambda: threading.current_thread().name)
    won: list[str] = []

    def grab() -> None:
        with contextlib.suppress(Refusal):
            won.append(state.take(fakes.config, "func_a", NOW).session)

    threads = [threading.Thread(target=grab, name=f"s{n}") for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(won) == 1


def test_a_refusal_for_a_lost_lease_says_to_whom_and_since_when(fakes, monkeypatch):
    state.take(fakes.config, "func_a", NOW)
    monkeypatch.setenv("RECIPE_SESSION", "s2")
    with pytest.raises(Refusal) as refused:
        state.held(fakes.config, "func_a", NOW + 1)
    reason = refused.value.findings[0].reason
    assert state.session() != "" and "has been leased to session" in reason and "since" in reason


def test_the_same_session_keeps_its_tries_when_it_comes_back_from_another_process(fakes):
    state.take(fakes.config, "func_a", NOW)
    state.count_try(fakes.config, "func_a", NOW, False)
    assert state.take(fakes.config, "func_a", NOW + 1).tries == 1


def test_a_skip_row_from_before_blockers_existed_has_none(fakes):
    store.append(fakes.config, "recipe/skips", {"time": NOW, "item": "func_a", "reason": "other", "deps": ""})
    state.skip(fakes.config, "func_b", "needs-header-change", "", 0.0, NOW, "", {"D_X": "extern s32 D_X;"})
    assert state.blocked(fakes.config) == {"func_b": {"D_X": "extern s32 D_X;"}}


def test_worst_is_the_lowest_score_and_zero_without_one_and_minutes_left_rounds_to_a_tenth(fakes):
    assert (state.worst({"a": 0.9, "b": 0.4}), state.worst({})) == (0.4, 0.0)
    assert state.minutes_left(state.take(fakes.config, "func_a", NOW), NOW + 60) == 14.0


def test_take_gives_the_same_session_its_expired_lease_back_with_its_tries_and_not_another_session(fakes, monkeypatch):
    state.take(fakes.config, "func_a", NOW)
    state.count_try(fakes.config, "func_a", NOW + 1, False)
    again = state.take(fakes.config, "func_a", NOW + 5000)
    assert (again.session, again.tries, again.expires) == ("s1", 1, NOW + 5000 + 900)
