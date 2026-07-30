# Design

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Premise](#premise)
- [Layout](#layout)
- [Session log data shapes](#session-log-data-shapes)
- [Data model](#data-model)
- [Key design choices](#key-design-choices)
- [What is excluded, and why](#what-is-excluded-and-why)
- [The silent-failure stance](#the-silent-failure-stance)
- [Integrity](#integrity)
- [Testing](#testing)
- [Changing things](#changing-things)
- [Deferred, at zero retrofit cost](#deferred-at-zero-retrofit-cost)
- [Measured](#measured)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

Internals and rationale, for changing this code.
For usage see [README.md](README.md); for the agent-facing reference see
[skill/SKILL.md](skill/SKILL.md).

## Premise

Borrowed from [obelisk](https://github.com/tommy0103/obelisk) — see Credits in
[README.md](README.md).

Session JSONL is already a complete, immutable, append-only evidence layer.
Rather than distilling conversations into facts and letting the originals rot,
index the originals and query them.
A distilled memory then becomes a cache with provenance — invalidatable and re-derivable.

That framing drives two consequences that show up everywhere below.
The index is a **derived cache**, never a source of truth,
so losing it is a non-event and rebuilding is always allowed.
And retrieval must be **loud when it fails**,
because a search that silently returns nothing reads as "this never happened".

## Layout

```
ksi/text.py     query/index transforms, snippet extraction
ksi/index.py    parsing, incremental update, integrity
ksi/query.py    the single SQL entry point and its guards
ksi/schema.sql  schema, with rationale in comments
ksi-index       CLI: build/refresh
ksi-query       CLI: search
skill/SKILL.md  agent-facing reference, @TOOL_DIR@ substituted at install
setup.sh        installs a self-contained copy; repo unused at runtime
```

The skill is named `session-history`, not after the repo:
skill names are matched against user intent, and the user asks about session history.

## Session log data shapes

Reverse-engineered from 853 sessions / 116 MB. Re-verify if the upstream format changes.

**Byte composition.** Prose is only 8.5% of raw bytes (9.9 MB).
Tool output is 65% across two encodings, `thinking.signature` 4.9%.
This is why scale never became a problem: the searchable fraction is small.

**Record kinds:** `Prompt`, `AssistantMessage`, `ToolResults`, `Clear`, `Compaction`.
**Content kinds:** `text`, `thinking`, `toolUse`, `toolResult`.

**Timestamps.** Only `Prompt` carries `meta.timestamp`, on 99.1% of records,
in epoch **seconds**.
`AssistantMessage` and `ToolResults` carry none, so timestamps are carried forward from
the preceding user turn.

**Three duplication sources.** Each must be excluded or hits double:

1. `content[].toolResult` mirrors `results[tool_use_id]` — same payload, different wrapper.
2. `Compaction.messages_snapshot` is a copy of earlier messages.
3. `session_state...user_turn_metadatas[].result` is a copy of message content.

**Sidecar `<session-id>.json`.** Top-level keys are `session_id`, `cwd`, `created_at`,
`updated_at`, `title`, `session_state`, plus `session_created_reason` (248 sessions,
always `subagent`) and `parent_session_id` (204 sessions, which is what links a subagent
to its parent).
`agent_name` and `rts_model_state.model_info` live *inside* `session_state`, not at the
top level.

**No cost data exists.** `session_state.conversation_metadata.user_turn_metadatas` is
present on only 197 of 853 sessions, a ratio stable across months rather than growing.
Within those, `metering_usage` is an empty array in all 1,903 turns and token counts are
always 0.
`turn_duration` is `{secs, nanos}`, not a number.
So usage analytics are not merely unbuilt, they are unavailable — and an aggregate over a
23% sample would mislead without saying so.

**`toolUse.input` is mostly not prose.** 7.66 of 8.1 MB is `newStr`/`oldStr`/`content` —
file bodies.
Only `__tool_use_purpose` (0.48 MB) is written language, and it is the highest-value
search target: a one-line intent written per tool call.

## Data model

See `ksi/schema.sql`; the rationale lives in comments there. Semantics worth knowing:

`messages` holds prose only. `content_type` is `text`, `thinking`, `toolUse`
(the purpose line), or `clear`.
`Clear` records are kept as text-less boundary markers so conversation resets stay visible
in `seq` order; dropping them would lose segment information permanently.

`tool_output` is content-addressed by sha1 and `tool_results` references it.
32% of results are byte-identical, so this stores and indexes each payload once while
every producing session still finds it — deduplication without losing recall.

`tool_results.status` records `success` or `error`, which is what makes
"which commands failed, and what were they trying to do" answerable.

`files` is the incremental cursor: `(path, mtime, size)`.

## Key design choices

**Contentless FTS5, `content=''` with `contentless_delete=1`.**
Halves index size — 8.8 MB versus 21.2 MB on the prose corpus —
because the original text has to be stored anyway for the substring fallback,
making the FTS copy pure duplication.
`contentless_delete=1` is mandatory, not optional:
plain contentless tables reject `DELETE`, which the incremental strategy requires.

**Unigram CJK splitting.**
`unicode61` groups an entire CJK run into one token, which makes Chinese unsearchable.
Splitting per character at index time and querying with phrases gives substring semantics
(`忘机` matches `遗忘机制`), which is what CJK users expect.
Benchmarked against the alternatives: bigram is 25–50% faster on common terms but larger,
and the built-in `trigram` cannot match queries shorter than 3 characters,
which rules out most Chinese words.
Phrase queries require `detail=full` — `detail=column` rejects them.

**Per-file incremental, deliberately no byte offsets.**
The cursor is `(mtime, size)` per file; a changed file has its whole session dropped and
re-parsed, so the outcome always equals a full rebuild.
A byte-offset cursor would be faster but carries state that can drift out of sync and
silently drop records.
At 2.7 s for a full rebuild that trade buys nothing.
A test asserts incremental output equals full-rebuild output field by field.

The cost of file granularity is that a growing session is re-parsed in full on every
refresh: at 4 MB — a long working session — a refresh costs ~0.8 s rather than ~0.3 s.
Acceptable, and the ceiling is the largest single session rather than the corpus.

**Two FTS corpora.**
Prose and tool output are indexed separately so BM25 ranking over the 9.9 MB that matters
is not swamped by machine output.

**Substring search by scan, not by index.**
`unicode61` indexes `DatabaseSync` as one token, so inner fragments are unreachable via
`MATCH` — measured recall 1/285.
A `trigram` column fixes it completely (281/281) but costs 3.97× raw size,
more than the entire existing index, to turn a 30 ms scan into 0.3 ms.
`LIKE` scanning is used instead, with an optional whole-word FTS prefilter that brings it
to sub-millisecond.

**Query shape free, execution path fixed.**
`--sql` accepts arbitrary SQL because query shapes cannot be enumerated in advance.
But everything routes through `ksi-query`, because three failure modes are silent and can
only be prevented in code — see below.

**Query syntax kept small.** `AND` by default, `"phrase"`, `prefix*`, `-negation`.
No `OR`, no field scoping, no parentheses:
every syntax feature is another way for a query to silently mean something other than
intended, and anything more expressive is available through `--sql`.

## What is excluded, and why

| Excluded | Reason |
|---|---|
| `thinking.signature` | Cryptographic blob, 4.9% of all bytes. Never stored. |
| `content[].toolResult` | Duplicate encoding of `results[tid]`. |
| `Compaction.messages_snapshot` | Duplicate of earlier messages. |
| `user_turn_metadatas[].result` | Duplicate of message content. |
| `FileRead` output | 12.8 MB duplicating files that still exist on disk, where ripgrep searches them better and fresher. |
| `toolUse.input` file bodies | Code, not prose. Kept in `input_json`, not full-text indexed. |

`ExecuteCmd` output *is* indexed: command results are transient and exist nowhere else
once a session ends. That is the irreplaceable half of tool output.

## The silent-failure stance

Anything that returns a plausible wrong answer instead of an error is handled in code,
never documented as a caution.

| Failure | Symptom | Handling |
|---|---|---|
| Untransformed CJK query | 0 rows, reads as "never discussed" | `build_match()` transforms every query |
| Whole query as one phrase | 0 rows on any multi-term query | per-term transform joined with `AND` |
| Stale index | recent sessions missing, no warning | source mtimes checked before every query |
| `snippet()` on contentless FTS | returns `''` | rejected; `snip()` provided instead |
| Schema drift | subtly wrong index persists | `meta.schema_version` mismatch forces a rebuild |
| Double-indexed session | duplicate hits, inflated counts | `UNIQUE` citation index fires immediately |

The second row is worth remembering because it is easy to reintroduce:
wrapping an entire multi-word query in one phrase requires adjacency across the whole
input, so `记忆 memory` returns 0 rows where per-term `AND` returns 8.
There is a regression test.

`snip()` is not merely a workaround for contentless tables.
On a normal FTS table `snippet()` would return the *split* text,
rendering Chinese as `知  识  库` — unreadable.
Slicing the original is the only way to get a readable CJK snippet, so it would have been
needed regardless.

## Integrity

**Citation anchor** is `(message_id, content_index)`.
`message_id` is a native UUID, verified unique across 24,207 records with no cross-session
collisions.
The composite matters: one record can emit several content items,
so 21,148 indexed rows come from 14,520 distinct `message_id`s.
Rowids change on re-index; the anchor does not.

The anchor carries a `UNIQUE` index whose purpose is catching bugs in *this* code rather
than policing upstream data.
If `drop_session()` ever fails to clear a session before re-inserting it,
the constraint fires instead of silently double-indexing.

**Failure isolation.** So that one anomaly cannot block every query,
each file is its own unit of work.
A violation rolls back, drops only that session, and is reported by name;
the remaining sessions still update, and `ksi-index` exits non-zero.

Rollback needs `Indexer.resync()`: the `sha1 -> id` cache for content-addressed output
would otherwise survive a rollback pointing at deleted rows,
silently corrupting every file indexed afterwards.

**Never writes to `~/.kiro/sessions/`.** A test asserts source mtimes are unchanged.

## Testing

```bash
python3 -m unittest discover -s tests -t .
```

Unit tests run against synthetic fixtures in `tests/fixtures.py`,
which reproduce every structural feature of real logs —
all three duplication sources, the signature blob, `Clear` and `Compaction` records.
They never read real session content, so they are neither sensitive nor
dependent on a corpus that changes under them.

Integration tests assert acceptance criteria against the real corpus and skip cleanly when
it is absent.
Two cautions when adding to them:

- The corpus is **live**. The session running the tests is appended to continuously, so
  any assertion of the form "nothing is stale" is racy. Scope such assertions to sessions
  dormant for some minutes.
- The corpus is **self-referential**. It contains transcripts of developing this tool, so
  a test asserting "the broken form finds zero rows" can fail because the discussion of
  that bug is itself indexed. Assert durable relationships instead of absolute counts.

`tests/test_skill.py` enforces the create-skill checklist mechanically, including that the
schema block in `SKILL.md` matches the real columns — a drifted schema block would send
the agent writing SQL against columns that do not exist.

Integration tests call `update()` against the default index, so running the suite
refreshes the real index. Idempotent, but not side-effect free.

## Changing things

**Schema changes** require bumping `SCHEMA_VERSION` in `ksi/index.py`.
`CREATE TABLE IF NOT EXISTS` cannot migrate an existing file, so a mismatch — or an
unreadable database — forces a full rebuild instead of leaving a subtly wrong index.
There is no migration path by design; rebuilding costs 2.7 s.

**After any change**, re-run `setup.sh`.
It replaces `scripts/` wholesale, so a renamed or deleted module cannot linger and shadow
current code.

**Entry points set `sys.pycache_prefix`** before importing `ksi`,
because the install lives inside `~/.kiro`, a git repo with no `__pycache__` ignore rule.

## Deferred, at zero retrofit cost

Neither touches existing schema or decisions, so both wait for evidence.

**Trigram index for code substrings.**
Add if 30 ms substring scans become annoying, or the corpus approaches 500 MB.
Cost measured: 3.97× raw size.

**Semantic layer over exported prose.**
Add if searches repeatedly miss things known to exist — the paraphrase gap, asking about
"自动过期" when the conversation said "衰减机制".
If added, index prose only; tool output would swamp it.
It would also need its own freshness mechanism, which is a second thing to go stale.

## Measured

853 sessions / 116 MB of logs.

| | |
|---|---|
| Full rebuild | 2.7 s |
| Incremental update | ~0.3 s |
| Index size | 70 MB |
| Prose corpus | 9.9 MB (8.5% of raw) |
| Query latency | 0.05–2 ms typical |
| Worst case at 10× corpus | 44 ms |

Benchmarked to 100 MB of prose with no superlinear degradation —
the index/raw ratio *falls* as the corpus grows (2.12× at 10 MB, 1.72× at 100 MB).
