"""Shared pytest fixtures. The fake toolchain row replaces toolchains.toml only; every other resource loads for real."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import fixture
import pytest
from abucache.store import forget
from unbake import config as configuration
from unbake import effort, pool
from unbake.contracts import Config

TOOLCHAIN_ROW = {
    "family": "gcc",
    "cc": "cc",
    "as_host": "mips_as",
    "cflags": ["-O2"],
    "supported_options": ["-O1", "-O2"],
    "splat": "splat",
    "emits_asm": False,
    "preserves_padding": True,
    "never_emits": ["mfc0", "eret"],
    "fpu_control_with": ["cvt.w.s"],
    "paired": [],
    "small_data": [8],
    "isa": [3],
    "pins": {},
    "downloads": [],
    "preprocess": ["{cpp}", "-E", "{cppflags}", "{defines}", "{includes}", "{source}"],
    "compile": ["{cc}", "{codegen}", "-c", "{source}", "-o", "{out}"],
}


def _ido_row() -> dict[str, Any]:
    """The shipped ido-7.1 row without pins or downloads, so tests need no installed toolchain."""
    row = dict(configuration.load_resource("toolchains.toml")["toolchain"]["ido-7.1"])
    return {**row, "pins": {}, "downloads": []}


@pytest.fixture
def toolchains(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject the toolchain rows gcc-test and ido-7.1 (no pins, no downloads) into config.load_resource."""
    real = configuration.load_resource
    # Fresh copies per test: tests may edit rows without leaking into later tests.
    document = {"schema": 1, "toolchain": {"gcc-test": copy.deepcopy(TOOLCHAIN_ROW), "ido-7.1": _ido_row()}}

    def load_resource(name: str) -> Any:
        return document if name == "toolchains.toml" else real(name)

    monkeypatch.setattr(configuration, "load_resource", load_resource)
    return document


@pytest.fixture
def host_file(tmp_path: Path) -> Path:
    return fixture.host(tmp_path)


@pytest.fixture
def cfg(tmp_path: Path, toolchains: dict[str, Any]) -> Config:
    """A loaded two-version config with one function."""
    return fixture.config(tmp_path)


@pytest.fixture(autouse=True)
def _fresh_module_state():
    """Every test starts with empty process-wide caches, so xdist workers never share state between tests."""
    forget()
    effort._memo.clear()
    effort._listeners.clear()
    pool._drop_executor()
    yield


@pytest.fixture(autouse=True)
def _recipe_env(tmp_path, monkeypatch):
    """A complete recipe config with no tools, one fixed session for every test."""
    config = tmp_path / "recipe.toml"
    config.write_text("[budget]\nstall_tries = 4\nidle_minutes = 15\npermute_seconds = 120\nskip_days = 7\n")
    monkeypatch.setenv("RECIPE_CONFIG", str(config))
    monkeypatch.setenv("RECIPE_SESSION", "s1")


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """The seam fakes over a two-version fake project, four fake workers."""
    from fakes import Fakes

    found = Fakes(tmp_path / "project")
    found.install(monkeypatch)
    return found


@pytest.fixture(autouse=True)
def _layout_on_test_snapshots(monkeypatch):
    """A test snapshot built by hand carries its layout as `layout`; a captured one is read by `world.of`."""
    from recipemaker import world

    real = world.of
    monkeypatch.setattr(world, "of", lambda snapshot: getattr(snapshot, "layout", None) or real(snapshot))


@pytest.fixture(autouse=True)
def _no_assembly_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing assembly file is waited for in production (an extraction may be swapping asm/); never in a test."""
    from recipemaker import world

    monkeypatch.setattr(world, "ASM_WAIT", 0.0)
