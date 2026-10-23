"""Candidate C for N64 decompilation. unbake proves and lands it; this package only proposes."""

import tomllib
from importlib import resources

from unbake import api
from unbake.contracts import Json


def resource(name: str) -> bytes:
    """The bytes of a data file shipped inside this package, such as `tables/gbi.toml`."""
    return resources.files("recipemaker").joinpath(name).read_bytes()


def table(name: str) -> Json:
    """A packaged TOML file, read once per command."""
    return api.memo(("recipe.table", name), lambda: tomllib.loads(resource(name).decode()))
