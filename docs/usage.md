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

## Target-safe names

Names and structure paths must pass unbake's shared `symbol-names.toml` table
(elf-gcc, coff-mingw, macho-clang, android-clang, msvc). This applies to debug
names as well as proposals and saved patches. Aliases use plain C names and
`#define`; the compiler owns object-file prefixes and C ABI decoration. Explicit
asm labels and target-specific symbol aliases are refused before a patch is
emitted or applied. Case-only collisions are refused across the source set.

## Portable class names and reviewed naming debt

Class methods use one underscore, for example `CVector3_GetDirectionRotation2`.
The `Class__Method` convention is retired. Name and style validation reject any
`__`, leading underscore reserved namespace or compiler keyword before accepting
evidence; debug and SDK identities do not grant permission to propose one.
C-prefixed class methods retain their class prefix independently of subsystem
folder names. Packs carry the single-underscore rule in `names.class_separator`.

`recipe name next` includes `naming_debt` grouped by source file in each normal
pack and publishes `.unbake/recipe/name/symbol-name-debt.json` for all steps. It
reads Unbake's reviewed `config/symbol-name-baseline.json`; successful
`unbake audit --against REV` runs remove fixed entries. New occurrences, paths
or findings fail audit. Land the reduced config together with source fixes.
SDK implementation namespace exemptions apply only to source checks and never
to names this tool proposes. Aliases remain plain C names and `#define`.
Source-only portability repairs use recipe name submit FILE with JSONL rows: kind is repair
(all source/header tokens and version symbol tables) or local (one function definition/prototype);
target, name, path, line and doc are required. A local row also names function.
The source path/line is checked evidence of the old nonportable identifier. New spellings must be portable
and unused in scope. Original SDK names must be left out of repair submissions. These rows do not build a ROM map.
Renames preserve comments and string bytes. `recipe name next` and `submit` apply the patch to the tree at once (only the renamed tokens change) and name one next link; `--no-apply` keeps it saved for `recipe apply PATH`.

Member renames (`field` rows, step M) of a name several records spell: the types rename the accesses they can place; the rest is settled by the compiler. After the apply, `submit` compiles the objects that read an access the types could not place, fixes each `structure has no member named M` from its source line, and reports a use no source line spells (a macro expansion) as a blocking finding. `--no-settle` (or `--no-apply`) keeps the old stop at an access of no known type.

## Bounded data evidence and private ledgers

Data aliases support caller evidence only when their positive byte width fits one storage
record with the same ROM/VRAM offset in every version shared by the reader and storage.
A pool defined in only one version is joined only to that version’s assembly reads.
That join proves read evidence, not interchangeable C declarations. Naming a data storage owner or alias renames
only that identity; other typed views retain their identities and byte offsets. Function entry-point spellings still
follow the function across versions. Overlapping owners, unknown
widths, missing versions and divergent offsets remain unproved. Symbol extents and direct MIPS memory
operands supply byte widths; an untyped address is not a proved access. String evidence reads only
terminated printable bytes inside the datum's own checked ROM extents, common to every
holding version. A datum needs no reader to cite its own string, and cannot cite another
datum's string merely because a function loads both.

Use `recipe --project TREE name submit ROWS --ledger LANE --ledger-dir DIR --json` to
validate and queue proposals into a private directory without applying source names or
writing the shared ledger. `name status --ledger-dir DIR --json` reads the same namespace.
Relative ledger directories resolve from the invocation directory.

`name status` includes each original `proposal` alongside its ledger disposition for reproducible reconciliation.
Resubmitting an already queued proposal returns its original sequence and owner. For proposals without
an address or member key, only the complete identical proposal envelope identifies that existing row;
a shared requested name alone never does. Changed targets, scopes or evidence do not inherit its ownership.
Duplicate matching owners refuse as ambiguous. Inflight registrations remain visible but cannot be selected
for another batch. Neither registration nor selection rewrites the original row or ledger history.

Ledger submission also returns complete `registrations` and a `selection` object. Each
selector binds the absolute ledger directory and the immutable sequence, original lane,
key, name and complete stored envelope. Already queued rows retain their original
owner; newly accepted rows receive ordinary ledger sequences. Refused, ambiguous,
inflight, already applied and suggested proposals remain explicit dispositions. The
selection contains only uniquely registered queued rows; check every registration before
using it as the reviewed set. Lists are not truncated.

The existing train batch applier accepts `names_batch.py make --selection FILE`. Save the
returned selection object as FILE, without editing row identities. It validates the entire
set, then uses the existing batch cap. Its complete dispositions identify selected rows
left queued by the cap. After the first batch lands, pass those deferred selectors in the
same ledger envelope to the next make. Never substitute a sequence range. A private
fixture uses `names_batch.py --ledger-dir DIR make --selection FILE --tree CLEAN_TREE`;
TREE must be a dedicated clean batch workspace with its ordinary `github/main` remote.
The production train still owns rebuild and receipt settlement. Repeated settlement of
the same attempt is idempotent; conflicting outcomes refuse. No caller writes events.

To retire a reviewed queued proposal, use `recipe name return SELECTION.json --reason TEXT
--ledger-dir DIR --json`. The file uses the same complete immutable selection envelope
as batch make. The command validates every owner and row before appending one ordinary
`returned` event with the review reason and selection. Original ledger rows and events
remain unchanged; unrelated rows stay queued. An active batch, foreign or stale selection,
unknown or duplicate sequence, ambiguous owner, or inflight/landed row refuses the whole
request. Repeating the same review returns the existing disposition without another event;
a different review reason or subsequent disposition refuses. Corrected proposals are then
admitted normally by `name submit`, with their own immutable rows. This operation never
fabricates a batch or train receipt and never changes source files.


### Complete tracked declaration inventory

`recipe name count --steps C --json` reads all tracked source/header paths once through the existing
Unbake fact API. Its nine declaration domains are function, global, static, type, member, parameter,
local, macro and enumerator. Each identity includes the path, kind, enclosing scope offsets and
exact declaration offset; names alone never merge shadowed bindings. Generated include guards are
macro identities with the current path-derived spelling. Source and header directory views derive
from that same path universe, so numbered header folders cannot disappear into source-folder totals.
The result pins HEAD and each input hash, reports parser limits and never claims semantic acceptance.
C is a whole-tree census step; it refuses target filters or mixing with evidence-pack steps.

Local naming packs continue to select only placeholder names. Their facts now include all declarations;
name-only proposals with shadowed declarations refuse and remain explicit census debt. A ledger batch
replays the admitted envelope, including reviewed correction intent and target; a correction whose
canonical identity now has a different target refuses as stale. Successful repeated corrections remain
already-applied, and ledger ownership/history are unchanged.
