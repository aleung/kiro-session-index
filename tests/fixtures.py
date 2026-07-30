"""Synthetic session-log fixtures.

Tests must never depend on the real corpus: it is sensitive, and it changes under
you as sessions accumulate. These fixtures reproduce every structural feature the
indexer has to cope with -- including the three duplication sources and the
crypto blob that must never be stored.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SIG = "SIGNATURE_BLOB_MUST_NEVER_BE_INDEXED_xxxxxxxxxxxxxxxxxxxxxx"
DUPLICATE_OUTPUT = "error: connection refused on port 5480"


def _rec(kind, data):
    return json.dumps({"version": "1", "kind": kind, "data": data}, ensure_ascii=False)


def session_lines(session_id, ts=1776350561):
    """A session exercising every record kind and content kind."""
    return [
        _rec("Prompt", {
            "message_id": f"{session_id}-m1",
            "meta": {"timestamp": ts},
            "content": [{"kind": "text", "data": "请帮我看看遗忘机制的设计"}],
        }),
        _rec("AssistantMessage", {
            "message_id": f"{session_id}-m2",
            "content": [
                {"kind": "thinking", "data": {"text": "用户在问记忆系统", "signature": SIG}},
                {"kind": "text", "data": "好的，我先看 DatabaseSync 的实现"},
                {"kind": "toolUse", "data": {
                    "toolUseId": f"{session_id}-t1",
                    "name": "shell",
                    "input": {
                        "__tool_use_purpose": "Check the decay mechanism in memory code",
                        "command": "grep -r decay .",
                        # A file body: stored in input_json, never full-text indexed.
                        "content": "BODY_OF_A_WRITTEN_FILE_SHOULD_NOT_BE_SEARCHABLE",
                    },
                }},
            ],
        }),
        _rec("ToolResults", {
            "message_id": f"{session_id}-m3",
            "results": {
                f"{session_id}-t1": {
                    "tool": {"kind": {"BuiltIn": {"ExecuteCmd": {
                        "command": "grep -r decay ."}}}},
                    "result": {"Success": {"items": [{"Text": DUPLICATE_OUTPUT}]}},
                },
                f"{session_id}-t2": {
                    "tool": {"kind": {"BuiltIn": {"FileRead": {"path": "/etc/hosts"}}}},
                    "result": {"Success": {"items": [
                        {"Text": "FILEREAD_PAYLOAD_MUST_BE_SKIPPED"}]}},
                },
            },
            # The model-facing mirror of the same payload: must not be indexed twice.
            "content": [{"kind": "toolResult", "data": {
                "toolUseId": f"{session_id}-t1",
                "content": [{"kind": "text", "data": DUPLICATE_OUTPUT}],
            }}],
        }),
        _rec("Clear", {"content": []}),
        _rec("Compaction", {
            # A duplicate snapshot of earlier messages: must be skipped wholesale.
            "messages_snapshot": [
                {"content": [{"kind": "text", "data": "COMPACTION_DUPLICATE_TEXT"}]}],
        }),
    ]


def write_corpus(root, n_sessions=3):
    """Create n synthetic sessions (.jsonl + .json sidecar). Returns session ids."""
    os.makedirs(root, exist_ok=True)
    ids = []
    for i in range(n_sessions):
        sid = f"sess{i:04d}"
        ids.append(sid)
        with open(os.path.join(root, f"{sid}.jsonl"), "w", encoding="utf8") as f:
            f.write("\n".join(session_lines(sid, ts=1776350561 + i * 3600)) + "\n")
        sidecar = {
            "session_id": sid,
            "cwd": f"/home/u/projects/proj{i % 2}",
            "title": f"synthetic session {i}",
            "created_at": "2026-04-16T14:42:41Z",
            "updated_at": "2026-04-16T15:00:00Z",
            "parent_session_id": ids[0] if i > 0 else None,
            "session_created_reason": "subagent" if i > 0 else None,
            "session_state": {
                "agent_name": "test-agent",
                "rts_model_state": {"model_info": {"model_name": "claude-test"}},
                # Must be ignored entirely: no cost data exists here.
                "conversation_metadata": {"user_turn_metadatas": [
                    {"metering_usage": [], "input_token_count": 0,
                     "turn_duration": {"secs": 5, "nanos": 0},
                     "result": {"Ok": {"content": [
                         {"kind": "text", "data": "TURN_RESULT_DUPLICATE"}]}}}
                ]},
            },
        }
        with open(os.path.join(root, f"{sid}.json"), "w", encoding="utf8") as f:
            json.dump(sidecar, f)
    return ids


class Corpus:
    """Context manager giving a throwaway corpus dir and index path."""

    def __init__(self, n_sessions=3):
        self.n = n_sessions

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "cli")
        self.db = os.path.join(self.tmp.name, "index", "index.db")
        self.ids = write_corpus(self.root, self.n)
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()
        return False

    def append_line(self, session_id, text="新增的一句话 appended"):
        """Append a user turn, simulating an in-progress session growing."""
        p = os.path.join(self.root, f"{session_id}.jsonl")
        self._appends = getattr(self, "_appends", 0) + 1
        with open(p, "a", encoding="utf8") as f:
            f.write(_rec("Prompt", {
                "message_id": f"{session_id}-appended{self._appends}",
                "meta": {"timestamp": 1776360000},
                "content": [{"kind": "text", "data": text}],
            }) + "\n")
        # Force a visible mtime change even on coarse-grained filesystems.
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 2))
