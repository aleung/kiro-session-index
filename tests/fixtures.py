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

    def duplicate_citation(self, session_id):
        """Append a record reusing an existing (message_id, content_index).

        Real logs never do this -- 24,207 records carrying a message_id, zero
        duplicates -- but the indexer must degrade gracefully if they ever did. The
        same violation is what would surface a drop_session() bug, which is the
        constraint's real purpose.
        """
        p = os.path.join(self.root, f"{session_id}.jsonl")
        with open(p, "a", encoding="utf8") as f:
            f.write(_rec("Prompt", {
                "message_id": f"{session_id}-m1",   # collides with the first record
                "meta": {"timestamp": 1776360000},
                "content": [{"kind": "text", "data": "重复的引用锚点 duplicate anchor"}],
            }) + "\n")
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 2))


# ---------- v3 layout ----------
#
# <root>/<hash>/sess_<uuid>/messages.jsonl + session.json, and sub-agent transcripts
# in sub-executions/<uuid>.jsonl. Records are {id, timestamp, payload}; payload.type
# names the event. Mixed with a v2 corpus under <root>/cli in MixedCorpus.

V3_SIG = "V3_REASONING_SIGNATURE_MUST_NEVER_BE_INDEXED_xxxxxxxxxxxx"
V3_HASH = "0123456789abcdef"
V3_SUB_ID = "9f000000-0000-4000-8000-000000000001"


def _v3(rid, ts, payload):
    return json.dumps({"id": rid, "timestamp": ts, "payload": payload},
                      ensure_ascii=False)


def v3_session_lines(sid, with_subagent=False):
    """A v3 session exercising every event type the indexer reads or must skip."""
    t = "2026-11-07T03:18:%02d.000Z"
    lines = [
        _v3(f"{sid}-start", t % 0, {"type": "session_start", "agentType": "test-agent",
            "content": "V3_SYSTEM_PROMPT_MUST_NOT_BE_INDEXED", "messageId": "x"}),
        _v3(f"{sid}-note", t % 1, {"type": "agent_note",
            "content": "V3_INJECTED_CONTEXT_MUST_NOT_BE_INDEXED"}),
        _v3(f"{sid}-u1", t % 2, {"type": "user", "content": "请帮我看看遗忘机制的设计"}),
        _v3(f"{sid}-ts", t % 2, {"type": "turn_start", "turnId": "t1"}),
        _v3(f"{sid}-r1", t % 3, {"type": "assistant", "operationType": "Reasoning",
            "content": "用户在问记忆系统", "reasoningSignature": V3_SIG}),
        _v3(f"{sid}-a1", t % 4, {"type": "assistant", "operationType": "Say",
            "content": "好的，我先看 DatabaseSync 的实现"}),
        _v3(f"{sid}-c1", t % 5, {"type": "tool_call", "toolCallId": f"{sid}-t1",
            "toolName": "execute_bash", "title": "Check decay",
            "args": {"command": "grep -r decay .",
                     "description": "Check the decay mechanism in memory code"}}),
        _v3(f"{sid}-o1", t % 6, {"type": "tool_result", "toolCallId": f"{sid}-t1",
            "content": DUPLICATE_OUTPUT, "success": False}),
        _v3(f"{sid}-c2", t % 7, {"type": "tool_call", "toolCallId": f"{sid}-t2",
            "toolName": "read_file", "title": "Read File",
            "args": {"path": "/etc/hosts"}}),
        _v3(f"{sid}-o2", t % 8, {"type": "tool_result", "toolCallId": f"{sid}-t2",
            "content": "V3_READ_FILE_PAYLOAD_MUST_BE_SKIPPED", "success": True}),
        _v3(f"{sid}-c3", t % 9, {"type": "tool_call", "toolCallId": f"{sid}-t3",
            "toolName": "str_replace", "title": "Replace in file",
            "args": {"path": "/tmp/x.py", "oldStr": "a",
                     "newStr": "V3_FILE_BODY_SHOULD_NOT_BE_SEARCHABLE"}}),
        _v3(f"{sid}-usage", t % 10, {"type": "usage_summary", "status": "success"}),
        _v3(f"{sid}-te", t % 10, {"type": "turn_end", "stopReason": "end_turn"}),
    ]
    if with_subagent:
        lines += [
            _v3(f"{sid}-sa", t % 11, {"type": "sub_agent_start",
                "subSessionId": V3_SUB_ID, "subAgentName": "helper-agent",
                "prompt": "子代理的任务 fetch the build log",
                "explanation": "Delegate the log fetch"}),
            _v3(f"{sid}-sc", t % 20, {"type": "sub_agent_complete",
                "subSessionId": V3_SUB_ID, "status": "success",
                "response": "SUBAGENT_RESPONSE build log saved"}),
        ]
    return lines


def v3_sub_lines():
    t = "2026-11-07T03:18:%02d.000Z"
    return [
        _v3("sub-a1", t % 12, {"type": "assistant", "operationType": "Say",
            "content": "子代理正在获取日志"}),
        _v3("sub-c1", t % 13, {"type": "tool_call", "toolCallId": "sub-t1",
            "toolName": "execute_bash", "args": {"command": "curl x",
                                                  "description": "Fetch the log"}}),
        _v3("sub-o1", t % 14, {"type": "tool_result", "toolCallId": "sub-t1",
            "content": "SUBAGENT_TOOL_OUTPUT", "success": True}),
    ]


def write_v3_corpus(root, n_sessions=2):
    """Create n v3 sessions under root; the first one delegates to a sub-agent.

    Returns the top-level session ids.
    """
    ids = []
    for i in range(n_sessions):
        sid = f"sess_00000000-0000-4000-8000-00000000000{i}"
        ids.append(sid)
        d = os.path.join(root, V3_HASH, sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "messages.jsonl"), "w", encoding="utf8") as f:
            f.write("\n".join(v3_session_lines(sid, with_subagent=i == 0)) + "\n")
        with open(os.path.join(d, "session.json"), "w", encoding="utf8") as f:
            json.dump({"id": sid, "title": f"v3 session {i}",
                       "createdAt": "2026-11-07T03:18:00.000Z",
                       "lastModifiedAt": "2026-11-07T03:19:00.000Z",
                       "rootPaths": [f"/home/u/projects/v3proj{i}"],
                       "workspacePaths": [f"/home/u/projects/v3proj{i}"],
                       "modelId": "test-model", "agentMode": "test-agent",
                       "schemaVersion": "1.0.0", "dataModelVersion": 1}, f)
        if i == 0:
            sub = os.path.join(d, "sub-executions")
            os.makedirs(sub)
            with open(os.path.join(sub, V3_SUB_ID + ".jsonl"), "w", encoding="utf8") as f:
                f.write("\n".join(v3_sub_lines()) + "\n")
    return ids


class MixedCorpus:
    """A sessions root holding a v2 corpus under cli/ and a v3 corpus beside it."""

    def __init__(self, n_v2=2, n_v3=2):
        self.n_v2, self.n_v3 = n_v2, n_v3

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "sessions")
        self.db = os.path.join(self.tmp.name, "index", "index.db")
        self.v2_ids = write_corpus(os.path.join(self.root, "cli"), self.n_v2)
        self.v3_ids = write_v3_corpus(self.root, self.n_v3)
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()
        return False

    def v3_log(self, sid):
        return os.path.join(self.root, V3_HASH, sid, "messages.jsonl")

    def append_v3(self, sid, text="新增的一句话 appended"):
        p = self.v3_log(sid)
        self._appends = getattr(self, "_appends", 0) + 1
        with open(p, "a", encoding="utf8") as f:
            f.write(_v3(f"{sid}-appended{self._appends}", "2026-11-07T04:00:00.000Z",
                        {"type": "user", "content": text}) + "\n")
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + 2))
