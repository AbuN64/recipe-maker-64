# unbake reduction (full strip)

Status: proposal, 2026-10-09, against unbake `main` 2f20387 (7,486 lines). All `path:line` are at that commit. Main is now 6feb29b (memory-aware auto workers). Lane B re-checks every path:line against main before cutting.
Owner direction: a full strip, not a minimal one. Every creative remnant leaves unbake. The limits:
- no new API (one verbatim move of `split_slot` into `report.py` is the only new public name)
- objective behaviour identical: proofs, landing, layout, ownership, build, check and caching
- no rewrite of objective code

It ships only after recipe maker passes the benchmark gate (EXECUTION.md).

## 1. Deleted files (they move to recipe maker nearly verbatim)

| File | Lines | Goes to |
|---|---|---|
| `src/unbake/draft.py` | 201 | `recipemaker/draft.py`. `_text_starts` and `split_slot` (104-121) go to `report.py` instead (2.1). |
| `src/unbake/crack.py` | 230 | `recipemaker/crack.py` |
| `src/unbake/gbi.py` | 278 | `recipemaker/gbi.py` |
| `resources/data/gbi.toml` | 3,348 data | `recipemaker/tables/gbi.toml`. The raw-gfx opcodes go into `rules.toml` (2.7). |
| `resources/data/hints.jsonl` | 19 data | `recipemaker/knowledge/techniques.jsonl` |
| `resources/data/subsystems.toml` | 145 data | `recipemaker/knowledge/domains/*.toml` as signals and idioms |
| `resources/templates/m2c_macros.h` | template | `recipemaker/tables/m2c_macros.h` |
| schemas `result.draft`, `result.crack`, `packet`, `attempt`, `hint`, `gbi`, `subsystems` | data | gone (recipe maker owns its own records) |
| `tests/test_draft.py`, `test_crack.py`, `test_gbi.py` | tests | recipe maker |

## 2. Cuts inside objective modules

### 2.1 `report.py`
- 12: import becomes `from unbake import effort, land, layout, pool, store, versions`.
- Add `_text_starts` and `split_slot`, verbatim from `draft.py:104-121`. Add `split_slot` to the `report` API row.
- 27-28 `_subsystems`: delete. 36, 49-56, 74: the subsystem bucket becomes the group's `segment` (same loop, other key). The result key `subsystems` becomes `segments`.
- 79-121 `items`: rows keep `member kind segment size address` (+ `source` for withheld units). Delete `rank`, `state`, `best`, `command`, `packet`, the `flow.toml` read (109) and the `crack`/`draft` calls (96, 105, 114). Sort key becomes `(size < 16, -size, address, member)`. `--subsystem` becomes `--segment`.
- 139, 177-179 `objdiff`: categories are segments, named by segment.
- `result.report` and `result.next` schemas follow.

### 2.2 `compare.py`
- 13: drop `crack` from the import. 141: delete the `crack.feedback` call. 148: drop `"feedback"` from the result.
- `result.compare.schema.json`: delete the `feedback` property (249-336) and its `required` entry (528).

### 2.3 `publish.py`
- 2: delete `import json`. 12: delete `crack,`. 222-235: delete `_learned`. 253-254: delete its call. The note still reaches the commit message.

### 2.4 `human.py`
- 99-113: delete the label, `compile_error`, feedback, hint and `next:` rendering. No remaining result carries those keys.

### 2.5 `types.py` (m2c signature guessing)
- Delete `_m2c_key`, `_m2c_job` (34-52), `_signature_jobs`, `signature_group`, `signature` (97-134), `context` (135-140) and `declarations` (141-146). They move to `recipemaker/signatures.py`.
- Imports: drop `Any` (7), `pool`, `process` (12).
- `load`, `scan`, `conflicts`, `landed` stay untouched. `types.toml` evidence and content do not change.

### 2.6 `headers.py` (m2c context)
- Delete `context` and `_parseable` (147-186). They move to `recipemaker/signatures.py`.
- Imports: drop `layout`, `recipes`, `store` (11) and `view` (12).
- `catalog`, `disagreements`, `fold`, `normal`, `sources` stay untouched.

### 2.7 `policy.py` and `rules.toml` (raw-gfx rule)
- `rules.toml`: add `[gfx] opcodes = [...]`, the sorted opcodes from `gbi.toml`, as data.
- `policy.py:122`: read `config.load_resource("rules.toml")["gfx"]["opcodes"]`. The rule's findings are identical.

### 2.8 `recipes.py` (option guessing)
- Delete `_LEVELS` (10) and `proposals` (62-86). `resolve` and its helpers stay.
- recipe maker builds the same variants as plain override dicts (`{"add": [t]}`, `{"omit": [t]}`, level swaps) and passes them to `compare.measure_many`. `recipes.resolve` already pairs and omits flags, so nothing is copied.

### 2.9 Subsystem labels (owner decision 1)
- `infer.py:193-215`: delete `subsystems`. 340-342: drop the label from `Group`.
- `contracts.py:149`: delete `Group.subsystem`. `layout.schema.json` 47-50 and 61: delete the field.
- `layout.py:381` and `infer.py:190`: drop the `"unknown"` argument.
- `repo.py:399`: count segments instead.
- `commands.toml:84-90`: help text and the `--segment` option.
- Migration (one commit in the game repo, by the supervisor (owner decision Q3), with the tool upgrade): `sed -i '/^subsystem = /d' layout.toml` (239 lines). The layout digest changes once, so the first run after it is cold.

### 2.10 Records, tools and keys
- `contracts.py:276-290`: delete `Attempt` (moves to recipe maker). `Submission.origin` comment loses `"crack"`.
- Host tools: delete `m2c` and `permuter` from `contracts.py:65`, `host.schema.json:116-123` and `templates/host.toml:28-29`. recipe maker reads them from its own config (`~/.config/recipemaker/recipe.toml [tools]`). Migration: delete the two lines from `~/.config/unbake/unbake.toml`, or unbake refuses it as an unknown key.
- `toolchains.toml`: delete the `m2c` and `permuter` keys of each toolchain and from `toolchains.schema.json` (47-54, 157-158). They move to `recipemaker/knowledge/compilers/*.toml`.
- `flow.toml`: keep only `[fuzzy] min_gain` (read by `land.py:59` and `publish.py:276`). `flow.schema.json` follows.
- `keys.toml`: delete `draft.context`, `draft.m2c`, `draft.data`, `types.m2c`.
- `config.py:21-22`: drop `subsystems.toml`, `gbi.toml`, `hints.jsonl` from the resource map.
- `rename.py:8`: drop `hints.jsonl` from `_SPELLED`.
- `commands.toml`: delete `[command.draft]` (25-31) and `[command.crack]` (45-53). 8 verbs remain: init, setup, report, compare, submit, land, check, rename.

### 2.11 Contract text and lint
- `CONTRACTS.md`: delete the `gbi`, `draft`, `crack` API rows (55-57) and budgets (141-143). Delete HINTREF, FEEDBACK and the `crack.feedback` shape lines (72-73, 75). Delete the attempts, packets and work runtime rows (84-86). Update the `types`, `headers`, `recipes`, `infer`, `report` rows.
- `tests/test_lint.py:13`: caps become soft 6,500, hard 7,000 (owner decision 2).
- `README.md`: rewrite the Work section with the 8 verbs. It still documents `cycle`, `next`, `search-variants`, `migrate-state` and `setup --confirm`, none of which exist.

## 3. Nothing creative stays

| Checked and kept | Why it is objective |
|---|---|
| `symptoms.py` incl. `diff` | measurement of built vs target bytes |
| `compare.measure_many` | measures several recipes in one call (`compare.measure` uses it) |
| `toolchains.toml supported_options` | what a toolchain accepts, a fact |
| `infer.sdk` | catalog identification |
| `rules.toml` `decompiler-placeholder` | a landing rule: it refuses `M2C_` text |
| `.unbake/` runtime area | recipe maker keeps its state under `.unbake/recipe/` |

## 4. Line count after

| Change | Lines |
|---|---|
| Today | 7,486 |
| draft, crack, gbi deleted | -709 |
| types (2.5) | -70 |
| headers (2.6) | -41 |
| recipes (2.8) | -26 |
| infer (2.9) | -24 |
| publish (2.3) | -18 |
| report (2.1): -20, plus 18 moved in | -2 |
| contracts (2.9, 2.10) | -17 |
| human (2.4) | -15 |
| compare (2.2) | -1 |
| **After** | **about 6,560** |

That is under the new hard cap of 7,000. It is about 60 lines over the soft cap of 6,500, which only warns.

## 5. Risks

| Risk | Handling |
|---|---|
| Old `layout.toml` with `subsystem` rows is refused | The migration line in 2.9, run in the same game-repo commit as the tool upgrade. |
| Old host config with `m2c`/`permuter` is refused | The migration in 2.10. Documented in the release commit message. |
| README bars and `report.json` categories change from subsystems to segments | Owner decision 1 accepted this. One regenerated-files commit at the next landing. |
| Live grinders call `unbake draft`/`crack` | The strip ships only after the grinder runs on `recipe` (EXECUTION.md). |
| Cold caches after the layout digest change | Expected once. Measured in the gate's cold run. |
