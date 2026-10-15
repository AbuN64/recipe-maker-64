"""tui: key handling and screen text against a fake backend; the live backend against faked unbake seams."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from unbake.contracts import Finding, Refusal

from recipemaker import tui

ROWS = [
    {"member": "func_a", "kind": "function", "segment": "sdk", "size": 100, "last": ""},
    {"member": "func_b", "kind": "function", "segment": "audio", "size": 300, "last": "better"},
    {"member": "func_c", "kind": "function", "segment": "audio", "size": 40, "last": ""},
]


class Fake:
    """Counts every call, like the unbake seam fakes; `next_item` answers from a script."""

    def __init__(self, root: Path) -> None:
        self.calls: Counter[str] = Counter()
        self.args: list[tuple[Any, ...]] = []
        self.root = root
        self.upcoming: dict[str, Any] = {"item": "func_b", "next": "recipe work func_b"}

    def _note(self, name: str, *args: Any) -> None:
        self.calls[name] += 1
        self.args.append((name, *args))

    def queue(self) -> list[dict[str, Any]]:
        self._note("queue")
        return [dict(r) for r in ROWS]

    def status(self) -> dict[str, Any]:
        self._note("status")
        return {"open": 3, "today": {"exact": 1}}

    def next_item(self) -> dict[str, Any]:
        self._note("next")
        return self.upcoming

    def verb(self, name: str, item: str, **params: object) -> dict[str, Any]:
        self._note(name, item, params)
        return {"result": f"{name} done", "next": f"recipe next after {name}"}

    def look(self, word: str) -> dict[str, Any]:
        self._note("look", word)
        return {"text": f"{word}: s32 f(void)"}

    def target(self, item: str) -> str:
        return "glabel f\n  jr $ra\n"

    def candidate_path(self, item: str) -> Path:
        return self.root / f"{item}.c"

    def edit(self, item: str) -> None:
        self._note("edit", item)


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    return Fake(tmp_path)


def press(model: tui.Model, fake: Fake, *keys: str, answers: tuple[str, ...] = ()) -> None:
    replies = iter(answers)
    for key in keys:
        tui.handle(model, key, fake, lambda prompt: next(replies))


def loaded(fake: Fake) -> tui.Model:
    model = tui.Model()
    tui.refresh(model, fake)
    return model


def test_refresh_reads_the_queue_and_status_once(fake: Fake) -> None:
    loaded(fake)
    assert fake.calls == {"queue": 1, "status": 1}


def test_views_switch_by_number_and_tab_and_the_cursor_stays_in_range(fake: Fake) -> None:
    model = loaded(fake)
    press(model, fake, "3")
    assert tui.VIEWS[model.view] == "Status"
    press(model, fake, "tab", "tab")
    assert model.view == 1
    press(model, fake, "1", "down", "down", "down", "down", "j")
    assert model.cursor == 2
    press(model, fake, "up", "up", "up", "k")
    assert model.cursor == 0


def test_next_leases_through_the_verb_and_opens_the_item(fake: Fake) -> None:
    model = loaded(fake)
    press(model, fake, "n")
    assert fake.calls["next"] == 1
    assert (model.view, model.item, model.message) == (1, "func_b", "recipe work func_b")


def test_next_with_nothing_to_lease_stays_on_the_queue(fake: Fake) -> None:
    fake.upcoming = {"error": "no open item"}
    model = loaded(fake)
    press(model, fake, "n")
    assert (model.view, model.item, model.message) == (0, "", "no open item")


def test_enter_opens_the_row_under_the_cursor(fake: Fake) -> None:
    model = loaded(fake)
    press(model, fake, "down", "enter")
    assert (model.view, model.item) == (1, "func_b")


def test_item_keys_call_one_verb_each_and_show_its_next_line(fake: Fake) -> None:
    model = loaded(fake)
    model.view, model.item = 1, "func_b"
    press(model, fake, "w", "t", "e", "l", answers=("func_9",))
    assert fake.calls == {"queue": 1, "status": 1, "work": 1, "try": 1, "edit": 1, "look": 1}
    assert ("look", "func_9") in fake.args and model.message == "func_9: s32 f(void)"


def test_skip_asks_for_a_reason_and_defaults_to_other(fake: Fake) -> None:
    model = loaded(fake)
    model.view, model.item = 1, "func_b"
    press(model, fake, "s", "s", answers=("jump-table", ""))
    reasons = [a[2]["reason"] for a in fake.args if a[0] == "skip"]
    assert reasons == ["jump-table", "other"]


def test_item_keys_do_nothing_without_an_item_and_q_quits(fake: Fake) -> None:
    model = loaded(fake)
    press(model, fake, "2", "w", "t", "s", "e", "l")
    assert not ({"work", "try", "skip", "edit", "look"} & set(fake.calls))
    press(model, fake, "q")
    assert model.quit


def test_screen_text_marks_the_tab_the_cursor_and_fits_the_window(fake: Fake) -> None:
    model = loaded(fake)
    press(model, fake, "down")
    screen = tui.lines(model, fake, 40, 12)
    assert len(screen) == 12 and all(len(row) < 40 for row in screen)
    assert screen[0].startswith("[Queue]") and "Item" in screen[0]
    marked = [row for row in screen if row.startswith("> ")]
    assert len(marked) == 1 and "func_b" in marked[0]
    assert "n next" in screen[-2]


def test_item_screen_shows_target_and_candidate(fake: Fake) -> None:
    model = loaded(fake)
    model.view, model.item = 1, "func_b"
    text = "\n".join(tui.lines(model, fake, 80, 20))
    assert "jr $ra" in text and "(none yet: press w)" in text
    fake.candidate_path("func_b").write_text("void f(void) {}\n")
    assert "void f(void) {}" in "\n".join(tui.lines(model, fake, 80, 20))


def test_status_screen(fake: Fake) -> None:
    model = loaded(fake)
    model.view = 2
    assert any(row == "open: 3" for row in tui.lines(model, fake, 60, 12))


def test_live_queue_keeps_the_report_order_and_reads_each_row_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: Counter[str] = Counter()
    items = [
        {"member": m, "kind": "function", "segment": g, "size": s, "address": 0}
        for m, g, s in (("a", "x", 10), ("b", "y", 20), ("c", "x", 30))
    ]
    monkeypatch.setattr(tui.layout, "capture", lambda config: calls.update(["capture"]) or "snapshot")
    monkeypatch.setattr(tui.report, "items", lambda snapshot, params: items)
    history = {"c": [SimpleNamespace(outcome="worse"), SimpleNamespace(outcome="better")]}
    monkeypatch.setattr(
        tui.state, "attempts", lambda config, member: calls.update(["attempts"]) or history.get(member, [])
    )
    live = tui.Live(SimpleNamespace(project=SimpleNamespace(root=tmp_path)))  # type: ignore[arg-type]
    rows = live.queue()
    assert [(r["member"], r["segment"], r["last"]) for r in rows] == [
        ("a", "x", ""),
        ("b", "y", ""),
        ("c", "x", "better"),
    ]
    assert calls == {"capture": 1, "attempts": 3}
    monkeypatch.setattr(tui, "LIMIT", 2)
    assert [r["member"] for r in live.queue()] == ["a", "b"]


def test_live_edit_runs_the_editor_as_an_argv_list_with_the_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(tui.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    live = tui.Live(SimpleNamespace(project=SimpleNamespace(root=tmp_path)))  # type: ignore[arg-type]
    monkeypatch.setenv("EDITOR", "ed -s")
    live.edit("func_a")
    monkeypatch.delenv("EDITOR")
    live.edit("func_a")
    path = str(tmp_path / ".unbake/recipe/work/func_a.c")
    assert seen == [(["ed", "-s", path], {"check": False}), (["vi", path], {"check": False})]


def test_live_calls_each_work_verb_with_the_params_lane_a_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    for name in ("next_item", "work", "try_", "skip", "look_", "status"):
        monkeypatch.setattr(tui.work, name, lambda config, params, name=name: seen.append((name, dict(params))) or {})
    live = tui.Live(SimpleNamespace(project=SimpleNamespace(root=tmp_path)))  # type: ignore[arg-type]
    live.next_item()
    live.verb("work", "f")
    live.verb("try", "f")
    live.verb("skip", "f", reason="budget", note="")
    live.look("f")
    live.status()
    assert seen == [
        ("next_item", {}),
        ("work", {"item": "f"}),
        ("try_", {"item": "f"}),
        ("skip", {"item": "f", "reason": "budget", "note": ""}),
        ("look_", {"name": "f"}),
        ("status", {}),
    ]


def test_a_failed_try_shows_its_error_class_when_there_is_no_next_line(fake: Fake) -> None:
    model = tui.Model()
    tui._say(model, {"result": "failed", "error": {"class": "call-arity", "line": 41, "what": "bad call"}})
    assert model.message == "call-arity at line 41: bad call"
    tui._say(model, {"item": "", "why": "no open item"})
    assert model.message == "no open item"


def test_the_skip_reason_goes_to_the_verb_unchanged_so_the_verb_is_the_guard(fake: Fake) -> None:
    model = loaded(fake)
    model.view, model.item = 1, "func_b"
    press(model, fake, "s", answers=("not-a-reason",))
    assert [a[2]["reason"] for a in fake.args if a[0] == "skip"] == ["not-a-reason"]
    assert fake.calls["skip"] == 1


def test_an_empty_queue_has_no_cursor_to_move_or_open(fake: Fake) -> None:
    model = tui.Model()
    press(model, fake, "down", "enter", "up")
    assert (model.cursor, model.view, model.item) == (0, 0, "")
    assert not fake.calls


def test_a_refused_verb_shows_its_reason_and_the_loop_keeps_going(fake: Fake) -> None:
    def refuse(name: str, item: str, **params: object) -> dict[str, Any]:
        raise Refusal(Finding("land.request", "budget spent (4 of 4 tries)"))

    fake.verb = refuse  # type: ignore[method-assign]
    model = loaded(fake)
    model.view, model.item = 1, "func_b"
    press(model, fake, "t")
    assert model.message == "budget spent (4 of 4 tries)" and not model.quit
    press(model, fake, "1")
    assert model.view == 0
