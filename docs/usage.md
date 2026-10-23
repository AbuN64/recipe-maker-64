# Usage
Recipe maker for N64 decompilation cleanup. It proposes file patches and names for a tree that [unbake](https://github.com/AbuN64/cake-unbake-64) builds and proves; it never decides whether a change is right.

It reads the project through `unbake.api` (the captured tree, symbol facts, caches, the pool and stage timing) and
`unbake.contracts` (the records), and runs the unbake command line for the map and for `sync`. It imports nothing else
of unbake; `tests/test_imports.py` walks `src/recipemaker` with `ast` and fails on any other `unbake` import.

Verbs: `structure`, `apply`, `name`, `style`, `look`. The draft verbs (`next`, `work`, `try`, `skip`, `status`) and the
modules behind them (m2c draft ladder, rungs, declaration and type items, leases and tries) were retired by RECIPE-FIX-1
(10-23); git history keeps them.

`recipe structure` writes module moves, file renames and include edits as a unified diff under `.unbake/recipe/patches/`
and prints it; it never moves a function into a `DATA` module. `recipe apply` runs `git apply` on the oldest saved patch
that still applies (one that no longer does is set aside as `.stale`), then `unbake sync`.

`recipe name next --step N1..N4` writes an evidence pack for an LLM to name functions, data, subsystems; `submit FILE`
judges the rows and writes the renames as a patch (every version's symbol table follows, the functions the project's
libultra table names take the SDK name by themselves, and the `#define NAME fz_OLD` shims and local `extern`s a header
declares go). `--step T` renames invented project records to the original types of the debug catalog: records that match
one original are merged into one (the duplicate is deleted, every spelling follows) when each member can take its original
name; a member spelling that other records share is renamed only where the base of every `.name` and `->name` is typed
(`access.py`); a report line says why each record did not move.

`recipe style [PATHS | --paths PATH...]` checks names and layout against the clean-code rules; a name the debug catalog
spells so is the original and is not a finding. `recipe look NAME` shows a prototype, type, field or global.
