"""The `recipe` command line: short text ending in one `next:` line, or one JSON object with `--json`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from unbake import api
from unbake.contracts import Config, Finding, Json, Refusal

from recipemaker import advice, brief, errors, flags, lifecycle, naming, retext, state, structure, style, tui, work

_VERBS: dict[str, Callable[[Config, Json], Json]] = {
    "next": work.next_item,
    "work": work.work,
    "try": work.try_,
    "skip": work.skip,
    "look": work.look_,
    "status": work.status,
    "structure": structure.run,
    "apply": structure.apply,
    "flags": flags.run,
    "name": naming.run,
    "style": style.run,
    "text": retext.run,
}


def _landing(result: Json) -> list[str]:
    if result["landed"]:
        same = [f"same shape, still open: {' '.join(result['alike'])} (recipe work NAME)"] * bool(result["alike"])
        return [f"landed ({result['landed']})", *same]
    if result["saved"]:
        return [f"saved as nonmatching {brief.scores(result['score'])} ({result['saved']})"]
    return []


def _render_next(result: Json) -> list[str]:
    if not result["item"]:
        return [f"none: {result['why']}"]
    shape = "" if result["kind"] == "declaration" else f"   size: {result['size']} bytes   regions: {result['regions']}"
    brief_line = [f"brief: {result['brief']}"] if result["kind"] == "declaration" else []
    return [
        f"item: {result['item']}",
        f"kind: {result['kind']}{shape}",
        *brief_line,
        f"why: {result['why']}",
        *([f"twin: {result['twin']}"] if "twin" in result else []),
        f"lease: ends after {result['stall_tries']} tries without progress or {result['idle_minutes']:g} idle minutes",
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
        lines.append("REGRESSED: " + result["restored"])
    if result["result"] != "exact":
        lines += [f"file: {result['candidate']}", f"brief: {result['brief']}"]
        budget = result["budget"]
        lines.append(
            f"budget: {budget['stall_tries'] - budget['stalled']} tries without progress left, "
            f"{budget['minutes_left']:g} idle minutes left"
        )
    return [*lines, f"wall: {result['wall_s']} s"]


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
    lines += _regions(result["regions"])
    lines += [f"compiler: {text}" for text in result["compiler"]]
    if "error" in result:
        error = result["error"]
        where = f" at line {error['line']}" if error["line"] else ""
        lines += [f"error: {error['class']}{where}", f"what: {error['what']}", f"fix: {error['fix']}"]
    if result["auto_fixes"]:
        lines.append("auto-fixes: " + "; ".join(result["auto_fixes"]))
    if "restored" in result:
        lines.append("REGRESSED: " + result["restored"])
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
        f"proposed {result['moves']} moves in {result['path'] or 'no patch'}, {len(result['boundaries'])} boundaries",
        ", ".join(sizes),
        *result.get("findings", []),
    ]


def _render_apply(result: Json) -> list[str]:
    return [f"applied {result['patch']}, {result['left']} patches left, unbake sync done"]


def _render_name(result: Json) -> list[str]:
    if result["action"] == "next":
        pack = f"pack {result['pack']} holds {result['targets']} targets, " if result["pack"] else ""
        return [f"{result['step']}: {pack}{result['moves']} moves in patch {result['path'] or 'none'}"]
    if result["action"] == "submit":
        rules = ", ".join(f"{k} {v}" for k, v in sorted(result["by_rule"].items()))
        return [
            f"{result['accepted']} of {result['rows']} names accepted as {result['moves']} moves"
            f" in {result['path'] or 'no patch'}",
            *([f"refused {result['refused']}: {rules}"] if result["refused"] else []),
        ]
    return [f"{len(result['files'])} banner candidates, {result['skipped']} skipped"]


def _render_style(result: Json) -> list[str]:
    counts = ", ".join(f"{k} {v}" for k, v in result["census"].items() if v)
    lines = [f"{result['files']} files, {result['parsed']} parsed: {counts or 'no findings'}"]
    lines += [f"{r} rose from {a} to {b}" for r, (a, b) in sorted(result["rose"].items())]
    lines += [f"{f['rule']} {f['path']} {f['detail']}".rstrip() for f in result["findings"][:50]]
    return [*lines, *(["baseline written"] * result["baseline"])]


def _render_flags(result: Json) -> list[str]:
    lines = [f"lattice {result['lattice']} sets over {result['fuzzy']} fuzzy functions in {result['wall_s']} s"]
    lines += [f"options {r['module']} {' '.join(r['flags'])} (for {r['for']})" for r in result["options"]]
    lines += [f"split {r['function']} into {r['module']} with {' '.join(r['flags'])}" for r in result["splits"]]
    lines += [f"cut {r['left']}|{r['right']} decided by {' '.join(r['by'])}" for r in result["decided_cuts"]]
    return lines


_RENDER: dict[str, Callable[[Json], list[str]]] = {
    "next": _render_next,
    "work": _render_work,
    "try": _render_try,
    "skip": lambda result: [f"skipped {result['item']} as {result['reason']}"],
    "look": _render_look,
    "status": _render_status,
    "structure": _render_structure,
    "apply": _render_apply,
    "flags": _render_flags,
    "name": _render_name,
    "style": _render_style,
    "text": lambda result: [
        f"{'retyped' if result['written'] else 'would retype'} {len(result['items'])} text arrays"
        f" in {len(result['files'])} files",
        *(f"{i['symbol']} in {i['file']}: {i['size']} bytes" for i in result["items"]),
        f"{'pointed' if result['written'] else 'would point'} {len(result['pointers'])} code words at their"
        f" functions ({result['rows']} alias rows)",
        *(f"{p['symbol']}[{p['word']}] in {p['file']}: &{p['alias']} ({p['function']})" for p in result["pointers"]),
        *(f"kept {k['symbol']}: {k['why']}" for k in result["kept"]),
    ],
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
    lines = errors.render(finding)
    return [*lines[:-1], f"next: {_action(finding.action, item)}"]


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
        return one

    one = verb("next", "lease the best open item")
    one.add_argument("--kind", choices=("function", "data", "declaration"))
    one.add_argument("--twin", choices=("yes", "no"), help="only items with (yes) or without (no) a landed twin")
    one.add_argument("--max-size", type=int, help="largest item in bytes")
    one.add_argument("--order", choices=("small", "large"), help="smallest or largest first (default: largest)")
    one = verb("work", "run the tool rungs on an item; prints once at exit, and a large item can take minutes")
    one.add_argument("item")
    one.add_argument("--kind", choices=("declaration",), help="work the declaration item of this name")
    one.add_argument("--no-submit", action="store_true")
    one = verb("try", "prove the candidate file")
    one.add_argument("item")
    one = verb("look", "show a prototype, type, field or global")
    one.add_argument("name")
    one = verb("skip", "release an item with a reason")
    one.add_argument("item")
    one.add_argument("--reason", required=True, choices=state.SKIP_REASONS)
    one.add_argument("--note", default="")
    one = verb("structure", "propose module structure: one JSON object {patch, files, moves} on stdout")
    one.add_argument("--segment", help="only these segments (comma list)")
    one.add_argument("--paths", nargs="+", metavar="FILE", help="only the modules holding these files and linked moves")
    one = verb("flags", "one flag set per object: score the flag lattice, propose options moves and flag splits")
    one.add_argument("--segment", help="only fuzzy functions and option units of these segments (comma list)")
    one = verb("name", "name things from evidence: next (pack or tag-named subsystem moves), submit FILE, banners")
    one.add_argument("action", choices=("next", "submit", "banners"))
    one.add_argument("file", nargs="?", help="the names file for submit")
    one.add_argument(
        "--step",
        choices=("N1", "N2", "N3", "N4", "T"),
        help="the naming step (default: N3); T: original types from the debug catalog",
    )
    one.add_argument("--count", type=int, default=40, help="targets per pack")
    one.add_argument("--target", help="pack this one function, datum or folder only (the one already chosen)")
    one = verb("style", "style findings, or with --census the count per rule against the saved baseline")
    one.add_argument("paths", nargs="*", help="check only these paths")
    one.add_argument(
        "--census", action="store_true", help="count per rule; exit 1 when a count rose above the baseline"
    )
    one.add_argument("--baseline", action="store_true", help="write the counts as the new baseline")
    one = verb("apply", "git apply the oldest saved patch, then unbake sync")
    one.add_argument("patch", nargs="?", help="a patch file (default: the oldest saved one)")
    one = verb("text", "retype integer arrays: text as char arrays, KUSEG code words as alias references")
    one.add_argument("--write", action="store_true", help="edit the unit sources (default: only list)")
    one.add_argument("--only", help="comma list of unit source paths")
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
    began = time.monotonic()
    host = args.host or (Path(os.environ["UNBAKE_HOST"]) if os.environ.get("UNBAKE_HOST") else None)
    item = getattr(args, "item", None)
    result: dict[str, Any] | None = None
    code, findings, landed = 0, [], None
    loaded: Config | None = None
    try:
        with lifecycle.bound():
            if host is None:
                raise Refusal(Finding("config.missing", "no host config: pass --host or set UNBAKE_HOST"))
            os.environ["UNBAKE_HOST"] = str(host.resolve())  # every unbake command this one runs reads the same host
            loaded = api.load(args.project or Path.cwd(), host)
            if args.verb == "tui":
                return tui.run(loaded)
            given = {k: v for k, v in vars(args).items() if k not in ("host", "project")}
            if args.verb in ("structure", "apply", "name", "flags"):  # these verbs call unbake
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
    if result is not None:
        result["wall_s"] = round(time.monotonic() - began)
        stream = result.pop("stream", "")
        if stream and not args.json:  # the patch owns stdout so that `recipe structure | jq -r .patch` works
            sys.stdout.write(stream)
            sys.stderr.write(render(args.verb, result) + "\n")
        else:
            _emit(render(args.verb, result), {"ok": True, **result}, args.json)
    else:
        first = []
        if landed is not None:
            first = [f"landed ({landed.landed})"] if landed.landed else []
            first += [f"saved as nonmatching ({landed.saved})"] if landed.saved else []
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
