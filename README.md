# AbuRecipeMaker64

Recipe maker for N64 decompilation. It proposes C for a function or data item. [unbake](https://github.com/AbuN64/cake-unbake-64) judges it.

unbake is the objective side. It extracts, builds, proves every unit against the ROM and lands what matches. Its answers are facts or refusals.
Recipe maker is the creative side. It drafts C, infers types and writes briefs a small model can act on. It may be wrong. It never writes to the project. It only produces candidate files that `unbake compare` or `unbake submit` then prove.

It talks to unbake through the unbake command line and its JSON output only. It never imports unbake.

Recipe maker also proposes module structure as JSON moves for `unbake move -` (`recipe structure`, then `recipe apply`).
