"""The folder tree over proposed modules: segment labels, module identities and paths in the project's naming style.

Everything here is a pure function of the evidence document. The libraries, call-graph subsystems and data ownership
of the structure design (rules L, Y, D) need call and load edges, which unbake 0.5.5 produces nowhere, so a proposed
module stays in the folder of the module it comes from."""

import posixpath
import re
import statistics
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unbake.contracts import Json

Mod = dict[str, Any]


@dataclass(frozen=True)
class Naming:
    """Project naming/layout style: case substituted fields, preserving evidence and library rules."""

    case: str = "as-is"
    stem_max: int = 0
    source_ext: str = ".c"
    stem: str = "{name}"
    evidence: str = "lower"
    subsystem_source: str = "src/game/{subsystem}"
    subsystem_header: str = "include/game/{subsystem}.h"

    def cased(self, text: str, case: str | None = None) -> str:
        how = case or self.case
        return text.upper() if how == "upper" else text.lower() if how == "lower" else text

    def fill(self, template: str, **values: str) -> str:
        """`template` with every field value cased (the template's own text stays as written)."""
        return template.format(**{k: self.cased(v) for k, v in values.items()})

    def file(self, folder: str, module: Mod) -> str:
        """`folder`/<stem><source_ext> of an assigned game or data module."""
        address = module.get("address", module["vram"])
        stem = self.fill(self.stem, name=module["name"], vram=f"{address:08X}", label=module.get("label", ""))
        if self.stem_max and len(stem) > self.stem_max:
            raise ValueError(f"recipe naming: stem {stem!r} of {module['name']} is over stem_max {self.stem_max}")
        return f"{folder}/{stem}{self.source_ext}"

    def evidenced(self, source: str) -> tuple[str, str]:
        """(subsystem, source path) an evidence path names, spelled the way `evidence` says."""
        parts = source.replace("\\", "/").strip("/").split("/")
        if self.evidence != "keep":
            parts = [self.cased(p, self.evidence) for p in parts]
            parts[-1] = parts[-1].rsplit(".", 1)[0] + self.source_ext
        sub = "/".join(parts[:-1])
        where = self.subsystem_source.format(subsystem=sub) if sub else self.subsystem_source.partition("/{")[0]
        return sub, f"{where}/{parts[-1]}"

    def header(self, sub: str) -> str:
        return self.subsystem_header.format(subsystem=sub)


PLAIN = Naming()  # the tool's own scheme, for a project with no sources yet
_SOURCE = re.compile(r"\.(c|cc|cpp|cxx)$", re.I)


def style(root: Path | None) -> Naming:
    """The project's Naming, read from its `src/` files by majority: the case of the stems, the source extension, and
    the stem length limit when every stem of the majority extension fits 8 characters. PLAIN where there are none."""
    found = sorted(p for p in (root / "src").rglob("*") if p.is_file() and _SOURCE.search(p.name)) if root else []
    if not found:
        return PLAIN
    ext = Counter(p.suffix for p in found).most_common(1)[0][0]
    stems = [p.stem for p in found if p.suffix == ext]
    kinds = Counter("upper" if s.isupper() else "lower" if s.islower() else "as-is" for s in stems if s != s.swapcase())
    case = kinds.most_common(1)[0][0] if kinds else "as-is"
    # subsystems sit where the project keeps them: under src/game/ when it has one, else right under src/ (RW: src/AI)
    nested = root is not None and (root / "src" / "game").is_dir()
    headers = (
        [p.suffix for p in (root / "include").rglob("*") if p.is_file() and p.suffix.lower() == ".h"] if root else []
    )
    hext = Counter(headers).most_common(1)[0][0] if headers else ".h"
    return Naming(
        case=case,
        stem_max=8 if max(map(len, stems)) <= 8 else 0,
        source_ext=ext,
        evidence="keep" if case == "as-is" else case,
        subsystem_source="src/game/{subsystem}" if nested else "src/{subsystem}",
        subsystem_header=f"include/{'game/' * nested}{{subsystem}}{hext}",
    )


def labels(doc: Json) -> dict[str, str]:
    """Group segments with the same lowest member VRAM; label the lowest one holding code main and the others overlays.
    Single-segment projects have an empty label."""
    segment = {m["name"]: m["segment"] for m in doc["modules"]}
    start: dict[str, int] = {}
    code: set[str] = set()
    for m in doc["members"]:
        s = segment[m["module"]]
        start[s] = min([start.get(s, m["placements"][0]["vram"]), *(p["vram"] for p in m["placements"])])
        code |= {s} if m["kind"] == "function" else set()
    starts = sorted({start[s] for s in code} or set(start.values()))
    if len(starts) <= 1:
        return dict.fromkeys(start, "")
    names = {v: ("main" if i == 0 else f"ovl_{v:08X}") for i, v in enumerate(starts)}
    return {s: names.get(v, f"ovl_{v:08X}") for s, v in start.items()}


def vram(atom: Json, first: str) -> int:
    """The VRAM of an atom: its placement in the version that names things, else the lowest version's."""
    text = atom["text"]
    return int((text.get(first) or text[min(text)])["vram"])


def _suffix(atoms: Sequence[Json], first: str) -> str:
    """The collision suffix of a module: the VRAM of its second atom, never a counter (one atom: its ROM start)."""
    if len(atoms) > 1:
        return f"{vram(atoms[1], first):08X}"
    text = atoms[0]["text"]
    return f"{int((text.get(first) or text[min(text)])['rom_start']):X}"


def identity(base: str, taken: Collection[str], atoms: Sequence[Json], first: str) -> tuple[str, int]:
    """Unique module identity and assigned address, keeping the existing address suffix sequence through naming."""
    address = vram(atoms[0], first)
    name, more = base, iter([_suffix(atoms, first), *(f"{vram(a, first):08X}" for a in atoms)])
    for extra in more:
        if name not in taken:
            return name, address
        name, address = f"{base}_{extra}", int(extra, 16)
    return name, address


def unique(base: str, taken: Collection[str], atoms: Sequence[Json], first: str) -> str:
    """Unique module name, disambiguated by the existing ordered atom-address suffixes."""
    return identity(base, taken, atoms, first)[0]


def stats(folders: Iterable[str], depth_of: Iterable[str]) -> dict[str, Any]:
    """Files per folder (max, median) and the deepest folder below the repo root, counting its `src/..` parts."""
    count = Counter(folders)
    return {
        "dir_max": max(count.values(), default=0),
        "dir_median": statistics.median(count.values()) if count else 0,
        "depth": max((p.count("/") for p in depth_of), default=0),
    }


def header_beside(header: str, source: str) -> str:
    """A module's header after its source moves: the old header's folder with the new source's stem, "" for none."""
    if not header:
        return ""
    stem = posixpath.splitext(posixpath.basename(source))[0]
    return posixpath.join(posixpath.dirname(header), stem + posixpath.splitext(header)[1])


def place(doc: Json, every: Sequence[Mod], modules: list[Mod], naming: Naming = PLAIN) -> list[str]:
    """Give each proposed module its label, address, data flag and paths: the folder of its origin module, the file
    stem of the project's style, the origin's header renamed beside it. Returns the folders, one batch each."""
    first = doc["names_from"]
    by_atom = {a["name"]: a for a in every}
    label = labels(doc)
    header = {m["name"]: m["header"] for m in doc["modules"]}
    for m in modules:
        atoms = [by_atom[n] for n in m["atoms"]]
        m |= {"label": label.get(m["segment"], m["segment"]), "vram": vram(atoms[0], first), "sub": ""}
        m |= {"data": all(a["kind"] != "function" for a in atoms), "folder": posixpath.dirname(m["origin"])}
        m["source"] = naming.file(m["folder"], m)
        m["header"] = header_beside(header.get(m["origin"], ""), m["source"])
        m["batch"] = m["folder"]
    twice = [p for p, n in Counter(m["source"].lower() for m in modules).items() if n > 1]
    if twice:  # a style whose stems drop the module name may give two modules one file
        raise ValueError(f"recipe naming: {len(twice)} paths name two modules, e.g. {twice[0]}")
    return sorted({m["folder"] for m in modules})
