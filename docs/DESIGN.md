# AbuRecipeMaker64 design (revision 2)

Status: proposal, 2026-10-09. Against unbake `main` 2f20387 (7,486 lines) and recipe maker branch `build` f6c57f7.
Companions: `EXECUTION.md` (how it gets built), `SUMMARY.md` (owner page).
Command `recipe`, package `recipemaker`.

## 0. Binding premises

1. unbake is correct as it is. It loses every creative part. No new unbake APIs. Objective behaviour stays identical.
2. recipe maker depends on unbake and imports its objective modules and machinery. Nothing is copied or reinvented. The creative modules move into recipe maker nearly verbatim.
3. Verdicts come only from unbake (`compare.run`, `land.submit`, `land.drain`). recipe maker never writes tracked project files.
4. Development tests are mocked at unbake's function seams. One profiled end-to-end pass runs at the end. The benchmark gate runs there.

## 1. Layering and the boundary

```
 LLM agent (35B local, stronger models)        human (terminal, TUI)
                  \                             /
          +--------- recipe (cli.py, tui.py) ---------+
          | work model: leases budgets errors look    |   creative: may be wrong
          | ladder: twin data draft options permute   |
          | ledger  domains  knowledge cards  brief   |
          +----------------- import ------------------+
          | unbake objective modules and machinery    |   objective: facts or refusals
          | layout versions headers types symbols     |
          | infer compare land recipes adapters view  |
          | config contracts pool store effort process|
          +-------------------------------------------+
```

### Boundary rules

| # | Rule | Enforced by |
|---|---|---|
| R1 | recipe maker calls only unbake functions listed in the Public API table of unbake's shipped `resources/CONTRACTS.md` (read through `importlib.resources`). No `_private` names. | `tests/test_boundary.py`, the same AST walk as unbake's `tests/test_lint.py:100-121`. |
| R2 | Verdicts: `try` calls `compare.run(config, params)` with the exact CLI params dict. Exploration rungs call `compare.bind`, `compare.measure`, `compare.measure_many`, then `land.submit` and `land.drain`, exactly as the moved `crack.run` does today. | Only `ladder.py` and `work.py` import `compare` and `land`. |
| R3 | Writes go only under `<project>/.unbake/` and only through `store.write`, `store.append`, `store.put`. Tracked files (`src/`, `include/`, `layout.toml`, `symbols.toml`, `types.toml`, splits, configs) are never written. `.unbake/` is ignored by the generated `.gitignore` (`repo.toml:3`) and CONTRACTS invariant 6 already names it as the runtime area. | `test_boundary.py` greps for `open(..., "w")`, `write_text`, `write_bytes` outside the moved modules' existing calls under `.unbake/`. |
| R4 | Processes start only through `process.run`, workers only through `pool.map`/`pool.gather`. One exception: `tui.py` hands the terminal to `$EDITOR`. | Same import grep as unbake invariant 7. |
| R5 | Dependency pin: `unbake @ git+https://github.com/AbuN64/cake-unbake-64@<sha>` in `pyproject.toml`. A version bump reruns `test_boundary.py`. | CI. |

### Where things live (decision on input 2)

`.unbake/` is tool state, not the project tree. recipe maker uses it.

| Path | Writer | Notes |
|---|---|---|
| `.unbake/recipe/work/<stem>.c` | moved `draft.create`, moved `crack.run` (path constant changed from `.unbake/work/`) | The one file the agent edits. |
| `.unbake/recipe/attempts/<stem>.jsonl` | moved `crack.feedback` (`store.append(config, "recipe/attempts/<stem>", row)`) | First run imports the old `.unbake/attempts/*.jsonl` once. |
| `.unbake/recipe/packets/<stem>.json` | moved `crack.packet` | The brief is built from it. |
| `.unbake/recipe/leases/<stem>.json` | `state.py` | Lease records. Read-modify-write under `store.exclusive(config, "recipe")`. |
| `.unbake/recipe/{skips,feedback,tries}.jsonl` | `state.py` via `store.append(config, "recipe/skips", row)` | |
| `.unbake/recipe/status.json`, `domains.json` | `state.py`, `domains.py` via `store.write` | |
| `.unbake/recipe/work/<stem>/brief.md`, `kit/`, `rungs/<n>.c` | `brief.py`, moved `_permute` (`store.work`) | |
| `.unbake/stages.jsonl` | unbake `effort` | Input 1: recipe maker's stage rows go here, tagged by `path[0] = "recipe <verb>"`. The supervisor's perf watcher already follows this file. |
| host content cache | unbake `store` / abucache | Same cache, same warmth. recipe maker kinds are prefixed `recipe-`. |

## 2. Package layout

Lines are estimates. "moved" means taken from unbake with only import lines changed.

| Module | Lines | Origin | unbake functions it uses |
|---|---|---|---|
| `cli.py` | 230 | new | `config.load`, `effort.command`, `effort.bind`, `human.attach`, `human.summary`, `contracts.Refusal` |
| `draft.py` | 180 | moved `draft.py` (minus `_text_starts`/`split_slot`, now `report.split_slot`) | `layout`, `versions`, `headers.catalog`, `native.prove`, `recipes.resolve`, `pool`, `store`, `process`, `report.split_slot`, `signatures` |
| `signatures.py` | 115 | moved `types.py:34-52, 97-146` and `headers.py:147-186` (m2c prototypes and header context) | `types.load`, `headers.sources`, `headers.normal`, `view.get`, `layout.overlay`, `recipes.resolve`, `pool`, `store`, `process` |
| `crack.py` | 215 | moved `crack.py` | `compare`, `land`, `recipes.resolve`, `adapters.assembler`, `adapters.script`, `process`, `store`, `symptoms`, `versions`, `options`, `signatures` |
| `options.py` | 25 | new, replaces `recipes.proposals` | builds override dicts from `toolchains.toml supported_options` and the unit recipe; `recipes.resolve` does the pairing |
| `gbi.py` | 278 | moved `gbi.py` | its table moves too: `recipemaker/tables/gbi.toml` |
| `twin.py` | 110 | new | `infer.pairs`, `infer.correspondences`, `layout.unit_of`, `symbols.rewrite`, `config.load_resource("units.toml")` |
| `ladder.py` | 120 | new | the rung order and budgets; calls `twin`, `draft`, `crack` steps |
| `work.py` | 160 | new | verbs `next`, `work`, `try`, `skip`; `compare.run`, `report.items` |
| `state.py` | 215 | new, plus `Attempt` moved from `contracts.py` | `store.write`, `store.append`, `store.rows`, `store.exclusive`, `store.stem` |
| `errors.py` | 140 | new | `contracts.Finding`, `config.sentence` |
| `look.py` | 110 | new | `index.py` |
| `index.py` | 200 | new | `layout.capture`, `headers.catalog`, `headers.disagreements`, `types.load`, `symbols.load`, `pool.map`, `store.cached`, `effort.memo` |
| `cparse.py` | 230 | today's `structs.py` | `headers.normal`, `headers.sources`, `pool.map`, `store.cached` |
| `ledger.py` | 280 | today's `rewrite.py`, grown | `index.py`, `types.load` |
| `domains.py` | 150 | new | snapshot groups (`segment`, `members`, `sdk`), `versions.rom_bytes`, `pool.map`, `store.cached` |
| `brief.py` | 130 | new | packet JSON, `symptoms.diff` |
| `llm.py` | 100 | new (last) | none (stdlib `urllib`) |
| `tui.py` | 260 | new | the verbs above |
| **Total** | **about 3,250** | of which about 790 moved | cap 5,000 (decision 3, raised a fourth time on 2026-10-10) |

Moved modules keep their bodies. Only imports, the `.unbake/recipe/` path constants and the finding keys change (recipe maker keys live in `knowledge/errors.toml`, since unbake's `keys.toml` drops the draft keys).

## 3. The work model (agents and humans)

An **item** is a layout member. One state machine. The agent and the human use the same seven verbs. The TUI only calls these verbs.

```
 open --next--> leased --work--> candidate --try--> exact | fuzzy | failed --try--> ...
                  |                                            |
                  +------------- skip (reason) <---------------+   budget out = automatic skip
```

### 3.1 Verbs

Default output is short text that ends with one `next:` line. `--json` prints one JSON object with the same fields.

| Verb | Does | Fields |
|---|---|---|
| `recipe next [--domain D] [--kind K]` | Ranks open items from `report.items(snapshot, {})`, takes a lease on the best one. Leases replace shards. | `item kind domain size why lease_until next` |
| `recipe work ITEM [--seconds N] [--no-submit]` | Runs tool rungs (section 6) until exact or spent. Writes the candidate and the brief. | `item rungs[] result score{v} candidate brief auto_fixes[] budget next` |
| `recipe try ITEM` | Applies safe auto-fixes, then `compare.run` on `.unbake/recipe/work/<stem>.c`. Translates errors. Counts the try. | `item result score try tries_max minutes_left error{class line what fix} auto_fixes[] next` |
| `recipe look NAME` | Prototype, struct layout, global type, landed path, callers. | `name kind text where fields[] seen_in[] next` |
| `recipe skip ITEM --reason R [--note T]` | Releases the lease. `R` in `m2c-failed split-symbol type-unresolved jump-table needs-header-change budget other`. | `item reason next` |
| `recipe status [--json]` | Sessions, leases, today's counts, error classes, feedback events. | section 10.2 |
| `recipe tui` | Curses views over the verbs. | section 11 |

### 3.2 Rules every verb follows

- One `next:` line with a full command and absolute paths.
- No logs on stdout. The stage tree goes to stderr through `human.summary` and to `.unbake/stages.jsonl` through `effort`.
- Every compiler error and every `Refusal` maps to a class with a fix (section 4).
- Budgets live in the tool (section 3.4).
- The tool refuses unsafe moves (section 3.5).

### 3.3 Session examples (exact text a weak model sees)

```
$ recipe next
item: func_8021A3B0_b
kind: function   size: 212 bytes   domain: audio
why: a landed twin exists (rev1 func_8021B000, 100% shared masked code)
lease: 15 minutes, 4 tries
next: recipe work func_8021A3B0_b
```

```
$ recipe work func_8021A3B0_b
rungs: twin -> exact in de
result: exact. unbake landed it (commit 4be1c02)
next: recipe next
```

```
$ recipe work func_80233F10_b
rungs: twin (none) -> draft (compiles) -> options (no gain) -> permute 120 s (91.4% -> 96.8%)
result: fuzzy 96.8% in de. unbake landed it as fuzzy
auto-fixes: resolved 9 of 11 M2C_FIELD. Removed local struct Actor (include/actor.h:14 defines it)
left: 2 M2C_FIELD on arg0 at 0x5C and 0x60. No view of its type has fields there.
file: /work/project/.unbake/recipe/work/func_80233F10_b.c
brief: /work/project/.unbake/recipe/work/func_80233F10_b/brief.md
budget: 4 tries, 14 minutes left
next: read the brief, edit the file, then run: recipe try func_80233F10_b
```

```
$ recipe try func_80233F10_b
result: does not compile (try 2 of 4, 9 minutes left)
error: call-arity at line 41
what: func_80012345 gets 2 arguments. Its prototype takes 3.
prototype: void func_80012345(s32 arg0, Actor *arg1, s32 arg2);   /work/project/include/actor.h:88
fix: add the third argument. In the asm it is $a2, set at line 57 (move $a2, $s1). $s1 holds `count` in your file.
file: /work/project/.unbake/recipe/work/func_80233F10_b.c
next: edit line 41, then run: recipe try func_80233F10_b
```

```
$ recipe try func_80233F10_b
refused: you changed a tracked project file: /work/project/include/actor.h
why: a candidate may change only its own file
fix: undo the header change
next: git -C /work/project checkout -- include/actor.h
```

```
$ recipe try func_80233F10_b
refused: budget spent (4 of 4 tries). Skipped with reason budget. Best kept: fuzzy 96.8%.
next: recipe next
```

### 3.4 Budgets and leases

| Item | Rule |
|---|---|
| Lease | `{item, session, started, expires, tries}` in `.unbake/recipe/leases/<stem>.json`. Taken and changed only under `store.exclusive(config, "recipe")`. Expired leases are free. `next` never returns a leased item. |
| Session | `RECIPE_SESSION` env, else the parent process id. Shown in `status`. |
| Per item | 4 tries and 15 minutes from lease (config `[budget]` in `~/.config/recipemaker/recipe.toml`). Tool rungs inside `work` do not count as tries. |
| Out of budget | `try` refuses, writes a skip with reason `budget`, releases the lease, prints `next: recipe next`. |
| Permuter | `--seconds` capped by config `[budget] permute_seconds`. It runs inside `store.slots(config, config.host.workers)` so it never oversubscribes the host. |
| Skip return | A skipped item reopens when the HEAD changes something it depends on (a new landed twin, a new declaration of a name it uses) or after 7 days. |

### 3.5 Refusals of unsafe moves

`try` refuses when:
- the item is leased by another session, or not leased at all
- `git status --porcelain -- src include layout.toml symbols.toml types.toml` is dirty (read-only `git` through `process.git`)
- the candidate still holds `M2C_` text (unbake would refuse it under rule `decompiler-placeholder`, `rules.toml:78-82`; recipe maker says so earlier and cheaper)
- the budget is spent

A verdict is only what `compare.run` returned for this candidate. recipe maker has no way to mark a failure as "pre-existing".

## 4. Error translation

`knowledge/errors.toml`, one row per class: `match` (regex on compiler text in `Proof.missing`, or a `Finding.key`), `what`, `fix` templates, optional `autofix`.

| Class | Matches | Auto-fix before `try` | Fix line |
|---|---|---|---|
| `decl-conflict` | `conflicting types for`, `redefinition of`, finding `headers.conflict` | Drop the local definition when `headers.catalog` has the same name. | use the header's type |
| `undeclared` | `` `x' undeclared `` | Moved `draft._checked` already adds `M2C_UNK` locals (`draft.py:144-162`). Known globals get their declaration from `types.declarations`. | declare x as the type from `look` |
| `m2c-text` | `M2C_ERROR`, `?`, unknown instruction comments | none | the asm lines behind it |
| `m2c-field` | `M2C_FIELD(` left | ledger (section 7) | the type and offset, with a `recipe look` command |
| `not-exact` | finding `land.not_exact` | none | score per version and the worst diff hunk |
| `unknown` | anything else | none | the raw first line, then `skip --reason other`. A feedback event is written. |

## 5. `look`

`look` reads only the index (section 9). Answers:

| Query | Answer |
|---|---|
| a function name | prototype with evidence (`types.toml` row) and `path:line`, landed path if any, callers from the snapshot |
| a type name | merged layout from the ledger: `offset size kind name view-path:line` per field |
| a global | its declaration and section/size |
| unknown | "not known" plus a feedback event `look-miss` |

## 6. The recipe ladder, built from the moved modules

| # | Rung | Code | unbake calls | Cost |
|---|---|---|---|---|
| 0 | guard | moved `draft.refuse_fragment` (calls `report.split_slot`); `layout.unit_of` for withheld source | `report.split_slot`, `layout.unit_of` | ms |
| 1 | twin | `twin.py` | `infer.pairs` (cached on masked code), `infer.correspondences` (operand pairs), `layout.unit_of` on the counterpart, `symbols.rewrite` to swap names, then `compare.measure` | 1 measure |
| 2 | data literal | moved `draft._data_text` | `versions.rom_bytes`, snapshot symbols | 1 measure |
| 3 | draft | moved `draft.create` (m2c both ways, GBI rewrite, header variants, `_checked` compile probes in `pool.map`) + ledger pass on the winner | `signatures.context`, `signatures.signature_group`, `signatures.declarations` (moved), `native.prove` | m2c + up to 8 compiles, all cached |
| 4 | options | moved crack options step, variants from `options.py` | `recipes.resolve` (via overrides), `compare.measure_many` in one call | 1 batched measure |
| 5 | permute | moved `crack._permute` | `adapters.assembler`, `adapters.script`, `process.run("permuter")` under `store.slots` | `--seconds` |
| 6 | edit loop | agent, or `llm.py` | `try` -> `compare.run` | LLM time |

Rungs 1-5 record attempts with moved `crack.feedback`, so the history format stays the `Attempt` record (moved from `contracts.py` into `state.py`, fields unchanged). A rung that lands exact stops the ladder. The best candidate always ends in `.unbake/recipe/work/<stem>.c`. Rungs 1-5 explore without `compare.run`. Only a real gain is sent to `land.submit`, with the same fuzzy rule (`flow.toml [fuzzy] min_gain`) that `crack.run` uses today.

Twin detail: for item `X` in version `a`, `infer.pairs` gives counterpart `Y` in version `b`. If `Y` is in a decompiled unit, copy that unit's text for `Y`, rename `Y` to `X`, and swap every name `y` to `x` where `infer.correspondences` has `(x, a, y, b)`. Both calls are pool jobs with content keys, so a warm run costs a cache read.

## 7. The type ledger (no casts, decision 7)

The project holds many partial one-function views of one real type. Today 28 of 164 `M2C_FIELD` resolve on 10 sample items.

1. **Observe.** `cparse.py` reads every struct view in `include/**` and `src/**` (paths from `headers.sources`), one pool job per file, cached on the file's content.
2. **Identify.** Views are one identity when: one global is declared with both (`types.load` globals and `headers.disagreements`); one function parameter is typed with both (prototype and landed definition); one callee argument receives both; or they share 3 or more equal `(offset, size, kind)` fields with no conflict.
3. **Merge.** The merged layout is the union of fields. A conflict at one offset splits the identity.
4. **Resolve** `M2C_FIELD(base, type, off)`:
   1. The declared type of `base` has a field at `off`: write it.
   2. Otherwise leave the placeholder. The brief names the identity, its views and the missing offset. The skip reason is `needs-header-change` when another view has the field.
   No casts to another view (decision 7).
5. **Merge proposals** (decision 6). For an identity with 2+ views, `ledger.py` writes a unified diff to `.unbake/recipe/proposals/<identity>.patch` that names the padding fields of the main view and turns the other views into the main type. unbake has no verb that lands a header-only change and gets no new one (premise 1). So a patch rides with the next C landing that needs it: unbake already lands header edits with a C unit and re-proves every includer (`publish.py:197`). Owner decision Q1.

## 8. Game parts: domains and knowledge cards

### 8.1 Domains

| id | Covers | Generic signals |
|---|---|---|
| `boot` | entry, threads, main loop | segment entry, `mtc0`/`cache`, thread and message-queue API calls |
| `sdk` | libultra, libc, libgcc helpers | `Group.sdk` (unbake SDK catalog), `os`/`__os` API names |
| `memory` | heaps, allocators, DMA, overlays | PI register region, copy-family calls, free-list walks |
| `graphics` | display lists, matrices, textures, RSP/RDP | GBI opcode stores (`gbi.toml`), `gu*` calls, VI registers |
| `audio` | synthesis, sequences, samples, voices | AI register region, `al*` API, audio task type |
| `sfx` | game-side sound requests and tables | calls into `audio` with constant ids |
| `input` | controllers, paks, rumble | SI region, `osCont*`/`osPfs*` API |
| `assets` | ROM tables, formats, decompression | DMA from table offsets, bit-stream loops |
| `engine` | objects, scene, collision, camera, animation, particles | callers of `memory` and `graphics` with per-object structs |
| `gameplay` | rules, players, weapons, AI, modes | the remainder calling `engine` and `sfx` |
| `math` | vectors, matrices, trig tables | leaf float functions, `sqrt.s` |
| `ui` | text, fonts, menus | string tables, glyph loops |

### 8.2 Discovery per project

1. Facts per group from the snapshot: `segment`, `members`, `sdk` (unbake's catalog identification).
2. Signals per group from the snapshot: callee names (asm `jal` targets), hardware region hits (`rules.toml [hardware]` ranges), mnemonic counts. One pool job per group, cached on the group's masked code.
3. Score against each card's `[signals]` (the scoring that `infer.subsystems` did with `subsystems.toml`, moved and extended).
4. Label propagation over the call graph: 0.5 weight per hop, 2 hops.
5. Data items take the domain of their referencing functions.
6. Owner pins in an optional owner-written `recipemaker.toml` at the project root. recipe maker only reads it.

Result in `.unbake/recipe/domains.json`, `{group: {domain, score, why}}`.

### 8.3 Cards (original, generic)

```
src/recipemaker/knowledge/
  domains/<id>.toml       idioms, naming, type shapes in prose, argument rules, compiler notes
  compilers/<family>.toml ido, kmc-gcc 2.7.2, sn-gcc 2.8.1: codegen tells, option orders
  hardware.toml           register regions, RSP task types
  errors.toml             section 4
```

Rules, checked by `tests/test_knowledge.py`: identifiers and hardware facts only, plus original prose. No C block longer than 3 lines. `source = "original"` on every card. No game names (a local untracked deny list is checked by `bin/lint`). No ROM bytes, no SDK header text.

Machine tables live apart in `src/recipemaker/tables/`: `gbi.toml` (3,348 lines, moved) and `m2c_macros.h` (moved). Knowledge cap (owner decision Q2): the 3,000-line cap counts authored files only (cards, `errors.toml`). Moved or generated tables do not count, but must be pure data with a schema.

### 8.4 What domains feed

| Consumer | Use |
|---|---|
| `next` | rank: `sdk` and `math` first, then twin-ready, then size; per-domain weights in `recipemaker.toml` |
| brief | card excerpt (at most 10 lines) |
| ledger | argument rules type call arguments; shapes hint pointer, integer or float |
| options | compiler notes order the option variants |
| naming | `look ITEM` shows a suggested name; names change only by `unbake rename` |

## 9. The index and the cache

The index is one value per HEAD, stored with `store.cached(config, "recipe-index", key, produce)`. `key = digest((snapshot.digest, header digests, knowledge digest, _CODE))`, where `_CODE` is recipe maker's own module digest (the pattern unbake uses in `ownership.py:21`). Per-file parses use `pool.map(..., key=...)` so they are cached one by one. Within a command, `effort.memo` holds the loaded index.

| Part | From | Invalidated when |
|---|---|---|
| items, groups, members | `layout.capture` snapshot | snapshot digest |
| declarations | `headers.catalog` (already pool-cached per file in unbake) | that file's content |
| type rows | `types.load` | `types.toml` content |
| struct views and fields | `cparse.py` per file | that file's content |
| identities | ledger build | any view in it |
| twins | `infer.pairs`, `infer.correspondences` | masked code of the versions |
| domains | section 8.2 | group signals or knowledge digest |

## 10. recipe maker state

### 10.1 Records

- Attempts: moved `crack.feedback` keeps writing `Attempt` rows (fields unchanged) to `.unbake/recipe/attempts/<stem>.jsonl`.
- `recipe/tries.jsonl`: `{time session item result score{v} error_class auto_fixes[] wall_s}`, one per `try`.
- `recipe/skips.jsonl`: `{time session item reason note best_score tries}`.
- `recipe/feedback.jsonl`, written by the tool: events `unknown-error` (with raw line), `repeat-error`, `refused-move`, `budget-out`, `look-miss`, `unbake-refused` (finding key). This replaces hand-written FEEDBACK.md.

### 10.2 `recipe status --json`

```json
{"head": "f86c5c5b1",
 "sessions": [{"session": "s2", "item": "func_80233F10_b", "rung": 6, "tries": 2, "minutes_left": 9}],
 "today": {"exact": 31, "fuzzy": 12, "skipped": 9, "tries": 88, "minutes_per_exact": 7.4},
 "skip_reasons": {"needs-header-change": 4, "split-symbol": 3, "budget": 2},
 "error_classes": {"call-arity": 14, "decl-conflict": 9, "unknown": 2},
 "feedback_events": 5, "open": 4120, "twin_ready": 212}
```

## 11. TUI (`recipe tui`, stdlib curses)

| View | Shows | Keys |
|---|---|---|
| Queue | ranked items: domain, size, twin flag, last result | `n` next, `/` domain filter |
| Item | target asm, candidate, worst diff; budget and score per version | `w` work, `t` try, `e` `$EDITOR`, `l` look word, `s` skip |
| Domains | per domain: items, bytes matched, open, top error classes | `enter` filter Queue |
| Status | sessions, leases, exact per hour, feedback | `r` refresh |

## 12. Speed: just as fast and warm as unbake

| Rule | How |
|---|---|
| One process, one snapshot | Each verb runs `layout.capture` once (unbake caches it on content) and passes the snapshot to every call. `compare.run` recaptures by design; that cost is unbake's. |
| Same cache | All heavy work goes through `store.cached` or `pool.map(key=...)`. m2c output, compile probes, header scans, pairs and proofs are already cached by unbake code; recipe maker adds only `recipe-*` kinds. |
| All cores | Per-file parses, per-group signals and compile probes run in `pool.map`, bounded by unbake's host limits. |
| Fed draft pool | The draft rung keeps every auto worker busy. Today's `crack.draft` and `crack.run` stages run at about 2.5 of 10 workers (live 21:35: 17.3 s for 9 jobs and 18.5 s for 30 jobs; 21:27: 11.2 s at 2.4 workers for 13 jobs). Rung 3 therefore builds the full candidate set before it calls the pool: m2c both ways x GBI rewrite x header variants x compile probes for the item, plus the same set for the next leased items (`work --batch N`, N from `config.host.workers`). It sends that set as one flat `pool.map`, with no per-variant rounds and no serial wait between m2c and the probes of another candidate. When the jobs number at least the workers, the stage's mean busy workers must be at least 0.75 x workers (perfwatch, section 14 gate). |
| Host slots | Permuter and native work hold `store.slots`. |
| No locks held | recipe maker never takes `store.exclusive("land")`. `land.drain` returns `running: true` when a landing is busy. |
| Targets (warm) | `next` under 1 s, `look` and `status` under 0.5 s, `try` = `compare.run` time + 0.2 s, `work` rungs 1-4 = unbake time + 2 s. |
| Observed | `effort` stage rows in `.unbake/stages.jsonl` and the stderr tree, same budgets checks as unbake (`effort._budget`). |

## 13. Testing

| Kind | How |
|---|---|
| Seam mocks | A `fakes.py` fixture monkeypatches unbake seams: `layout.capture` (a small hand-built `Snapshot` from `contracts` dataclasses), `compare.measure`/`measure_many`/`run`, `land.submit`/`drain`, `process.run`, `native.prove`, `infer.pairs`/`correspondences`, `headers.catalog`, `types.load`. `pool.map` runs inline. |
| Work counting | Every fake counts calls. Tests assert counts: twin rung = 1 measure; data rung = 1 measure; options rung = 1 `measure_many`; a second `work` on unchanged inputs = 0 `process.run` (all cache hits, read from `effort.counters()`); `try` past budget = 0 `compare.run`. |
| Pure units | ledger merge and resolution, error classification (one real stderr sample per class), twin name swap, budget and lease logic, brief size cap. |
| Moved tests | unbake's `test_draft.py`, `test_crack.py`, `test_gbi.py`, the signature tests of `test_types.py`, the context tests of `test_headers.py`, the proposals tests of `test_recipes.py` and the subsystem tests of `test_infer.py` move with their code, imports changed. |
| Boundary | R1, R3, R4 as AST tests. |
| Knowledge | provenance and no-code-block checks. |
| Speed | `bin/test` runs `pytest -n auto`. The whole suite stays under 30 s. No real tools, no real project. |
| End-to-end | Once, at the end: `bin/bench` on the live project with the stage tree profiled. The benchmark gate runs there. |

## 14. Phases

The build order, lanes, branches and perf checks live in the maintainer's execution plan (not in the repo). In short:

| Phase | Work | Done when |
|---|---|---|
| P1 Code | All recipe maker code (moves first, then new modules). | `bin/lint` clean, imports resolve, no tests written yet. |
| P2 Tests | Lean seam tests, then one full suite run. | `bin/test` green, under 30 s. |
| P3 Gate | One profiled end-to-end pass and the benchmark gate. The grinder switches to `recipe`. | Gate passes warm and cold. |
| P4 Strip | The unbake strip and the two owner migrations. | unbake suite green, caps enforced. |
| P5 Driver | optional `llm.py`. | Exact per hour at least that of the external agent at equal slots. |

### Benchmark gate (in P3)

Frozen item list `bench/items.txt` at one HEAD: 40 unlanded functions stratified by size and domain, 15 data items, 15 twin-eligible, 10 jump-table functions.
Run A: `unbake crack ITEM --seconds 120 --no-submit` (before the reduction). Run B: `recipe work ITEM --seconds 120 --no-submit`. Same HEAD, host and workers. Cold and warm recorded.

| Metric | Gate |
|---|---|
| exact items | B at least A, strictly more on the twin set |
| first draft compiles | B at least A + 10 points |
| median best score of non-exact | B at least A |
| `M2C_FIELD` left in best candidate | B at most 40% of A's count |
| draft rung pool use | every `recipe` draft stage row with jobs at least the workers runs at 0.75 x workers or more (today's `crack.draft` is about 0.25 x) |
| wall per item, warm | B at most 1.2 x A |

## 15. Owner decisions

From revision 1 (2026-10-09), all holding under the full strip:

| # | Decision | Status |
|---|---|---|
| 1 | Subsystem labels leave unbake | Done by the unbake strip. Needs the `layout.toml` migration (239 `subsystem =` lines, Q3). |
| 2 | unbake caps hard 7,000, soft 6,500 | About 6,560 after the strip: under hard, 60 over soft (warns only). |
| 3 | recipe maker caps 3,800 Python (raised from 3,500 on 2026-10-10), 3,000 knowledge | About 3,250 planned. See Q2 for tables. |
| 4 | LLM driver late, one slot | P5. |
| 5 | Plain text default, `--json` | Holds. |
| 6 | Header patches that merge partial structs | Holds. See Q1 for how they land. |
| 7 | No casts to another view | Holds (section 7). |
| 9 | Benchmark gates | Hold. |

Round 2 (2026-10-09, owner said yes to all):
- **Q1** Merge patches ride with the next C landing that needs them. unbake already lands header edits with a C unit and re-proves every includer. No new verb and no hand commits.
- **Q2** Moved machine tables (`gbi.toml`) do not count against the 3,000-line knowledge cap.
- **Q3** The supervisor runs the two migrations when the strip ships: the 239 `subsystem =` lines of `layout.toml` as one approved supervisor commit, and the `m2c`/`permuter` lines of the host config.
- **E1** The grinder pauses for the step 8 gate (about 2 hours).
- **E2** Lane B does the unbake strip, then the knowledge half.

## Structure

recipe maker also proposes module structure. Two verbs, both reaching unbake only through its command line (R4).

- `recipe structure [--segment S]` runs `unbake map` and reads `.unbake/map.json`, the only evidence channel. It never
  reads layout files itself. It prints the proposed moves as JSON lines on stdout, so `recipe structure | unbake move -`
  works, and saves them with `proposal.json` under `.unbake/recipe/structure/`. The summary and one `next:` line go to
  stderr. `--json` prints one summary object instead.
- `recipe apply` takes the next batch (one segment, at most `[structure] batch_moves` moves) from `moves.jsonl` and
  pipes it to `unbake move -`. A move unbake refuses is dropped and appended to `refused.jsonl`, and the rest is sent
  again. The cursor advances. It never stages or commits. Its `next:` line is the commit unbake printed, then
  `recipe apply`.
- The proposal cuts a segment into modules where two independent signals agree in every holder version: a padded
  object boundary, no call edge crossing, no shared load crossing, no caller on the left, a holder set change.
  Segment edges, toolchain changes, sdk edges, authored modules and library units are hard cuts. A module longer than
  `split_over` functions is cut at its weakest join. Same-includer and identical headers merge into one, and the most
  included typedef-only header becomes `include/types.h`.
- Paths follow the project's naming style, config.toml `[naming]` with the folder templates of its `[layout]`: case
  of the assigned names, a stem limit, the source suffix, the stem, subsystem, shared, data and part templates, and
  whether an evidence row's path keeps its own spelling. Without the table recipe uses its own lower-case scheme.
  Recipe holds no project's style. Library code keeps its names.
- Names stay neutral: `code_<VRAM>`. LLM naming of modules and files is later work and will emit `module`, `source`,
  `header` and `file` moves into the same stream.
