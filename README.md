# kiro-session-index

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Install](#install)
- [Use](#use)
- [Where things live](#where-things-live)
- [Measured on 853 sessions / 116 MB of logs](#measured-on-853-sessions--116-mb-of-logs)
- [Design decisions](#design-decisions)
- [Excluded from the index](#excluded-from-the-index)
- [The three silent failure modes](#the-three-silent-failure-modes)
- [Citations](#citations)
- [Tests](#tests)
- [Privacy](#privacy)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

A local, queryable index over Kiro CLI session logs.

The premise: session JSONL is already a complete, immutable, append-only evidence layer.
Rather than distilling conversations into facts and letting the originals rot,
index the originals and query them.
A distilled "memory" then becomes a cache with provenance — invalidatable and re-derivable.

Zero dependencies: python3 stdlib only.

## Install

```bash
./setup.sh                    # install and build the index
./setup.sh --no-index         # install only
./setup.sh --skill-dir DIR    # install elsewhere
```

`setup.sh` copies **everything needed** — `SKILL.md`, both entry points, and the `ksi`
package — into the skill directory,
following the convention of other skills here (code under `scripts/`).

**After setup, this repo is not used at runtime.**
The installed copy contains no reference back to it,
and `setup.sh` verifies that before finishing:
it greps the install for the repo path, checks no placeholder went unsubstituted,
confirms every expected file arrived,
and runs the installed `ksi-query` from `/` with `PYTHONPATH` cleared.

Re-run `setup.sh` after changing anything in the repo.
It replaces `scripts/` wholesale, so a renamed or deleted module cannot linger and shadow
current code.

## Use

```bash
~/.kiro/skills/session-history/scripts/ksi-query "记忆 遗忘"
```

Or from the repo during development:

```bash
./ksi-query "记忆 遗忘"             # prose search
./ksi-query -t "connection refused" # tool output search
./ksi-query -l "abaseSy"            # exact substring, for code fragments
./ksi-query --sql "SELECT ..."      # arbitrary SQL
./ksi-query --status                # counts and freshness
./ksi-index                         # refresh (normally automatic)
```

`ksi-query` is the only entry point.
Query *shape* is unrestricted — `--sql` takes anything —
but the execution *path* is fixed so that three silent failure modes cannot bite.

## Where things live

| What | Where | Why |
|---|---|---|
| Installed tools | `~/.kiro/skills/session-history/scripts/` | Self-contained: entry points plus the `ksi` package. The skill directory is the runtime location. |
| Skill doc | `~/.kiro/skills/session-history/SKILL.md` | Rendered from `skill/SKILL.md` with paths substituted. |
| Index | `~/.cache/kiro-session-index/index.db` | Purely derived; XDG cache semantics. Outside any git repo, so it can never be committed. `0600` including `-wal`/`-shm`. |
| Bytecode cache | `~/.cache/kiro-session-index/pycache/` | Redirected via `sys.pycache_prefix`, because the install sits inside `~/.kiro`, which is a git repo. |
| Source logs | `~/.kiro/sessions/cli/` | Read-only truth. Never written to. |
| This repo | `~/projects/personal/kiro-session-index` | Source of truth for development. Not needed at runtime. |

## Measured on 853 sessions / 116 MB of logs

| | |
|---|---|
| Full rebuild | 2.7 s |
| Incremental update | ~0.3 s |
| Index size | 70 MB |
| Prose corpus | 9.9 MB (8.5% of raw) |
| Query latency | 0.05–2 ms typical; 44 ms worst case at 10× this corpus |

Scale is not a concern.
Benchmarked to 100 MB of prose (10× the real corpus) with no superlinear degradation —
the index/raw ratio actually *falls* as the corpus grows.

## Design decisions

**Contentless FTS5.**
`content=''` with `contentless_delete=1`.
Halves index size (8.8 MB vs 21.2 MB on the prose corpus)
because the original text must be stored anyway for substring fallback.
`contentless_delete=1` is mandatory: plain contentless tables reject `DELETE`,
which the incremental strategy needs.

**Unigram CJK splitting.**
`unicode61` groups a whole CJK run into one token, making Chinese unsearchable.
Indexing character-by-character and querying with phrases gives substring semantics
(`忘机` matches `遗忘机制`).
Benchmarked against bigram and the built-in `trigram`:
bigram is 25–50% faster on common terms but larger;
`trigram` cannot match queries shorter than 3 characters,
which rules out most Chinese words.
Phrase queries require `detail=full`.

**Per-file incremental, no byte offsets.**
The cursor is `(mtime, size)` per file.
A changed file has its whole session deleted and re-parsed,
so the result is always identical to a full rebuild.
A byte-offset cursor would be faster but can drift out of sync and silently drop records;
at 2.7 s for a full rebuild, that trade is not worth making.
Verified by test: incremental output equals full-rebuild output.

**Two FTS corpora.**
Prose and tool output are indexed separately so that BM25 ranking over the 9.9 MB
that matters is not swamped by machine output.
`FileRead` results are excluded entirely — 12.8 MB duplicating files that still exist on
disk, where ripgrep searches them better and fresher.
`ExecuteCmd` output *is* indexed: command results are transient and exist nowhere else
once a session ends.

**Content-addressed tool output.**
32% of tool results are byte-identical.
They are stored and indexed once, but referenced from every session that produced them,
so deduplication costs no recall.

**Substring search by scan, not by index.**
`unicode61` indexes `DatabaseSync` as one token, so inner fragments are unfindable.
A `trigram` column would fix it (281/281 recall vs 1/285) but costs 3.97× the raw size —
more than the entire existing index — to turn a 30 ms scan into 0.3 ms.
`LIKE` scanning is used instead.
This is deferrable at zero cost: adding a trigram table later touches nothing else.

**No usage analytics.**
`session_state.conversation_metadata` is not read.
`metering_usage` is an empty array in all 1,903 turns — there is no cost data —
token counts are always 0, and coverage is a stable 23% of sessions,
so any aggregate would mislead from a biased sample.

## Excluded from the index

| Excluded | Reason |
|---|---|
| `thinking.signature` | Cryptographic blob, 4.9% of all bytes. Never stored. |
| `content[].toolResult` | Model-facing mirror of `results[tid]`; same payload, different wrapper. |
| `Compaction.messages_snapshot` | Duplicate copy of earlier messages. |
| `session_state...turn.result` | Duplicate copy of message content. |
| `FileRead` output | Files still on disk. |
| `toolUse.input` file bodies | `newStr`/`oldStr`/`content` are code, not prose. Kept in `input_json`, not full-text indexed. Only `__tool_use_purpose` is indexed. |

## The three silent failure modes

Each of these returns a plausible-looking wrong answer rather than an error,
which is why each is handled in code rather than documented as a caution.

| Failure | Symptom | Handling |
|---|---|---|
| Untransformed CJK query | 0 rows — reads as "never discussed" | `build_match()` transforms every query, per term, joined with `AND` |
| Stale index | Recent sessions missing, no warning | Source mtimes checked before every query |
| `snippet()` on contentless FTS | Returns `''` | Rejected outright; `snip()` provided, which also renders CJK readably |

The middle one has a corollary worth knowing:
transforming an entire multi-word query into a single phrase — as the original design
sketch did — silently breaks every multi-term query.
`记忆 memory` returned 0 rows instead of 8.
There is a regression test for exactly this.

## Citations

`message_id:content_index` is the stable anchor.
`message_id` is a native UUID: verified unique across 24,207 records with no cross-session
collisions.
The composite matters — one record can emit several content items,
so 21,148 indexed rows come from 14,520 distinct `message_id`s.
Rowids are *not* stable across re-indexing, so cite the anchor.

The anchor is enforced by a `UNIQUE` index.
Its purpose is catching bugs in *this* code rather than policing upstream data:
if `drop_session()` ever failed to clear a session before re-inserting it,
the constraint fires immediately
instead of silently double-indexing and inflating counts.

So that one anomaly cannot block every query,
each file is its own unit of work:
a violation rolls back and drops only that session, is reported by name,
and the other 852 still update.
`ksi-index` exits non-zero when any session fails.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

218 tests.
Unit tests run against synthetic fixtures that reproduce every structural feature
of real logs — including all three duplication sources and the signature blob —
so they never depend on real session content.
Integration tests assert acceptance criteria against the real corpus
and skip cleanly when it is absent.

## Privacy

The index contains session content, including sensitive material.
It is deliberately placed outside any git repository,
so "never committed" is structural rather than a thing to remember.
Permissions are `0600` on the database and its WAL sidecars, `0700` on the directory —
strictly better protected than the source logs, which are `0644`.
Nothing is ever transmitted anywhere.
