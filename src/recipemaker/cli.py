"""The `recipe` command line: short text ending in one `next:` line, or one JSON object with `--json`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import config, effort, human, store
from unbake.contracts import Config, Finding, Json, Refusal

from recipemaker import advice, brief, errors, lifecycle, naming, state, structure, style, tui, work

_VERBS: dict[str, Callable[[Config, Json], Json]] = {
    "next": work.next_item,
    "work": work.work,
    "try": work.try_,
    "skip": work.skip,
    "look": work.look_,
    "status": work.status,
    "structure": structure.run,
    "apply": structure.apply,
    "name": naming.run,
    "style": style.run,
}


def _landing(result: Json) -> list[str]:
    if result["landed"]:
        same = [f"same shape, still open: {' '.join(result['alike'])} (recipe work NAME)"] * bool(result["alike"])
        held = result.get("held") or {}
        exact = ", ".join(v for v in result["score"] if v not in held)
        what = f"exact for {exact}; {', '.join(held)} withheld ({brief.percents(held)}) " if held else ""
        return [f"landed {what}(commit {result['landed']})", *same]
    if result["saved"]:
        return [f"saved as nonmatching {brief.scores(result['score'])} (commit {result['saved']})"]
    done = [v for v, score in result["score"].items() if score >= 1]
    rest = [v for v in result["score"] if v not in done]
    return [f"exact for {', '.join(done)}; {', '.join(rest)} withheld on landing"] if done and rest else []


def _render_next(result: Json) -> list[str]:
    if not result["item"]:
        return [f"none: {result['why']}"]
    withheld = [
        f"withheld: {', '.join(result['withheld'])} (existing source, fix it for these versions); "
        f"a try aims to land exact for {', '.join(result['aims'])}"
    ]
    shape = "" if result["kind"] == "declaration" else f"   size: {result['size']} bytes   regions: {result['regions']}"
    brief_line = [f"brief: {result['brief']}"] if result["kind"] == "declaration" else []
    return [
        f"item: {result['item']}",
        f"kind: {result['kind']}{shape}",
        *brief_line,
        f"why: {result['why']}",
        *([f"evidence: {result['evidence']}"] * bool(result["evidence"])),
        *([f"twin: {result['twin']}"] if "twin" in result else []),
        f"lease: ends after {result['stall_tries']} tries without progress or {result['idle_minutes']:g} idle minutes",
        *(withheld if result["withheld"] else []),
    ]


def _render_work(result: Json) -> list[str]:
    if result.get("kind") == "declaration":
        return [*_render_next(result), f"wall: {result['wall_s']} s"]
    if result["result"] == "skipped":
        return [f"skipped {result['item']} as {result['reason']}: {result['why']}"]
    lines = [*_landing(result), f"result: {result['result']}"]
    if result["score"]:
        lines.append("score: " + brief.scores(result["score"]))
    if result["refused"]:
        lines.append(f"exact but refused: {result['refused']}")
    elif result["result"] == "exact" and not result["landed"]:
        lines.append(
            "not landed: " + ("submitted, waiting for the landing" if result["submitted"] else "not submitted")
        )
    if result["seed"]:
        lines.append(result["seed"])
    if result.get("unresolved_classes"):
        rows = sorted(result["unresolved_classes"].items(), key=lambda r: (-r[1], r[0]))
        lines.append("placeholders left: " + "; ".join(f"{n} {why}" for why, n in rows))
    if result["twin"]:
        lines.append("twin: " + result["twin"])
    if result["auto_fixes"]:
        lines.append("auto-fixes: " + "; ".join(result["auto_fixes"]))
    if "restored" in result:
        lines.append("restored: " + result["restored"])
    if result["result"] != "exact":
        lines += [f"file: {result['candidate']}", f"brief: {result['brief']}"]
        budget = result["budget"]
        lines.append(
            f"budget: {budget['stall_tries'] - budget['stalled']} tries without progress left, "
            f"{budget['minutes_left']:g} idle minutes left"
        )
    return [*lines, f"wall: {result['wall_s']} s (--seconds bounds the permuter only)"]


def _regions(found: Json) -> list[str]:
    if not found["total"]:
        return []
    gained = "" if found["gained"] is None else f" ({found['gained']:+d})"
    return [f"regions matching {found['matched']} of {found['total']}{gained}"]


def _render_try(result: Json) -> list[str]:
    word = result["result"] if result["compiled"] else "does not compile"
    budget = (
        f"{result['stalled']} of {result['stall_tries']} tries without progress, "
        f"{result['minutes_left']:g} idle minutes left"
    )
    lines = [*_landing(result), f"result: {word} ({budget})", "score: " + brief.scores(result["score"])]
    lines += [*_regions(result["regions"]), *([result["permuter"]] if "permuter" in result else [])]
    lines += [f"compiler: {text}" for text in result["compiler"]]
    if "error" in result:
        error = result["error"]
        where = f" at line {error['line']}" if error["line"] else ""
        lines += [f"error: {error['class']}{where}", f"what: {error['what']}", f"fix: {error['fix']}"]
    if result["auto_fixes"]:
        lines.append("auto-fixes: " + "; ".join(result["auto_fixes"]))
    if "restored" in result:
        lines.append("restored: " + result["restored"])
    if result["result"] != "exact":
        lines += [*(f"why: {w}" for w in result["why"]), f"file: {result['file']}", f"brief: {result['brief']}"]
    return lines


def _render_look(result: Json) -> list[str]:
    lines = [f"{result['kind']}: {result['name']}", result["text"]]
    if result["where"]:
        lines.append(f"where: {result['where']}")
    lines += [f"  {f['offset']} size {f['size']} {f['kind']} {f['name']}" for f in result["fields"]]
    if result["seen_in"]:
        lines.append("seen in: " + ", ".join(result["seen_in"][:8]))
    return [*lines, *result["facts"]]


def _render_status(result: Json) -> list[str]:
    today = result["today"]
    debt = [f"debt: {d['name']} blocks {d['blocks']} items, {d['spellings']} spellings" for d in result["debt"]]
    lines = [
        f"head: {result['head']}   open: {result['open']}   twin ready: {result['twin_ready']}",
        f"today: {today['exact']} exact, {today['fuzzy']} fuzzy, {today['skipped']} skipped, {today['tries']} tries",
    ]
    lines += [
        f"session {s['session']}: {s['item']}, {s['tries']} tries without progress, "
        f"{s['minutes_left']:g} idle minutes left"
        for s in result["sessions"]
    ]
    for label, key in (("skips", "skip_reasons"), ("errors", "error_classes")):
        if result[key]:
            lines.append(f"{label}: " + ", ".join(f"{k} {v}" for k, v in sorted(result[key].items())))
    return [*lines, *debt]


def _render_structure(result: Json) -> list[str]:
    sizes = [f"{k} {v['before']} -> {v['after']}" for k, v in result.items() if isinstance(v, dict) and "before" in v]
    return [
        f"proposed {result['moves']} moves in {result['path']}, {len(result['boundaries'])} boundaries",
        ", ".join(sizes),
    ]


def _render_apply(result: Json) -> list[str]:
    head = (
        f"segment {result['batch']}: {result['applied']} of {result['moves']} moves applied, {result['remaining']} left"
    )
    return [
        head,
        result["message"],
        *(f"refused {r['move']['subject']}: {r['key']} {r['reason']}" for r in result["refused"]),
    ]


def _render_name(result: Json) -> list[str]:
    if result["action"] == "next":
        pack = f"pack {result['pack']} holds {result['targets']} targets, " if result["pack"] else ""
        return [f"{result['step']}: {pack}{result['moves']} moves queued as batch names-{result['step']}"]
    if result["action"] == "submit":
        rules = ", ".join(f"{k} {v}" for k, v in sorted(result["by_rule"].items()))
        return [
            f"{result['accepted']} of {result['rows']} names accepted as {result['moves']} moves in {result['batch']}",
            *([f"refused {result['refused']}: {rules}"] if result["refused"] else []),
        ]
    return [f"{len(result['files'])} banner candidates, {result['skipped']} skipped"]


def _render_style(result: Json) -> list[str]:
    counts = ", ".join(f"{k} {v}" for k, v in result["census"].items() if v)
    lines = [f"{result['files']} files, {result['parsed']} parsed: {counts or 'no findings'}"]
    lines += [f"{r} rose from {a} to {b}" for r, (a, b) in sorted(result["rose"].items())]
    lines += [f"{f['rule']} {f['path']} {f['detail']}".rstrip() for f in result["findings"][:50]]
    return [*lines, *(["baseline written"] * result["baseline"])]


_RENDER: dict[str, Callable[[Json], list[str]]] = {
    "next": _render_next,
    "work": _render_work,
    "try": _render_try,
    "skip": lambda result: [f"skipped {result['item']} as {result['reason']}"],
    "look": _render_look,
    "status": _render_status,
    "structure": _render_structure,
    "apply": _render_apply,
    "name": _render_name,
    "style": _render_style,
}


def render(verb: str, result: Json) -> str:
    return "\n".join([*_RENDER[verb](result), f"next: {result['next']}"])


def _action(action: str, item: str | None) -> str:
    """An unbake action names an unbake command; the recipe verb that does it is try, or skip when only S can."""
    if "unbake " not in action:
        return action or "recipe status"
    if item is None:
        return "recipe status"
    return f"recipe try {item}" if action.startswith("unbake compare") else f"recipe skip {item} --reason other"


def _finding(finding: Finding, item: str | None = None) -> list[str]:
    if errors.own(finding):
        return errors.render(finding)
    try:
        text = human.finding(finding).splitlines()
    except KeyError:  # a key unbake's sentence table does not know
        return errors.render(finding)
    return [*[line for line in text if not line.strip().startswith("next:")], f"next: {_action(finding.action, item)}"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recipe", epilog=advice("sessions"), formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", type=Path, default=None, help="unbake host config (default: UNBAKE_HOST)")
    parser.add_argument("--project", type=Path, default=None, help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="verb", required=True)

    def verb(name: str, help_: str) -> argparse.ArgumentParser:
        one = sub.add_parser(name, help=help_)
        one.add_argument("--json", action="store_true", help="print one JSON object")
        one.add_argument("--stages", action="store_true", help="also print the stage tree and cache lines")
        return one

    one = verb("next", "lease the best open item")
    one.add_argument("--kind", choices=("function", "data", "declaration"))
    one.add_argument("--twin", choices=("yes", "no"), help="only items with (yes) or without (no) a landed twin")
    one.add_argument("--max-size", type=int, help="largest item in bytes")
    one.add_argument("--order", choices=("small", "large"), help="smallest or largest first (default: largest)")
    one = verb("work", "run the tool rungs on an item; prints once at exit, and a large item can take minutes")
    one.add_argument("item")
    one.add_argument("--kind", choices=("declaration",), help="work the declaration item of this name")
    one.add_argument("--seconds", type=float)
    one.add_argument("--no-submit", action="store_true")
    one = verb("try", "prove the candidate file")
    one.add_argument("item")
    one = verb("look", "show a prototype, type, field or global")
    one.add_argument("name")
    one = verb("skip", "release an item with a reason")
    one.add_argument("item")
    one.add_argument("--reason", required=True, choices=state.SKIP_REASONS)
    one.add_argument("--note", default="")
    one = verb("structure", "propose module structure: JSON move lines on stdout for unbake move -")
    one.add_argument("--segment", help="only these segments (comma list), authored modules included")
    one = verb("name", "name things from evidence: next (pack or catalog moves), submit FILE, banners")
    one.add_argument("action", choices=("next", "submit", "banners"))
    one.add_argument("file", nargs="?", help="the names file for submit")
    one.add_argument("--step", choices=("N0", "N1", "N2", "N3", "N4"), help="the naming step (default: N3)")
    one.add_argument("--count", type=int, default=40, help="targets per pack")
    one = verb("style", "style findings, or with --census the count per rule against the saved baseline")
    one.add_argument("paths", nargs="*", help="check only these paths")
    one.add_argument(
        "--census", action="store_true", help="count per rule; exit 1 when a count rose above the baseline"
    )
    one.add_argument("--baseline", action="store_true", help="write the counts as the new baseline")
    one = verb("apply", "send the next batch of proposed moves to unbake move -")
    one.add_argument(
        "--all", action="store_true", help="the whole stream as one unbake move --partial -, refusals recorded"
    )
    verb("status", "sessions, leases and counts")
    verb("tui", "curses views over the verbs")
    return parser


def _emit(text: str, document: Json, as_json: bool) -> None:
    sys.stdout.write((json.dumps(document, default=str) if as_json else text) + "\n")


def _where(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return f"{exc!r} at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else repr(exc)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    human.attach(sys.stderr, sys.stderr.isatty())
    host = args.host or (Path(os.environ["UNBAKE_HOST"]) if os.environ.get("UNBAKE_HOST") else None)
    item = getattr(args, "item", None)
    result: dict[str, Any] | None = None
    code, findings, landed = 0, [], None
    loaded: Config | None = None
    try:
        with lifecycle.bound(), effort.command(f"recipe {args.verb}", argv or sys.argv[1:]):
            if host is None:
                raise Refusal(Finding("config.missing", "no host config: pass --host or set UNBAKE_HOST"))
            loaded = config.load(args.project or Path.cwd(), host)
            effort.bind(loaded)
            if args.verb == "tui":
                return tui.run(loaded)
            given = {k: v for k, v in vars(args).items() if k not in ("host", "project")}
            if args.verb in ("structure", "apply", "name"):  # these verbs call unbake
                given["host"] = host
            result = dict(_VERBS[args.verb](loaded, given))
            code = int(result.pop("exit", 0))
    except Refusal as refusal:
        code, findings = 1, list(refusal.findings)
    except work.AfterLanding as late:
        code, findings, landed = 3, [Finding("internal.error", _where(late.cause), action="recipe next")], late
    except Exception as exc:
        again = f'recipe skip {item} --reason other --note "internal error"' if item else "recipe next"
        code, findings = 3, [Finding("internal.error", _where(exc), action=again)]
    finally:  # success, refusal, crash and the tui alike: the command's touched keys become one manifest
        if loaded is not None:
            store.finish_command(loaded)
    for line in human.summary(effort.closed()) if args.stages else []:
        sys.stderr.write(line + "\n")
    if result is not None:
        result["wall_s"] = round(next(r.wall_seconds for r in reversed(effort.closed()) if r.kind == "command"))
        stream = result.pop("stream", "")
        if stream and not args.json:  # the moves own stdout so that `recipe structure | unbake move -` works
            sys.stdout.write(stream)
            sys.stderr.write(render(args.verb, result) + "\n")
        else:
            _emit(render(args.verb, result), {"ok": True, **result}, args.json)
    else:
        first = []
        if landed is not None:
            first = [f"landed (commit {landed.landed})"] if landed.landed else []
            first += [f"saved as nonmatching (commit {landed.saved})"] if landed.saved else []
        lines = [*first, *[line for f in findings for line in _finding(f, item)]]
        refused = " ".join(["recipe", args.verb, item or ""]).strip()
        if lines[-1].startswith(f"next: {refused}"):  # a refusal never sends the agent back to the command it refused
            lines[-1] = "next: recipe status"
        document = {
            "ok": False,
            "landed": landed.landed if landed is not None else "",
            "findings": [asdict(f) for f in findings],
        }
        _emit("\n".join(lines), document, args.json)
    return code
