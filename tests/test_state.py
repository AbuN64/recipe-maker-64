"""The recipe config: read from RECIPE_CONFIG, checked against its schema."""

import pytest
from unbake.contracts import Refusal

from recipemaker import state


def test_a_missing_config_file_is_refused_by_path(tmp_path, monkeypatch):
    monkeypatch.setenv("RECIPE_CONFIG", str(tmp_path / "gone.toml"))
    with pytest.raises(Refusal) as caught:
        state.structure()
    assert "gone.toml does not exist" in caught.value.findings[0].reason


@pytest.mark.parametrize(
    "body",
    ['[tools]\nmystery = "x"\n', "[tools]\nm2c = 3\n", "[budget]\nstall_tries = 0\n", "[extra]\na = 1\n", "not toml ["],
)
def test_the_recipe_config_is_checked_against_its_schema(tmp_path, monkeypatch, body):
    path = tmp_path / "recipe.toml"
    path.write_text(body)
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    with pytest.raises(Refusal) as caught:
        state.structure()
    assert caught.value.findings[0].key == "recipe.config"


def test_the_retired_budget_and_tool_keys_still_validate_and_structure_limits_are_read(tmp_path, monkeypatch):
    path = tmp_path / "recipe.toml"
    path.write_text('[budget]\nstall_tries = 4\n[tools]\nm2c = "/x"\n[structure]\ndir_max = 20\n')
    monkeypatch.setenv("RECIPE_CONFIG", str(path))
    assert state.structure().dir_max == 20
