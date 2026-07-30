-- kiro-session-index schema
--
-- Design decisions encoded here (see README for the reasoning):
--   * FTS tables are contentless (content='') to avoid storing text twice, since the
--     original text must be kept anyway for LIKE fallback and snippet generation.
--   * contentless_delete=1 is REQUIRED: plain contentless tables reject DELETE, which
--     the per-file incremental strategy depends on.
--   * Tool output is content-addressed in tool_output so identical output is indexed
--     once but still discoverable from every session that produced it.
--   * (message_id, content_index) is the stable citation anchor. Rowids are NOT stable
--     across re-indexing; message_id is a native UUID and never changes.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per source .jsonl. mtime+size is the incremental cursor: coarse on purpose.
-- A changed file has its whole session deleted and re-parsed, so the result is always
-- identical to a full rebuild. There is deliberately no byte offset to drift.
CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    cwd               TEXT,
    project           TEXT,
    title             TEXT,
    created_at        TEXT,
    updated_at        TEXT,
    created_reason    TEXT,   -- 'subagent' marks a spawned session
    parent_session_id TEXT,   -- links a subagent session to its parent
    model             TEXT,
    agent_name        TEXT
);

-- Prose: user turns, assistant text, thinking text, and tool-call purposes.
-- Excluded by design: thinking.signature (crypto blob), Compaction.messages_snapshot
-- (duplicate of earlier messages), turn.result (duplicate message content),
-- and file bodies inside toolUse.input (kept in tool_calls.input_json, not indexed).
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    session_id    TEXT NOT NULL,
    message_id    TEXT,               -- native UUID; NULL only for Clear markers
    content_index INTEGER NOT NULL,   -- position within the record's content[]
    seq           INTEGER NOT NULL,   -- ordering within the session
    kind          TEXT,               -- Prompt | AssistantMessage | Clear
    role          TEXT,               -- user | assistant
    content_type  TEXT,               -- text | thinking | toolUse | clear
    text          TEXT,
    ts            INTEGER             -- epoch SECONDS, carried forward from Prompt
);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    message_id  TEXT,
    seq         INTEGER,
    name        TEXT,
    purpose     TEXT,   -- __tool_use_purpose: agent-written intent, high-value target
    path        TEXT,   -- path/file_path argument, for file-history queries
    input_json  TEXT    -- full input, stored but NOT full-text indexed
);

-- Content-addressed tool output: indexed once per unique payload.
CREATE TABLE IF NOT EXISTS tool_output (
    id     INTEGER PRIMARY KEY,
    sha1   TEXT NOT NULL UNIQUE,
    output TEXT NOT NULL
);

-- One row per tool result, pointing at the shared payload. FileRead results are
-- skipped entirely: they duplicate files that still exist on disk, where ripgrep
-- searches them better and fresher.
CREATE TABLE IF NOT EXISTS tool_results (
    tool_use_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    message_id  TEXT,
    tool_name   TEXT,
    status      TEXT,   -- success | error, for "which commands failed" queries
    output_id   INTEGER NOT NULL REFERENCES tool_output(id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    search_text,
    tokenize = 'unicode61',
    content = '',
    contentless_delete = 1
);

CREATE VIRTUAL TABLE IF NOT EXISTS tool_output_fts USING fts5(
    search_text,
    tokenize = 'unicode61',
    content = '',
    contentless_delete = 1
);

-- Citation lookups. Deliberately NOT UNIQUE: message_id uniqueness is a property of
-- the upstream logs (measured: 24,083 records, zero duplicates, no cross-session
-- collisions), not an invariant this tool controls. A UNIQUE constraint would turn a
-- benign upstream quirk into a failed index update, and INSERT OR REPLACE would
-- silently drop a record. Uniqueness is asserted in the integration tests instead.
CREATE INDEX IF NOT EXISTS ix_msg_citation ON messages(message_id, content_index);
CREATE INDEX IF NOT EXISTS ix_msg_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS ix_msg_ts      ON messages(ts);
CREATE INDEX IF NOT EXISTS ix_tc_session  ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS ix_tc_path     ON tool_calls(path);
CREATE INDEX IF NOT EXISTS ix_tc_name     ON tool_calls(name);
CREATE INDEX IF NOT EXISTS ix_tr_session  ON tool_results(session_id);
CREATE INDEX IF NOT EXISTS ix_tr_output   ON tool_results(output_id);
CREATE INDEX IF NOT EXISTS ix_sess_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS ix_sess_proj   ON sessions(project);
