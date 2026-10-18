"""Context preprocessing uses the public build recipe, including header line markers."""

from recipemaker import cpp


def test_context_uses_public_preprocess_with_source_and_markers(monkeypatch):
    calls = []

    def preprocess(config, path, version, text, markers=False):
        calls.append((config, path, version, text, markers))
        return '# 1 "include/game.h"\nint value;\n'

    monkeypatch.setattr(cpp.api, "preprocess", preprocess)
    config = object()
    assert cpp.run(config, "build/context/context.c", '#include "game.h"', "eu") == (
        '# 1 "include/game.h"\nint value;\n'
    )
    assert calls == [(config, "build/context/context.c", "eu", b'#include "game.h"', True)]
