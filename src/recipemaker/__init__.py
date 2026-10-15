"""Candidate C for N64 decompilation. unbake proves and lands it; this package only proposes."""

from __future__ import annotations

import tomllib
from importlib import resources

from unbake import effort
from unbake.contracts import Finding, Json, Refusal


def resource(name: str) -> bytes:
    """The bytes of a data file shipped inside this package, such as `tables/gbi.toml`."""
    return resources.files("recipemaker").joinpath(name).read_bytes()


def table(name: str) -> Json:
    """A packaged TOML file, read once per command."""
    return effort.memo(("recipe.table", name), lambda: tomllib.loads(resource(name).decode()))


def advice(name: str) -> str:
    """A sentence of advice from the `[text]` table of knowledge/errors.toml."""
    return str(table("knowledge/errors.toml")["text"][name])


def m2c_target(toolchain: str) -> str:
    """The m2c target of the compiler card that lists `toolchain`."""
    for card in resources.files("recipemaker").joinpath("knowledge/compilers").iterdir():
        row = tomllib.loads(card.read_text(encoding="utf-8"))
        if toolchain in row["toolchains"]:
            return str(row["m2c_target"])
    raise Refusal(Finding("adapter.unknown", f"toolchain {toolchain} has no compiler card"))
