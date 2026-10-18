"""A stand-in for the tree and config readers of `unbake.api`, active in each test module that imports
`fake_api` from here. A config carrying `owners` (function name -> file) gets a tree that answers `owner` from it."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from unbake import api


class FakeTree:
    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners

    def owner(self, version: str, name: str) -> str | None:
        return self.owners.get(name)


def capture(config: Any, overlay: Any = None) -> FakeTree:
    return FakeTree(dict(getattr(config, "owners", {})))


def load(root: Any, host: Any) -> Any:
    raise AssertionError("tests replace api.load where they need a config")


@pytest.fixture(autouse=True)
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(api, "capture", capture)
    monkeypatch.setattr(api, "load", load)
    yield
