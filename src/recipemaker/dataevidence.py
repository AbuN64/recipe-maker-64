"""Alias membership is a checked range relation, never a nearest-symbol guess."""

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from typing import Any

from unbake.contracts import Json


class Storage:
    """Index data extents once; reject overlapping owners, unknown widths and divergent versions."""

    def __init__(
        self,
        members: Mapping[str, Json],
        tables: Mapping[str, Any],
        widths: Mapping[str, Mapping[str, int]] | None = None,
    ) -> None:
        self.members, self.tables = members, tables
        self.widths = widths or {}
        self.spans: dict[str, list[tuple[int, int, int, str]]] = {}
        self.ends: dict[str, list[int]] = {}
        for name, member in members.items():
            if member["kind"] != "data":
                continue
            for p in member["placements"]:
                if p["section"] != ".bss" and p["rom_end"] > p["rom_start"]:
                    self.spans.setdefault(p["version"], []).append((p["rom_start"], p["rom_end"], p["vram"], name))
        for version, spans in self.spans.items():
            spans.sort()
            high = 0
            self.ends[version] = []
            for _, end, _, _ in spans:
                high = max(high, end)
                self.ends[version].append(high)

    def at(self, version: str, row: Any) -> tuple[str, int] | None:
        vram, rom, width, kind = row
        if kind == "func" or rom is None or not width or width < 0:
            return None
        spans = self.spans.get(version, [])
        i = bisect_right(spans, (rom, float("inf"), float("inf"), "")) - 1
        owners = []
        while i >= 0 and self.ends[version][i] > rom:
            start, end, address, name = spans[i]
            if start <= rom < end:
                # Count every intersecting owner, including a short overlapping record.
                owners.append((name, rom - start, rom + width <= end and vram - address == rom - start))
            i -= 1
        return (owners[0][0], owners[0][1]) if len(owners) == 1 and owners[0][2] else None

    def alias(self, spelling: str) -> str:
        found = {}
        for version, table in self.tables.items():
            if spelling not in table:
                continue
            address, rom, width, kind = table[spelling]
            width = max(width or 0, self.widths.get(version, {}).get(spelling, 0))
            found[version] = self.at(version, (address, rom, width, kind))
        relations = set(found.values())
        if len(relations) != 1 or None in relations:
            return ""
        relation = next(iter(relations))
        assert relation is not None
        name, _ = relation
        required = {p["version"] for p in self.members[name]["placements"]}
        return name if required == set(found) else ""

    def loaded(self, versioned: Mapping[tuple[str, str], Sequence[str]]) -> dict[str, list[str]]:
        """Join reader aliases per version; prove every version shared by that reader and storage.

        A version-specific pool is not the storage of another version merely because both use
        one alias spelling. Conversely a version outside the pool's lifetime cannot hide its reader.
        """
        candidates: dict[tuple[str, str, str], set[tuple[str, int]]] = {}
        for (version, reader), aliases in versioned.items():
            for alias in aliases:
                row = self.tables.get(version, {}).get(alias)
                if row is None:
                    continue
                address, rom, width, kind = row
                width = max(width or 0, self.widths.get(version, {}).get(alias, 0))
                relation = self.at(version, (address, rom, width, kind))
                if relation:
                    owner, offset = relation
                    candidates.setdefault((reader, alias, owner), set()).add((version, offset))
        out: dict[str, list[str]] = {}
        for (reader, _, owner), placements in sorted(candidates.items()):
            reader_versions = {p["version"] for p in self.members.get(reader, {}).get("placements", ())}
            owner_versions = {p["version"] for p in self.members[owner]["placements"]}
            if (
                (owner_versions & reader_versions) == {v for v, _ in placements}
                and len({o for _, o in placements}) == 1
                and owner not in out.setdefault(reader, [])
            ):
                out[reader].append(owner)
        return out
