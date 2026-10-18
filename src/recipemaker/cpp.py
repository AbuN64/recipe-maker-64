"""Preprocess C text the way the project's build does: the host `cpp` with the CPPFLAGS of the generated Makefile, the
version macro and the include directories, line markers kept."""

from __future__ import annotations

import posixpath
import re
import shlex

from unbake.contracts import Config, Finding, Refusal

from recipemaker import host

_FLAGS = re.compile(r"^CPPFLAGS\s*:?=(.*)$", re.M)


def flags(config: Config) -> list[str]:
    """The project's CPPFLAGS without -P, so line markers stay."""
    try:
        text = (config.project.root / "Makefile").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as error:
        raise Refusal(
            Finding("draft.context", "the project has no Makefile: run unbake sync", path="Makefile")
        ) from error
    found = _FLAGS.search(text)
    return [f for f in shlex.split(found[1]) if f != "-P"] if found else []


def macro(version: str) -> str:
    return "VERSION_" + version.upper().replace("-", "_")


def run(config: Config, path: str, text: str, version: str) -> str:
    """`text`, as the project file `path`, preprocessed for `version`."""
    tool = config.host.tools.get("cpp")
    if tool is None:
        raise Refusal(Finding("native.missing_tool", "the host config names no cpp", action="set tools.cpp"))
    argv = [str(tool), *flags(config), f"-D{macro(version)}", "-Iinclude", f"-I{posixpath.dirname(path) or '.'}", "-"]

    def produce() -> bytes:
        done = host.run(argv, config.project.root, config, stdin=text.encode())
        if done.exit != 0:
            said = done.stderr.decode(errors="replace").strip().splitlines()[-1:] or [f"cpp exited {done.exit}"]
            raise Refusal(Finding("draft.context", f"cpp: {said[0]}", path=path))
        return bytes(done.stdout)

    return produce().decode(errors="replace")
