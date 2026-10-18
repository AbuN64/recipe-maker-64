"""`recipe name`: evidence packs for an outside naming pass, then rows validated against the style and the map, turned
into one file patch (symbol, module, source and header renames) that `recipe apply` applies. Catalog names (N0) and
tag-named subsystems (N2) need no model. No name is accepted without evidence a reader can check in map.json."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from unbake import api
from unbake.contracts import Config, Json

from recipemaker import state, structure, style, table

_ORDER = {"subsystem": 0, "module": 1, "function": 2, "global": 2, "static": 2, "type": 2}
_EVIDENCE = re.compile(r"^(callee|caller|tag) (\S+)$|^string '(.*)'$", re.S)
_CARD_LIMIT = 12


def _root(config: Config) -> Path:
    return config.project.root / ".unbake/recipe/name"


def _bytes(member: Json, version: str) -> int:
    return next((p["rom_end"] - p["rom_start"] for p in member["placements"] if p["version"] == version), 0)


class Map:
    """The map document as the name rules read it: members, modules, folders and callers by current name."""

    def __init__(self, config: Config, doc: Json) -> None:
        self.root, self.doc = config.project.root, doc
        self.members: dict[str, Json] = {m["name"]: m for m in doc["members"]}
        self.modules: dict[str, dict[str, Any]] = {m["name"]: dict(m) for m in doc["modules"]}
        self.callers: dict[str, list[str]] = {}
        for m in doc["members"]:
            for target in (*m["calls"], *m["loads"]):
                self.callers.setdefault(target, []).append(m["name"])
        self._gbi: dict[str, bool] = {}
        self.alias: dict[str, str] = {}  # a module renamed by an accepted row: old name to new name
        self.taken: set[str] = {*self.members, *self.modules}
        for module in self.modules.values():
            self.taken |= {PurePosixPath(module[k]).stem for k in ("source", "header")} | {self.folder(module["name"])}

    def folder(self, module: str) -> str:
        module = self.alias.get(module, module)
        return PurePosixPath(self.modules[module]["source"]).parent.name if module in self.modules else ""

    def folder_of(self, member: str) -> str:
        return self.folder(self.members[member]["module"]) if member in self.members else ""

    def word(self, folder: str) -> str:
        """The subsystem word of a folder, or an empty string when it is neutral or no subsystem folder."""
        rules = table("knowledge/style.toml")
        ok = re.fullmatch(rules["names"]["subsystem"], folder) and folder not in rules["paths"]["no_prefix"]
        return folder if ok and not style.neutral(folder) else ""

    def functions(self, folder: str) -> list[str]:
        return [
            m["name"] for m in self.members.values() if m["kind"] == "function" and self.folder_of(m["name"]) == folder
        ]

    def gbi(self, member: str) -> bool:
        unit = self.members[member]["unit"]
        if unit and unit not in self._gbi:
            try:
                text = (self.root / unit).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            self._gbi[unit] = bool(re.search(table("knowledge/style.toml")["gbi"]["use"], text))
        return bool(unit and self._gbi[unit])

    def tag_of(self, callee: str) -> str:
        """The subsystem word an SDK callee name gives: its prefix followed by an upper-case letter or underscore."""
        name = self.members.get(callee, {}).get("sdk_name") or callee
        for prefix, word in table("knowledge/style.toml")["tags"].items():
            if re.match(re.escape(prefix) + r"(?:[A-Z_0-9]|$)", name):
                return str(word)
        return ""

    def tags(self, names: Iterable[str]) -> Counter[str]:
        found: Counter[str] = Counter()
        for name in names:
            found.update(t for t in (self.tag_of(c) for c in self.members[name]["calls"]) if t)
            found.update([table("knowledge/style.toml")["gbi"]["tag"]] * self.gbi(name))
        return found

    def users(self, target: str, kind: str) -> list[str]:
        """The members whose evidence speaks for a target: itself, a folder's or module's functions, a type's users."""
        if kind in ("function", "global", "static"):
            return [target] * (target in self.members)
        if kind == "subsystem":
            return [m for mod in self.modules if self.folder(mod) == target for m in self.modules[mod]["members"]]
        if kind == "module":
            return list(self.modules.get(target, {}).get("members", ()))
        headers = [h for h in self.doc["headers"] if target in h["declares"]]
        mods = {m for h in headers for m in h["modules"]}
        units = {u["path"]: u["members"] for u in self.doc["units"]}
        return sorted(
            {m for h in headers for p in h["included_by"] for m in units.get(p, ())}
            | {m for mod in mods for m in self.modules[mod]["members"]}
        )

    def verifies(self, item: str, names: Sequence[str]) -> bool:
        found = _EVIDENCE.match(item)
        if not found:
            return False
        if found[3] is not None:
            return any(found[3] in s for n in names for s in self.members[n]["strings"])
        kind, value = found[1], found[2]
        if kind == "tag":
            return self.tags(names)[value] > 0
        if kind == "caller":
            return any(value in self.callers.get(n, ()) for n in names)
        sdk = {self.members[c].get("sdk_name") for n in names for c in self.members[n]["calls"] if c in self.members}
        return any(value in self.members[n]["calls"] for n in names) or value in sdk


def _exists(ctx: Map, row: Json) -> str:
    kind, target = row["kind"], row["target"]
    if kind == "subsystem":
        return "" if any(ctx.folder(m) == target for m in ctx.modules) else "no folder has this name"
    if kind == "module":
        return "" if target in ctx.modules else "no module has this name"
    if kind == "type":
        return "" if ctx.users(target, kind) else "no header declares this type"
    want = "function" if kind == "function" else "data"
    found = ctx.members.get(target)
    return "" if found and (found["kind"] == "function") == (want == "function") else f"no {want} has this name"


def _form(ctx: Map, row: Json) -> tuple[str, str]:
    """Whether the new name has the shape its kind and subsystem ask for, and holds no banned word."""
    kind, target, name = row["kind"], row["target"], row["name"]
    word = ""
    if kind == "function":
        word = ctx.word(ctx.folder_of(target))
        if not word:
            return "prefix", f"the subsystem folder {ctx.folder_of(target)} has no name yet"
    elif kind == "module":
        word = ctx.word(ctx.folder(target))
        if not word:
            return "prefix", f"the subsystem folder {ctx.folder(target)} has no name yet"
    if not style.legal(kind, name, word) or style.neutral(name):
        return "regex", f"{name} does not match {style.pattern(kind, word)}"
    if kind == "module" and style.collision(name):
        return "regex", f"{name} ends in a collision counter"
    if found := style.banned(name, kind):
        return "banned", f"{found} is a banned word"
    return "", ""


def judge(ctx: Map, row: Json) -> tuple[str, str]:
    """The first rule the row breaks as (rule, reason), or ("", "")."""
    kind, target, name = row.get("kind"), row.get("target"), row.get("name")
    if kind not in _ORDER or not isinstance(target, str) or not isinstance(name, str) or not row.get("doc"):
        return "shape", "a row needs kind, target, name, evidence and doc"
    if reason := _exists(ctx, row):
        return "target", reason
    if kind != "type" and not style.neutral(target):
        return "named", f"{target} already has a name"
    if kind == "function" and ctx.members[target].get("sdk_name"):
        return "catalog", f"{target} is an SDK function: its name is the catalog name"
    rule, reason = _form(ctx, row)
    if rule:
        return rule, reason
    if name in ctx.taken:
        return "duplicate", f"{name} is already a symbol, folder or file name"
    evidence = row.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(isinstance(e, str) for e in evidence):
        return "evidence", "a name needs at least one evidence item"
    users = ctx.users(target, kind)
    if missing := [e for e in evidence if not ctx.verifies(e, users)]:
        return "unverifiable", f"map.json does not show: {missing[0]}"
    return "", ""


def _moves(ctx: Map, row: Json) -> list[dict[str, str]]:
    """The moves of an accepted row, with the map's paths and names brought forward for the rows after it."""
    kind, target, name = row["kind"], row["target"], row["name"]
    ctx.taken.add(name)
    if kind == "type":  # a struct, union or typedef: renamed, or folded into the type that holds the name
        for header in ctx.doc["headers"]:
            header["declares"] = [name if d == target else d for d in header["declares"]]
        return [{"op": "type", "subject": target, "target": name}]
    if kind not in ("subsystem", "module"):
        return [{"op": "symbol", "subject": target, "target": name}]
    out: list[dict[str, str]] = []
    if kind == "module":
        module = ctx.modules.pop(target)
        ctx.modules[name] = module
        module["name"] = name
        ctx.alias[target] = name
        out.append({"op": "module", "subject": target, "target": name})
        for op in ("source", "header"):
            path = PurePosixPath(module[op])
            module[op] = str(path.with_name(name + path.suffix))
            out.append({"op": op, "subject": name, "target": module[op]})
        return out
    for module in ctx.modules.values():
        if ctx.folder(module["name"]) != target:
            continue
        for op in ("source", "header"):
            parts = [name if p == target else p for p in PurePosixPath(module[op]).parts]
            if parts != list(PurePosixPath(module[op]).parts):
                module[op] = str(PurePosixPath(*parts))
                out.append({"op": op, "subject": module["name"], "target": module[op]})
    return out


def _ledger(config: Config, name: str) -> str:
    path = _root(config) / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _patch(config: Config, doc: Json, moves: Sequence[Json]) -> Json:
    """The moves as a saved patch: `{"patch", "files", "moves", "path"}`, the patch built on the files as they are."""
    captured = api.capture(config)
    built = dict(
        structure.build(
            config.project.root,
            moves,
            lambda name: captured.owner(doc["names_from"], name),
            structure.modules_of(doc),
        )
    )
    return {**built, "path": structure.save(config, built) if built["moves"] else ""}


def _number(config: Config) -> int:
    """The next pack or names number: one past every pack and names file there is."""
    found = [int(m[1]) for p in _root(config).glob("*-*") if (m := re.match(r"(?:pack|names|banners)-(\d+)\.", p.name))]
    return max(found, default=0) + 1


def _card(ctx: Map, kind: str, target: str, version: str) -> Json:
    names = ctx.users(target, kind)
    members = [ctx.members[n] for n in names]
    calls = Counter(c for m in members for c in m["calls"] if c not in names)
    shown = [ctx.members[c].get("sdk_name") or c if c in ctx.members else c for c, _ in calls.most_common(_CARD_LIMIT)]
    card: dict[str, Any] = {
        "target": target,
        "kind": kind,
        "size": sum(_bytes(m, version) for m in members),
        "callees": shown,
        "sdk_callees": sorted({ctx.members[c]["sdk_name"] for c in calls if ctx.members.get(c, {}).get("sdk_name")}),
        "strings": sorted({s for m in members for s in m["strings"]})[:_CARD_LIMIT],
        "tags": dict(ctx.tags(names)),
        "loads": sorted({d for m in members for d in m["loads"]})[:_CARD_LIMIT],
    }
    outside = Counter(c for n in names for c in ctx.callers.get(n, ()) if c not in names)
    card["callers"] = [c for c, _ in outside.most_common(_CARD_LIMIT)]
    card["fan_in"] = sum(outside.values())
    if kind in ("function", "global", "static"):
        card["file"] = ctx.modules.get(ctx.members[target]["module"], {}).get("source", "")
        card["subsystem"] = ctx.folder_of(target)
    else:
        card["human_names"] = [n for n in names if not style.neutral(n)][:_CARD_LIMIT]
    return card


def _targets(ctx: Map, step: str) -> list[tuple[str, str]]:
    """The (kind, target) pairs an LLM step names, the most useful first."""
    neutral = {n: m for n, m in ctx.members.items() if style.neutral(n)}
    if step == "N2":
        folders = {f for m in ctx.modules if (f := ctx.folder(m)).startswith("sys_") and style.neutral(f)}
        return [("subsystem", f) for f in sorted(folders)]
    if step == "N1":  # leaf numeric functions many subsystems call
        leaf = [n for n, m in neutral.items() if m["kind"] == "function" and not m["calls"]]
        spread = {n: {ctx.folder_of(c) for c in ctx.callers.get(n, ())} for n in leaf}
        return [("function", n) for n in sorted(leaf) if len(spread[n]) >= 5]
    if step == "N3":  # public API: called from another subsystem, by fan-in
        rows = [
            (len({c for c in ctx.callers.get(n, ()) if ctx.folder_of(c) != ctx.folder_of(n)}), n)
            for n, m in neutral.items()
            if m["kind"] == "function" and ctx.word(ctx.folder_of(n))
        ]
        return [("function", n) for fan, n in sorted(rows, key=lambda r: (-r[0], r[1])) if fan]
    data = {n: ctx.callers.get(n, []) for n, m in neutral.items() if m["kind"] != "function"}
    held = [(n, "global" if len({ctx.folder_of(c) for c in by}) >= 2 else "static") for n, by in data.items() if by]
    return [(k, n) for n, k in sorted(held, key=lambda r: (-len(data[r[0]]), r[0]))]


def _auto(ctx: Map) -> list[Json]:
    """N2 rows for the folders whose external calls are at least half one tag; a tag word names one folder only."""
    out: list[Json] = []
    claimed: set[str] = set()
    for _, folder in _targets(ctx, "N2"):
        names = [n for n in ctx.users(folder, "subsystem") if n in ctx.members]
        calls = [c for n in names for c in ctx.members[n]["calls"] if ctx.folder_of(c) != folder]
        tags, external = ctx.tags(names), len(calls) + sum(ctx.gbi(n) for n in names)
        if not tags or not external:
            continue
        word, held = tags.most_common(1)[0]
        if held / external >= 0.5 and word not in ctx.taken | claimed:
            claimed.add(word)
            seen = next((c for c in calls if ctx.tag_of(c) == word), "")
            evidence = [f"tag {word}", *([f"callee {seen}"] if seen else [])]
            out.append(
                {"target": folder, "kind": "subsystem", "name": word, "doc": f"the {word} code", "evidence": evidence}
            )
    return out


def next_(config: Config, params: Json) -> Json:
    """`recipe name next --step`: N0 and tag-named N2 become a batch of moves at once, the rest an evidence pack."""
    step, doc = params["step"] or "N3", structure.evidence(config, params["host"])
    ctx, moves, pick, path = Map(config, doc), [], [], None
    if step == "N0":
        claims: dict[str, list[str]] = {}  # catalog name -> the neutral members the catalog gives it to
        for n, m in sorted(ctx.members.items()):
            if m.get("sdk_name") and m["sdk_name"] not in ctx.taken and style.neutral(n):
                claims.setdefault(m["sdk_name"], []).append(n)
        # one name, one function: a name two members claim stays neutral on both and is recorded in the ledger
        moves = [{"op": "symbol", "subject": n[0], "target": t} for t, n in claims.items() if len(n) == 1]
        twice = [{"name": t, "members": n} for t, n in sorted(claims.items()) if len(n) > 1]
        structure.write(_root(config) / "n0-ledger.jsonl", "".join(json.dumps(r) + "\n" for r in twice))
    else:
        done: set[str] = set()
        for row in _auto(ctx) if step == "N2" else []:
            if not judge(ctx, row)[0]:
                moves, done = [*moves, *_moves(ctx, row)], done | {row["target"]}
        pick = [t for t in _targets(Map(config, doc), step) if t[1] not in done][: int(params["count"])]
    answer = ""
    if pick:
        k, rules = _number(config), table("knowledge/style.toml")
        path, answer = _root(config) / f"pack-{k}.json", f".unbake/recipe/name/names-{k}.jsonl"
        cards = [_card(ctx, kind, t, doc["names_from"]) for kind, t in pick]
        pack = {"step": step, "k": k, "targets": cards, "answer": answer}
        pack |= {x: rules[x] for x in ("names", "banned", "evidence_forms")}
        structure.write(path, json.dumps(pack))
    built = _patch(config, doc, moves) if moves else {"patch": "", "files": [], "moves": 0, "path": ""}
    return {
        "action": "next",
        "step": step,
        "pack": str(path.relative_to(config.project.root)) if path else None,
        "targets": len(pick),
        **{k: built[k] for k in ("patch", "files", "moves", "path")},
        "stream": structure.stream(built) if built["moves"] else "",
        "next": "recipe apply" if built["moves"] else f"recipe name submit {answer}" if pick else "recipe status",
        "after": f"recipe name submit {answer}" if pick else "",
    }


def submit(config: Config, params: Json) -> Json:
    """`recipe name submit FILE`: validate every row, append the accepted ones as moves and write their banners."""
    path = Path(params["file"])
    if not path.is_file():
        raise state.stop("recipe.name", f"{path} does not exist", action="recipe name next")
    doc = structure.evidence(config, params["host"])
    ctx = Map(config, doc)
    rows: list[Json] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            row = {"bad": line}
        rows += [row if isinstance(row, dict) else {"bad": line}] if row is not None else []
    k = m[1] if (m := re.search(r"names-(\d+)", path.name)) else str(_number(config))
    accepted, refused = [], []
    for row in sorted(rows, key=lambda r: _ORDER.get(str(r.get("kind")), 9)):
        rule, reason = judge(ctx, row) if "bad" not in row else ("shape", "not a JSON object")
        if rule:
            refused.append({"row": row, "rule": rule, "reason": reason})
            continue
        accepted.append((row, _moves(ctx, row)))
    moves = [m for _, group in accepted for m in group]
    built = _patch(config, doc, moves) if moves else {"patch": "", "files": [], "moves": 0, "path": ""}
    banners = [{"name": r["name"], "kind": r["kind"], "doc": " ".join(r["doc"].split())} for r, _ in accepted]
    structure.write(_root(config) / f"banners-{k}.json", json.dumps(banners))
    old = _ledger(config, "refused.jsonl")
    structure.write(
        _root(config) / "refused.jsonl", old + "".join(json.dumps(r, sort_keys=True) + "\n" for r in refused)
    )
    return {
        "action": "submit",
        "rows": len(rows),
        "accepted": len(accepted),
        "refused": len(refused),
        "by_rule": dict(Counter(r["rule"] for r in refused)),
        **{k_: built[k_] for k_ in ("patch", "files", "moves", "path")},
        "stream": structure.stream(built) if built["moves"] else "",
        "banners": f".unbake/recipe/name/banners-{k}.json",
        "next": "recipe apply" if built["moves"] else "recipe name next",
    }


def _banner(name: str, doc: str, vram: str, version: str) -> str:
    where = [f"{version} {vram}"] if vram else []
    return "\n".join(["/*", "=" * 16, name, "", doc, *where, "=" * 16, "*/", ""])


def banners(config: Config, params: Json) -> Json:
    """`recipe name banners`: candidate sources with the id banner above each renamed function, for `submit --bulk`."""
    doc = structure.evidence(config, params["host"])
    ctx, version = Map(config, doc), doc["names_from"]
    wanted = [
        b for p in sorted(_root(config).glob("banners-*.json")) for b in json.loads(p.read_text(encoding="utf-8"))
    ]
    by_unit: dict[str, list[Json]] = {}
    for item in wanted:
        member = ctx.members.get(item["name"])
        if item["kind"] == "function" and member and member["unit"]:
            by_unit.setdefault(member["unit"], []).append(item)
    out, skipped = [], 0
    for unit, items in sorted(by_unit.items()):
        text = (ctx.root / unit).read_text(encoding="utf-8")
        for item in items:
            name = item["name"]
            start = re.search(
                rf"^[ \t]*(?:static[ \t]+)?[\w \t*]*?\b{re.escape(name)}[ \t]*\([^;{{]*\)\s*\{{", text, re.M
            )
            if not start or re.search(rf"={{16}}\n{re.escape(name)}\n", text):
                skipped += 1
                continue
            vram = next((p["vram"] for p in ctx.members[name]["placements"] if p["version"] == version), None)
            line = _banner(name, item["doc"], f"{vram:08X}" if vram is not None else "", version)
            text = text[: start.start()] + line + text[start.start() :]
        target = _root(config) / "banners" / (unit.replace("/", "__"))
        structure.write(target, text)
        out.append(str(target.relative_to(config.project.root)))
    return {
        "action": "banners",
        "files": out,
        "skipped": skipped,
        "next": "unbake submit --bulk " + " ".join(out) if out else "recipe name next",
    }


def run(config: Config, params: Json) -> Json:
    return {"next": next_, "submit": submit, "banners": banners}[params["action"]](config, params)
