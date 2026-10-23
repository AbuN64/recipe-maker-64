"""Findings as text: the sentence and fix of a key, the reason when it differs, one next line."""

from unbake.contracts import Finding

from recipemaker import cli, errors


def test_recipe_findings_render_without_unbakes_key_table():
    lines = cli._finding(Finding("recipe.apply", "git apply refused", action="recipe structure"))
    assert lines[0] == "refused: git apply refused" and lines[-1] == "next: recipe structure"


def test_a_key_with_a_sentence_gives_its_sentence_the_reason_and_the_fix():
    lines = errors.render(Finding("move.syntax", "line 3 is not a move"))
    assert lines[0].startswith("refused: the move stream") and "because: line 3 is not a move" in lines
    assert any(x.startswith("fix: fix the stream") for x in lines) and lines[-1] == "next: recipe name next"


def test_a_key_unbake_does_not_know_still_renders(monkeypatch):
    from unbake import config

    monkeypatch.setattr(config, "sentence", lambda key: (_ for _ in ()).throw(KeyError(key)))
    assert cli._finding(Finding("land.request", "no such member"))[-1] == "next: recipe name next"
