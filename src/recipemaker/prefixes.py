"""The one prefix authority: a function's prefix is the `prefix` column of the project's module map (the TSV that
config.toml names with `module_map`), read for the unit, module or header path of the file that holds the function.
`recipe style` (the audit) and `recipe name submit` (the gate) both ask here, so they cannot disagree. A path the map
does not list, or a project with no map, keeps the folder rule of `style.prefix_word`."""

import csv
from pathlib import Path

from unbake.contracts import Config

from recipemaker import state

_CACHE: dict[Path, tuple[tuple[int, int], dict[str, str]]] = {}


def table(config: Config) -> dict[str, str]:
    """Path -> prefix for every unit, module and header the map lists ({} when the project names no map). A unit's own
    row wins over the module or header path another row spells the same."""
    spelled = getattr(config.project, "module_map", None)
    if not spelled:
        return {}
    path = Path(config.project.root) / spelled
    try:
        seen = path.stat()
    except OSError:
        raise state.stop(
            "recipe.prefix",
            f"config.toml module_map names {spelled}, which does not exist in this tree",
            action=f"restore {spelled} (the module map) or remove module_map from config.toml",
        ) from None
    stamp = (seen.st_mtime_ns, seen.st_size)
    if (held := _CACHE.get(path)) and held[0] == stamp:
        return held[1]
    units: dict[str, str] = {}
    shared: dict[str, str] = {}
    with open(path, encoding="utf-8", newline="") as handle:
        lines = (line for line in handle if line.strip() and not line.startswith("#"))
        for row in csv.DictReader(lines, delimiter="\t"):
            prefix = (row.get("prefix") or "").strip()
            if not prefix or prefix == "-":
                continue
            if row.get("unit"):
                units.setdefault(row["unit"], prefix)
            for key in ("module", "header"):
                if row.get(key):
                    shared.setdefault(row[key], prefix)
    found = {**shared, **units}
    _CACHE[path] = (stamp, found)
    return found
