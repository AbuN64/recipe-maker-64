# Usage

Recipe maker for N64 decompilation. It proposes C for a function or data item. [unbake](https://github.com/AbuN64/cake-unbake-64) judges it.

unbake is the objective side. It extracts, builds, proves every unit against the ROM and lands what matches. Its answers are facts or refusals.
Recipe maker is the creative side. It drafts C, infers types and writes briefs a small model can act on. It may be wrong. It never writes to the project. It only produces candidate files that `unbake compare` or `unbake submit` then prove.

It reads the project through `unbake.api` (the captured tree, symbol facts, caches, the pool and stage timing) and
`unbake.contracts` (the records), and asks for verdicts through the unbake command line and its JSON output: `unbake
score` (one candidate, or `--variants` for many in one process), `unbake compare`, `unbake submit` and `unbake report
--next`. It imports nothing else of unbake; `tests/test_imports.py` walks `src/recipemaker` with `ast` and fails on any
other `unbake` import.

A candidate is the function's C (with the declarations it brings) in `.unbake/recipe/work/NAME.c`; unbake splices it
into the module that owns the name in place of its `INCLUDE_ASM` line. Header edits travel as `PATH=FILE` change-set
files. Structure proposals are unified diffs: `recipe structure` writes them, `recipe apply` runs `git apply`, then
`unbake sync`.
