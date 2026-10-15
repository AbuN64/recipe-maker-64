"""`recipe tui`: curses views over the verbs. Lane B owns this file."""

from __future__ import annotations

import curses
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unbake import layout, report, store
from unbake.contracts import Config, Json, Refusal

from recipemaker import state, work

Row = dict[str, Any]
VIEWS = ("Queue", "Item", "Status")
LIMIT = 500  # queue rows kept; the queue is a window onto the ranked list, not the whole project
ASM_LINES = 40


@dataclass
class Model:
    view: int = 0
    rows: list[Row] = field(default_factory=list)
    status: Json = field(default_factory=dict)
    cursor: int = 0
    item: str = ""
    message: str = ""
    quit: bool = False


class Live:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _snapshot(self) -> Any:
        return layout.capture(self.config)

    def queue(self) -> list[Row]:
        snapshot = self._snapshot()
        rows = [dict(r) for r in report.items(snapshot, {})[:LIMIT]]
        for row in rows:
            tried = state.attempts(self.config, row["member"])
            row["last"] = tried[-1].outcome if tried else ""
        return rows

    def status(self) -> Json:
        return work.status(self.config, {})

    def next_item(self) -> Json:
        return work.next_item(self.config, {})

    def verb(self, name: str, item: str, **params: object) -> Json:
        call = {"work": work.work, "try": work.try_, "skip": work.skip}[name]
        return call(self.config, {"item": item, **params})

    def look(self, word: str) -> Json:
        return work.look_(self.config, {"name": word})

    def target(self, item: str) -> str:
        return "".join(work._asm(self._snapshot(), item).splitlines(keepends=True)[:ASM_LINES])

    def candidate_path(self, item: str) -> Path:
        return self.config.project.root / ".unbake/recipe/work" / f"{store.stem(item)}.c"

    def edit(self, item: str) -> None:
        subprocess.run([*shlex.split(os.environ.get("EDITOR") or "vi"), str(self.candidate_path(item))], check=False)


def refresh(model: Model, backend: Any) -> None:
    model.rows = backend.queue()
    model.status = backend.status()
    model.cursor = min(model.cursor, max(len(model.rows) - 1, 0))


def _say(model: Model, result: Json) -> None:
    error = result.get("error")
    if isinstance(error, dict):  # try_: {class, line, what, fix}
        error = f"{error['class']} at line {error['line']}: {error['what']}"
    model.message = str(result.get("next") or error or result.get("result") or result.get("why") or result)


def handle(model: Model, key: str, backend: Any, ask: Callable[[str], str]) -> None:
    try:
        _apply(model, key, backend, ask)
    except Refusal as refusal:
        finding = refusal.findings[0]
        model.message = finding.reason or finding.key


def _apply(model: Model, key: str, backend: Any, ask: Callable[[str], str]) -> None:
    if key == "q":
        model.quit = True
    elif key in ("1", "2", "3"):
        model.view = int(key) - 1
    elif key == "tab":
        model.view = (model.view + 1) % len(VIEWS)
    elif key == "r":
        refresh(model, backend)
        model.message = "refreshed"
    elif key in ("down", "j") and model.rows:
        model.cursor = min(model.cursor + 1, len(model.rows) - 1)
    elif key in ("up", "k"):
        model.cursor = max(model.cursor - 1, 0)
    elif key == "n":
        result = backend.next_item()
        model.item = str(result.get("item", ""))
        model.view = 1 if model.item else 0
        _say(model, result)
    elif key == "enter" and model.view == 0 and model.rows:
        model.item = model.rows[model.cursor]["member"]
        model.view = 1
    elif model.view == 1 and model.item:
        _item_key(model, key, backend, ask)


def _item_key(model: Model, key: str, backend: Any, ask: Callable[[str], str]) -> None:
    if key in ("w", "t"):
        _say(model, backend.verb("work" if key == "w" else "try", model.item))
    elif key == "s":
        reason = ask(f"reason {'|'.join(state.SKIP_REASONS)} [other]: ").strip() or "other"
        _say(model, backend.verb("skip", model.item, reason=reason, note=""))
    elif key == "e":
        backend.edit(model.item)
        model.message = "edited " + model.item
    elif key == "l":
        word = ask("look up: ").strip()
        if word:
            result = backend.look(word)
            model.message = str(result.get("text") or result.get("next") or result)


def _row(row: Row) -> str:
    return f"{row['member'][:34]:<34} {row['segment']:<9} {row['size']:>6}  {row.get('last', '')}"


def lines(model: Model, backend: Any, width: int, height: int) -> list[str]:
    tabs = "  ".join(f"[{n}]" if i == model.view else f" {n} " for i, n in enumerate(VIEWS))
    out = [tabs, ""]
    if model.view == 0:
        out.append(f"{'item':<34} {'segment':<9} {'size':>6}  last")
        for i, row in enumerate(model.rows):
            out.append(("> " if i == model.cursor else "  ") + _row(row))
    elif model.view == 1:
        out.append(f"item: {model.item or '(none: press n for the next item)'}")
        if model.item:
            out += ["", "target:", *backend.target(model.item).splitlines()]
            path = backend.candidate_path(model.item)
            out += ["", "candidate:", *(path.read_text().splitlines() if path.exists() else ["(none yet: press w)"])]
    else:
        out += [f"{k}: {v}" for k, v in model.status.items()]
    help_line = {
        0: "n next  enter open  r refresh  q quit",
        1: "w work  t try  e edit  l look  s skip  n next  q quit",
        2: "r refresh  q quit",
    }[model.view]
    room = max(height - 3, 1)
    body = [ln[: max(width - 1, 1)] for ln in out[:room]]
    footer = ["", help_line[: max(width - 1, 1)], model.message[: max(width - 1, 1)]]
    return body + [""] * (room - len(body)) + footer


_KEYS = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", 9: "tab", 10: "enter", curses.KEY_ENTER: "enter"}


def _loop(screen: Any, backend: Any) -> None:
    model = Model()
    refresh(model, backend)

    def ask(prompt: str) -> str:
        height, _ = screen.getmaxyx()
        curses.echo()
        screen.addstr(height - 1, 0, prompt)
        text: str = screen.getstr(height - 1, len(prompt), 80).decode(errors="replace")
        curses.noecho()
        return text

    while not model.quit:
        height, width = screen.getmaxyx()
        screen.erase()
        for y, text in enumerate(lines(model, backend, width, height)):
            screen.addstr(y, 0, text)
        screen.refresh()
        code = screen.getch()
        handle(model, str(_KEYS.get(code, chr(code) if 0 <= code < 256 else "")), backend, ask)


def run(config: Config) -> int:
    curses.wrapper(_loop, Live(config))
    return 0
