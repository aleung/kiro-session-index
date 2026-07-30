---
name: session-history
description: "Search past Kiro CLI sessions to recover earlier decisions, discussions, commands, and errors. Use when the user asks whether something was discussed or decided before, when a topic last came up, whether an error has been seen previously, which past sessions touched a file, or refers back to an earlier conversation. Triggers: 'did we discuss', 'have we decided', '以前是不是', '之前讨论过', 'when did I', 'have I seen this error', 'what did we say about', 'which session', 'earlier conversation', 'last time we'."
---

# Session History

A local SQLite index over `~/.kiro/sessions/cli/*.jsonl` — every past session, searchable.
Use it to recover *why* something was decided, not just what the current memory files summarise.

Repo: `~/projects/personal/kiro-session-index`
Index: `~/.cache/kiro-session-index/index.db` (auto-refreshed, `0600`, outside any git repo)

## Always query through `ksi-query`

```bash
cd ~/projects/personal/kiro-session-index
./ksi-query "记忆 遗忘"            # prose: user turns, assistant text, thinking, tool purposes
./ksi-query -t "connection refused" # tool output: command results, transient errors
./ksi-query -l "abaseSy"            # exact substring (code fragments MATCH cannot find)
./ksi-query --sql "SELECT ..."      # arbitrary SQL
./ksi-query --status                # counts and freshness
```

Never open the database directly with `sqlite3`. The wrapper is the only entry point
because it does three things raw SQL cannot, each preventing a *silent* wrong answer:

1. **Transforms the query.** CJK is indexed character-by-character. An untransformed
   Chinese query returns zero rows and looks like "we never discussed it".
2. **Refreshes the index.** Checks source mtimes first, so the session you are in right
   now is searchable within seconds.
3. **Provides `snip()` and blocks `snippet()`.** See the traps below.

Useful flags: `-n` limit, `-w` snippet width, `--json`, `--project`, `--session`,
`--role user|assistant`, `--since YYYY-MM-DD`, `--tool-status error`,
`--with TOKEN` (whole-word prefilter that makes `-l` sub-millisecond), `--no-update`.

## Query syntax

| Input | Meaning |
|---|---|
| `记忆 memory` | both terms present (AND) |
| `"遗忘机制"` | exact phrase |
| `token*` | prefix match |
| `memory -pipeline` | first present, second absent |

Chinese matching is substring-like within a run: `忘机` matches `遗忘机制`.

## Schema

```
sessions(session_id, cwd, project, title, created_at, updated_at,
         created_reason, parent_session_id, model, agent_name)
messages(id, session_id, message_id, content_index, seq, kind, role,
         content_type, text, ts)          -- content_type: text|thinking|toolUse|clear
tool_calls(tool_use_id, session_id, message_id, seq, name, purpose, path, input_json)
tool_output(id, sha1, output)             -- content-addressed, deduplicated
tool_results(tool_use_id, session_id, message_id, tool_name, status, output_id)
messages_fts(search_text)                 -- contentless; rowid = messages.id
tool_output_fts(search_text)              -- contentless; rowid = tool_output.id
files(path, session_id, mtime, size, indexed_at)
```

`ts` is epoch **seconds**, carried forward from the preceding user turn (assistant
records carry no timestamp of their own).

**Citation anchor:** `message_id:content_index`. Stable across rebuilds — cite this when
recording a conclusion that came from a past session. Rowids are not stable.

## Query patterns

```bash
# which past sessions touched a file
./ksi-query --sql "SELECT s.created_at, s.title, t.name, t.path
  FROM tool_calls t JOIN sessions s USING(session_id)
  WHERE t.path LIKE '%nightly-consolidation%' ORDER BY s.created_at"

# commands that failed, and what they were trying to do
./ksi-query --sql "SELECT s.project, c.purpose, r.tool_name FROM tool_results r
  JOIN tool_calls c USING(tool_use_id) JOIN sessions s ON s.session_id=r.session_id
  WHERE r.status='error' LIMIT 20"

# what a subagent was asked to do
./ksi-query --sql "SELECT p.title AS parent, c.title AS child, c.agent_name
  FROM sessions c JOIN sessions p ON p.session_id=c.parent_session_id"

# readable snippets inside your own SQL
./ksi-query --sql "SELECT s.title, snip(m.text,'索引',30) FROM messages m
  JOIN sessions s USING(session_id) WHERE m.text LIKE '%索引%' LIMIT 5"
```

## Three traps

**`snippet()` and `highlight()` are blocked.** On a contentless FTS table they return an
empty string *without erroring*. Use `snip(text, query [, width])`, which slices the
original text — and is the only way to get readable Chinese, since the FTS index holds
character-split text (`snippet()` would yield `知  识  库`).

**Code substrings need `-l`.** `unicode61` indexes `DatabaseSync` as one token, so
`getUserName` is findable but `UserName` is not. Whole identifiers via normal search
(100% recall, camelCase and snake_case); inner fragments via `-l` (~30 ms full scan).

**Two corpora, chosen deliberately.** Prose ranks cleanly because tool output is in a
separate table. "Why did we decide X" → prose. "Have I seen this error" → `-t`.
`FileRead` output is *not* indexed: those files still exist on disk, so use ripgrep.

## Rebuilding

```bash
./ksi-index          # incremental (~0.3 s); normally automatic
./ksi-index --full   # from scratch (~3 s for 853 sessions)
```

Losing the index is harmless — it is a derived cache and rebuilds from the immutable
logs. The source under `~/.kiro/sessions/` is never written to.
