"""The recipe config (`RECIPE_CONFIG`, else ~/.config/recipemaker/recipe.toml): structure limits and evidence inputs."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from unbake.contracts import Finding, Json, Refusal


@dataclass(frozen=True)
class Structure:
    dir_max: int = 32


def stop(key: str, reason: str, **more: Any) -> Refusal:
    return Refusal(Finding(key, reason, **more))


def _object(**properties: object) -> dict[str, object]:
    return {"type": "object", "additionalProperties": False, "properties": properties}


# [budget] and [tools] stay accepted: a config written for the retired draft verbs still validates
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


def structure(path: Path | None = None) -> Structure:
    return Structure(**_document(path).get("structure", {}))


def inputs(path: Path | None = None) -> dict[str, str]:
    """The `[inputs]` table: project evidence files (`debug_types`), absolute or relative to the project root. A
    missing config or table gives none."""
    return dict(_document(path).get("inputs", {})) if _where(path).is_file() else {}
