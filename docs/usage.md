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

Declaration items (a name declared more than one way) are planned, not written by hand. `recipe next --kind declaration
[--count N] [--dry]` ranks the conflicts (names the open functions need first, a record width mismatch counts as
critical), plans for each one change set (the prototype once in the definition's family header, the other copies
deleted, an include where a unit loses sight of the name, scalar casts only where a call site's old declaration took
other types), and leases the first group of names whose files do not overlap; `--dry` leases nothing and prints every
group. `recipe try NAME [--dry]` measures the whole group in one unbake process (the sources each set edits, compiled
with the set laid over the tree) and lands every exact name as its own commit: unbake lands only candidates that replace
a stand-in, so `settle` writes the set, runs `make check` through `unbake-run`, and commits when every ROM still equals
its base (else the old bytes go back). A try spends at most about 100 CPU s landing; names it did not reach stay leased.
A name with no landed definition, or whose header would break a unit that includes it, is reported with the names it
waits for.
