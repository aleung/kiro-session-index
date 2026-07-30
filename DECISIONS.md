# Design decisions

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Confirmed with the user](#confirmed-with-the-user)
- [Decided autonomously — please review](#decided-autonomously--please-review)
  - [1. Citation index is `UNIQUE`, with per-file failure isolation](#1-citation-index-is-unique-with-per-file-failure-isolation)
  - [2. Schema version triggers an automatic rebuild](#2-schema-version-triggers-an-automatic-rebuild)
  - [3. `status` column on `tool_results`](#3-status-column-on-tool_results)
  - [4. `Clear` records stored as boundary markers](#4-clear-records-stored-as-boundary-markers)
  - [5. Skill named `session-history`, not `kiro-session-index`](#5-skill-named-session-history-not-kiro-session-index)
  - [6. Wrapper query syntax kept deliberately small](#6-wrapper-query-syntax-kept-deliberately-small)
  - [7. Two integration tests loosened to avoid false alarms](#7-two-integration-tests-loosened-to-avoid-false-alarms)
- [Open, deliberately deferred](#open-deliberately-deferred)
- [Corrections to the original handoff](#corrections-to-the-original-handoff)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## Confirmed with the user

Settled in a grilling session on 2026-07-30, one question at a time.

| # | Decision | Choice |
|---|---|---|
| 1 | Primary consumer | Agent retrieval mid-session; derived-memory-with-provenance as a bonus, so the schema carries citation anchors |
| 2 | Privacy control | At-rest hardening only — `0600`, outside git. No index-time filtering; snippets returned freely |
| 3 | Index scope | Two FTS tables (prose / tool output); `FileRead` excluded; sha1 deduplication |
| 4 | Query interface | Thin wrapper as the sole SQL entry point — shape unrestricted, path fixed |
| 5 | Code substrings | No trigram index; `LIKE` scan as fallback |
| 6 | Refresh | Per-file incremental keyed on mtime, plus a staleness check at query time |
| 7 | Storage | Contentless FTS + `contentless_delete=1`; originals stored; `snip()` for snippets |
| 8 | Runtime | python3 (zero dependencies) |
| 9 | Location | `~/.cache/kiro-session-index/`, `0600` including `-wal`/`-shm` |
| 10 | Usage analytics | Not built — no cost data exists, 23% coverage would mislead |
| 11 | Semantic layer | Not built; revisit only if keyword search demonstrably falls short |
| 12 | Skill location | Global `~/.kiro/skills/` |

## Decided autonomously — please review

Made while the user was asleep, per instruction to proceed and record.
None of these contradict the twelve decisions above; each resolves a detail that only
surfaced during implementation.

### 1. Citation index is `UNIQUE`, with per-file failure isolation

**This entry previously recorded the opposite decision. It was wrong, and the original
reasoning was based on a false premise.**

What actually happened: `test_reindex_after_append_does_not_duplicate` crashed on the
`UNIQUE` constraint. The cause was a bug in the *test fixture* — `append_line()` wrote a
hardcoded `message_id`, so appending twice produced two records sharing an id. Real logs
never do this: measured 24,207 records carrying a `message_id`, 24,207 distinct, zero
duplicates, zero cross-session collisions.

I fixed the fixture *and* demoted the constraint. Only the first was warranted.

The reasoning error was mistaking what the constraint protects against. Its real value is
catching bugs in **this** code: if `drop_session()` ever fails to clear a session before
re-inserting it, the constraint fires immediately, instead of silently double-indexing
content and producing duplicate hits with inflated counts. Removing it traded a guard
against a likely failure for immunity to a measured-zero one — and the failure it guards
against is precisely the silent kind this project exists to eliminate.

**Decision:** `ux_msg_citation` is `UNIQUE`. To avoid the original concern (one anomaly
aborting everything and blocking all queries), each file is its own unit of work: a
violation rolls back and drops only that session, reports it, and the remaining sessions
still update. `ksi-index` exits non-zero and names the failed sessions.

Rollback safety needed one extra piece: `Indexer.resync()`. The `sha1 -> id` cache for
content-addressed tool output would otherwise survive a rollback pointing at rows that no
longer exist, silently corrupting every file indexed afterwards. There is a test for that.

Verified: full rebuild of 853 sessions under the constraint gives `files_failed: 0`.

Also note a side effect: the integration suite calls `update()` against the real default
index, so running the tests refreshes it. Harmless — the operation is idempotent — but it
means the tests are not side-effect free.

### 2. Schema version triggers an automatic rebuild

`CREATE TABLE IF NOT EXISTS` cannot migrate an existing database,
so a schema change would otherwise leave a subtly wrong index in place, silently.

**Decision:** `meta.schema_version` is compared on every run;
a mismatch, or an unreadable file, forces a full rebuild.
Cheap to do because a rebuild is 2.7 s.
Consistent with the project's stance of making silent staleness impossible.

### 3. `status` column on `tool_results`

Not discussed during grilling.
Each result records `Success` or `Error`, which makes
"which commands failed, and what were they trying to do" answerable.
Costs one small column.

### 4. `Clear` records stored as boundary markers

Retained as rows with `content_type='clear'`, no text, and no FTS entry,
which preserves conversation-reset boundaries in sequence order at negligible cost.
The alternative — dropping them — would lose segment information permanently.

### 5. Skill named `session-history`, not `kiro-session-index`

The repo keeps the descriptive name.
The skill is named for what the user is asking about,
since the skill name is matched against intent.

### 6. Wrapper query syntax kept deliberately small

`AND` by default, `"phrase"`, `prefix*`, `-negation`.
No `OR`, no field-scoped search, no parentheses.
Rationale: every syntax feature is a new way for a query to silently mean something
other than intended.
Anything more expressive is available through `--sql`.

### 7. Two integration tests loosened to avoid false alarms

Both failed for reasons that were artifacts of testing against a live corpus,
not product defects.

**Staleness check** asserted zero stale files, but the session running the tests is being
appended to continuously and is stale within seconds.
Now asserts that no session dormant for 5+ minutes is stale.

**Broken-phrase-transform check** asserted the wrong form finds zero rows.
It found 3 — because this session's own transcript contains the literal text `记忆 memory`
from the discussion of that very bug.
Now asserts the durable property: per-term `AND` finds strictly more than the phrase form.

## Open, deliberately deferred

Both are zero-cost to add later — neither touches existing schema or decisions —
so each waits for evidence rather than speculation.

**Trigram index for code substrings.**
Add if 30 ms substring scans become annoying, or the corpus reaches ~500 MB.
Cost measured: 3.97× raw size.

**Semantic layer over exported prose.**
Add if searches repeatedly miss things known to exist — the paraphrase gap
(asking about "自动过期" when the conversation said "衰减机制").
If added, index prose only; tool output would swamp it.

## Corrections to the original handoff

The handoff's central estimate was accurate — it predicted prose at ~8.6% of raw bytes,
measured 8.49%.
These points needed correction.

Would have caused silent wrong answers:

1. `detail=column` does **not** support phrase queries; FTS5 requires `detail=full`.
2. The appendix's query transform wraps whole queries in one phrase,
   which silently returns 0 rows for any multi-term query.
3. `metering_usage` is an empty array in all 1,903 turns — there is no cost data,
   so "cost analytics come for free" does not hold.

Factual corrections:

4. `Prompt` timestamp coverage is 99.1%, not ~57%.
5. `agent_name` sits in `session_state`, not at the sidecar's top level.
   `parent_session_id` exists on 204 sessions and was unmentioned — it links subagent
   sessions to their parents.
6. `turn_duration` is `{secs, nanos}`, not a number.
7. The scale worry was unfounded: no superlinear degradation to 100 MB, 44 ms worst case.
8. `toolUse.input` is mostly file bodies (7.66 of 8.1 MB); only `__tool_use_purpose` is prose.

Omissions:

9. A `Compaction` record kind exists.
10. There are **three** duplication sources, not one:
    the tool-output double encoding, `Compaction.messages_snapshot`, and `turn.result`.
