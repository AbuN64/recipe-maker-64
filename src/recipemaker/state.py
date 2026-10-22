"""Leases, budgets, tries, skips and feedback under `.unbake/recipe/`, plus the attempt record."""

from __future__ import annotations

import json
import os
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
from unbake.contracts import Config, Finding, Json, Refusal

from recipemaker import host

SKIP_REASONS = ("m2c-failed", "split-symbol", "type-unresolved", "jump-table", "needs-header-change", "budget", "other")


@dataclass(frozen=True)
class Attempt:
    member: str
    step: str
    score: float
    best_before: float
    outcome: str  # "exact" | "better" | "same" | "worse" | "failed"
    symptoms: Json
    note: str
    time: str
    text: str = ""  # the candidate file as measured, so a later session can resume the best one
    stamp: str = ""  # the recipe code and function assembly it was made from; a resume skips any other stamp


@dataclass(frozen=True)
class Settings:
    stall_tries: int
    idle_minutes: float
    permute_seconds: float
    skip_days: int


@dataclass(frozen=True)
class Structure:
    batch_moves: int = 400
    dir_max: int = 32


@dataclass(frozen=True)
class Lease:
    item: str
    session: str
    started: float
    expires: float
    tries: int


def stop(key: str, reason: str, **more: Any) -> Refusal:
    return Refusal(Finding(key, reason, **more))


def _object(**properties: object) -> dict[str, object]:
    return {"type": "object", "additionalProperties": False, "properties": properties}


_SCHEMA = _object(
    budget=_object(
        stall_tries={"type": "integer", "minimum": 1},
        idle_minutes={"type": "number", "exclusiveMinimum": 0},
        permute_seconds={"type": "number", "exclusiveMinimum": 0},
        skip_days={"type": "integer", "minimum": 0},
    ),
    structure=_object(**{k: {"type": "number", "exclusiveMinimum": 0} for k in Structure.__dataclass_fields__}),
    tools=_object(m2c={"type": "string", "minLength": 1}, permuter={"type": "string", "minLength": 1}),
    inputs=_object(debug_types={"type": "string", "minLength": 1}),
)


def _where(path: Path | None) -> Path:
    return path or Path(os.environ.get("RECIPE_CONFIG") or Path.home() / ".config" / "recipemaker" / "recipe.toml")


def _document(path: Path | None) -> Json:
    where = _where(path)
    if not where.is_file():
        raise stop("recipe.config", f"{where} does not exist", path=str(where), action=f"create {where}")
    try:
        document = tomllib.loads(where.read_text(encoding="utf-8"))
        jsonschema.validate(document, _SCHEMA)
    except (tomllib.TOMLDecodeError, jsonschema.ValidationError) as error:
        raise stop(
            "recipe.config", f"{where} is not valid: {getattr(error, 'message', error)}", path=str(where)
        ) from error
    return document


def settings(path: Path | None = None) -> Settings:
    """Read the recipe config (`RECIPE_CONFIG`, else the default path); a missing file or key is refused by name."""
    document = _document(path)

    def need(*keys: str) -> Any:
        node = document
        for key in keys:
            if key not in node:
                where, dotted = _where(path), ".".join(keys)
                raise stop("recipe.config", f"{where.name}:{dotted} is missing from {where}", path=str(where))
            node = node[key]
        return node

    return Settings(
        stall_tries=int(need("budget", "stall_tries")),
        idle_minutes=float(need("budget", "idle_minutes")),
        permute_seconds=float(need("budget", "permute_seconds")),
        skip_days=int(need("budget", "skip_days")),
    )


def structure(path: Path | None = None) -> Structure:
    return Structure(**_document(path).get("structure", {}))


def tool(name: str, path: Path | None = None) -> Path:
    """The path of an external tool (`m2c`, `permuter`) from the `[tools]` table of the recipe config."""
    found = _document(path).get("tools", {}).get(name)
    if not found:
        where, action = _where(path), f'add {name} = "/path/to/{name}" under [tools]'
        raise stop("recipe.tool", f"no path for {name} in the [tools] table of {where}", path=str(where), action=action)
    return Path(found)


def inputs(path: Path | None = None) -> dict[str, str]:
    """The `[inputs]` table of the recipe config: project evidence files (`debug_types`), absolute or relative to the
    project root. A missing config or table gives none."""
    return dict(_document(path).get("inputs", {})) if _where(path).is_file() else {}


def session() -> str:
    """`RECIPE_SESSION`, else the parent process id."""
    return os.environ.get("RECIPE_SESSION") or str(os.getppid())


def _dead(owner: str) -> bool:
    """True when `owner` is a process id that no longer exists on this host; named sessions are never dead."""
    try:
        os.kill(int(owner), 0)
    except ProcessLookupError:
        return True
    except (ValueError, OSError):
        return False
    return False


def _held(current: Lease, now: float) -> str:
    return f"live, held {max(0, round((now - current.started) / 60))} min"


def _root(config: Config) -> Path:
    return config.project.root / ".unbake" / "recipe"


def _path(config: Config, item: str) -> Path:
    return _root(config) / "leases" / f"{host.stem(item)}.json"


def _load(path: Path) -> Lease | None:
    try:
        return Lease(**json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError, TypeError):
        return None


def _write(config: Config, lease: Lease) -> None:
    host.write(_path(config, lease.item), json.dumps(asdict(lease), sort_keys=True).encode())


def lease(config: Config, item: str) -> Lease | None:
    return _load(_path(config, item))


def lost(item: str, current: Lease | None, now: float) -> Refusal:
    """Why `item` is not leased to this session: to whom and since when, or that it expired."""
    if current is None or not current.session:
        reason = f"{item} is not leased to this session (no live lease)"
    elif current.expires <= now:
        reason = f"{item} was leased to session {current.session} and expired at {stamp(current.expires)}"
    else:
        since = f"{stamp(current.started)} ({_held(current, now)})"
        reason = f"{item} has been leased to session {current.session} since {since}"
    return stop("recipe.not-leased", reason, unit=item, action="recipe next")


def _expiry(now: float) -> float:
    return now + settings().idle_minutes * 60


def minutes_left(current: Lease, now: float) -> float:
    return round((current.expires - now) / 60, 1)


def worst(score: Json) -> float:
    """The lowest per-version score, 0 when there is none."""
    return min(score.values(), default=0.0)


def take(config: Config, item: str, now: float) -> Lease:
    """Lease `item` to this session; a lease another session holds live is never granted, and this session's own
    expired lease is taken again with its stall count."""
    with host.exclusive(config, "recipe", wait=True):
        current = lease(config, item)
        live = current is not None and current.expires > now
        if current is not None and live and current.session != session() and _dead(current.session):
            current = replace(current, session=session())
        if current is not None and live and current.session != session():
            raise Refusal(
                Finding(
                    "recipe.leased",
                    f"{item} is leased to session {current.session} ({_held(current, now)}) until "
                    f"{stamp(current.expires)}",
                    unit=item,
                    action="recipe next",
                )
            )
        expires = _expiry(now)
        granted = (
            replace(current, expires=expires)
            if current and (live or current.session == session())
            else Lease(item, session(), now, expires, 0)
        )
        _write(config, granted)
        return granted


def held(config: Config, item: str, now: float) -> Lease:
    current = lease(config, item)
    if current is not None and current.expires > now and current.session != session() and _dead(current.session):
        current = replace(current, session=session())  # no lock here: callers may already hold it
        _write(config, current)
    if current is None or current.expires <= now or current.session != session():
        raise lost(item, current, now)
    return current


def leases(config: Config, now: float) -> list[Lease]:
    folder = _root(config) / "leases"
    found = (_load(folder / name) for name in sorted(host.listing(config, "recipe/leases")))
    return [x for x in found if x is not None and x.expires > now]


def _extend(config: Config, item: str, now: float, progressed: bool | None) -> Lease:
    """Renew the held lease; `progressed` None leaves the stall count, else a gain refunds it and none counts a try."""
    with host.exclusive(config, "recipe", wait=True):
        current = held(config, item, now)
        tries = current.tries if progressed is None else 0 if progressed else current.tries + 1
        renewed = replace(current, tries=tries, expires=_expiry(now))
        _write(config, renewed)
        return renewed


def count_try(config: Config, item: str, now: float, progressed: bool) -> Lease:
    """A try that raised the score refunds the lease; any other try counts toward the stall budget."""
    return _extend(config, item, now, progressed)


def renew(config: Config, item: str, now: float) -> Lease:
    """A try that was exact but refused by a declaration or rule check costs no stall try."""
    return _extend(config, item, now, None)


def release(config: Config, item: str) -> None:
    with host.exclusive(config, "recipe", wait=True):
        current = lease(config, item)
        if current is not None:
            _write(config, Lease(item, "", current.started, 0.0, 0))


def percent(value: float) -> str:
    return f"{int(round(value * 1000, 6)) / 10:.1f}%"


def stamp(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")


def skip(
    config: Config,
    item: str,
    reason: str,
    note: str,
    best: float,
    now: float,
    deps: str = "",
    blockers: Json | None = None,
) -> None:
    if reason not in SKIP_REASONS:
        action = "reasons: " + " ".join(SKIP_REASONS)
        raise Refusal(Finding("recipe.reason", f"{reason} is not a skip reason", action=action))
    held_now = lease(config, item)
    row = {"time": now, "session": session(), "item": item, "reason": reason, "note": note, "best_score": best}
    host.append(
        config,
        "recipe/skips",
        {**row, "tries": held_now.tries if held_now else 0, "deps": deps, "blockers": blockers or {}},
    )
    release(config, item)


def _latest_skips(config: Config) -> dict[str, Json]:
    return {row["item"]: row for row in host.rows(config, "recipe/skips")}


def skipped(config: Config, now: float, deps: Mapping[str, str] | None = None) -> frozenset[str]:
    limit = settings().skip_days * 86400
    latest = _latest_skips(config)
    return frozenset(
        item
        for item, row in latest.items()
        if now - row["time"] < limit and (deps is None or deps.get(item, row["deps"]) == row["deps"])
    )


def blocked(config: Config) -> dict[str, dict[str, str]]:
    """Per skipped item, the symbols that blocked it with the declaration each had when it was skipped."""
    return {item: row["blockers"] for item, row in _latest_skips(config).items() if row.get("blockers")}


def unblocked(config: Config, declared: Mapping[str, str]) -> frozenset[str]:
    """The skipped items one of whose blocking symbols is now declared differently."""
    return frozenset(
        item for item, held in blocked(config).items() if any(declared.get(n, "") != t for n, t in held.items())
    )


def attempts(config: Config, member: str) -> list[Attempt]:
    rows = host.rows(config, f"recipe-attempts/{host.stem(member)}")
    return [Attempt(**{k: v for k, v in row.items() if k in Attempt.__dataclass_fields__}) for row in rows]


def record(config: Config, item: str, now: float, word: str, score: Json, **more: Any) -> None:
    empty = {"error_class": "", "autofix": "", "name": "", "auto_fixes": [], "regions": []}
    row = {"time": now, "session": session(), "item": item, "result": word, "score": score, **empty, **more}
    host.append(config, "recipe/tries", row)


def status(config: Config, now: float) -> Json:
    today = stamp(now)[:10]
    tries = [r for r in host.rows(config, "recipe/tries") if stamp(r["time"])[:10] == today]
    skips = host.rows(config, "recipe/skips")
    done = Counter(r["result"] for r in tries)
    exact = done["exact"]
    live = [
        {"session": x.session, "item": x.item, "tries": x.tries, "minutes_left": minutes_left(x, now)}
        for x in leases(config, now)
    ]
    return {
        "sessions": live,
        "today": {
            "exact": exact,
            "fuzzy": done["fuzzy"],
            "skipped": sum(stamp(r["time"])[:10] == today for r in skips),
            "tries": len(tries),
        },
        "skip_reasons": dict(Counter(r["reason"] for r in skips)),
        "error_classes": dict(Counter(r["error_class"] for r in tries if r.get("error_class"))),
    }
