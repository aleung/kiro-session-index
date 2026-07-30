---
name: session-history
description: "Load when the user asks what was discussed or decided before, references earlier work you lack context for, resumes previous work, or when knowing prior decisions would improve your answer. Triggers: 'did we discuss', '之前讨论过', '继续之前的', 'have I seen this error', 'last time we'."
metadata:
  version: "1.1.0"
---

# Session History

SQLite index over `~/.kiro/sessions/cli/*.jsonl`. Tools: `@TOOL_DIR@`.

Run every query through `ksi-query`. Never open the database with `sqlite3` — that
bypasses the CJK query transform, the freshness check, and the `snippet()` guard, each of
which fails silently rather than erroring.

```bash
@TOOL_DIR@/ksi-query "记忆 遗忘"             # prose: user turns, assistant text, thinking, tool purposes
@TOOL_DIR@/ksi-query -t "connection refused"  # tool output: command results, transient errors
@TOOL_DIR@/ksi-query -l "abaseSy"             # exact substring
@TOOL_DIR@/ksi-query --sql "SELECT ..."       # arbitrary SQL
@TOOL_DIR@/ksi-query --status                 # counts and freshness
@TOOL_DIR@/ksi-index --full                   # rebuild from scratch (~3 s)
```

Flags: `-n` limit, `-w` snippet width, `--json`, `--project`, `--session`,
`--role user|assistant`, `--since YYYY-MM-DD`, `--tool-name`, `--tool-status error`,
`--with TOKEN` (whole-word prefilter; makes `-l` sub-millisecond), `--no-update`.

## Query without being asked

Search first, do not ask the user to remind you, in these cases:

- The user refers to a decision, plan, or problem as already settled and you have no
  record of it in this session.
- The user resumes work: "继续之前的", "continue where we left off", "把那个做完".
- You are about to change a file with a long history, and the reason for its current
  shape is not evident from the code.
- You hit an error whose wording looks like something already encountered.
- The user's request contradicts what the code does, suggesting an earlier decision you
  cannot see.

Restraint: one bounded query, then answer. Do not open a session and read it through, and
do not run this on turns where nothing is being recalled. When a past decision changes
what you are about to do, say so and cite it.

## Retrieval discipline

Narrow before broadening: `--session` or `--project` or a `tool_calls.path` filter beats a
bare full-text query when you already know the scope. Empty scoped results are a real
answer.

Keep output small: `-n` and `-w` exist so synthesis stays cheap. Prefer counts and
groupings computed in `--sql` over reading many snippets.

Answer with citations (`message_id:content_index`), not with volume.

## Choosing the corpus

Prose and tool output are separate indexes. Query the wrong one and you get zero rows.

- "Why did we decide X", "what did we say about Y" → default (prose).
- "Have I seen this error", "what did that command print" → `-t`.
- Contents of a file that still exists → neither; use ripgrep. `FileRead` output is not indexed.

## Query syntax

| Input | Meaning |
|---|---|
| `记忆 memory` | both terms present (AND) |
| `"遗忘机制"` | exact phrase |
| `token*` | prefix match |
| `memory -pipeline` | first present, second absent |

Chinese matches substring-like within a run: `忘机` matches `遗忘机制`.

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

`ts` is epoch seconds. `created_reason='subagent'` marks spawned sessions.
`tool_calls.purpose` is the agent-written intent line — the highest-value search target.

Cite past sessions as `message_id:content_index`. Rowids change on re-index; that pair
does not.

## Query patterns

```bash
# which past sessions touched a file
@TOOL_DIR@/ksi-query --sql "SELECT s.created_at, s.title, t.name, t.path
  FROM tool_calls t JOIN sessions s USING(session_id)
  WHERE t.path LIKE '%nightly-consolidation%' ORDER BY s.created_at"

# commands that failed, and what they were trying to do
@TOOL_DIR@/ksi-query --sql "SELECT s.project, c.purpose, r.tool_name FROM tool_results r
  JOIN tool_calls c USING(tool_use_id) JOIN sessions s ON s.session_id=r.session_id
  WHERE r.status='error' LIMIT 20"

# what a subagent was asked to do
@TOOL_DIR@/ksi-query --sql "SELECT p.title AS parent, c.title AS child, c.agent_name
  FROM sessions c JOIN sessions p ON p.session_id=c.parent_session_id"

# readable snippets inside your own SQL
@TOOL_DIR@/ksi-query --sql "SELECT s.title, snip(m.text,'索引',30) FROM messages m
  JOIN sessions s USING(session_id) WHERE m.text LIKE '%索引%' LIMIT 5"
```

## Gotchas

<!-- Append here when the agent fails. Each entry prevents a class of errors. -->

- Never call `snippet()` or `highlight()` — `ksi-query` rejects them. On contentless FTS
  tables they return an empty string without erroring, and on a normal table they would
  return character-split text (`知  识  库`). Use `snip(text, query [, width])`.
- Zero rows for a Chinese query does not mean the topic is absent — it usually means the
  query bypassed `ksi-query`. Re-run through the wrapper before concluding anything.
- `MATCH` cannot find fragments inside an identifier: `DatabaseSync` is one token, so
  `getUserName` is findable but `UserName` is not. Use `-l` for inner fragments.
- Never conclude "we never discussed this" from a single failed query. Try the other
  corpus (`-t`), then `-l`, then a synonym. The conversation may have used different words.
- Absence of a `path` in `tool_calls` does not mean no file was touched; only `path` and
  `file_path` arguments are extracted.
- `ksi-index` exits non-zero when a session fails to index and names it; the rest of the
  index is still current. Do not treat a non-zero exit as "the index is broken".
- Do not add rows to the index by hand. It is a derived cache; the next refresh discards
  anything not present in the source logs.
