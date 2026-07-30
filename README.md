# kiro-session-index

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Install](#install)
- [Use](#use)
  - [Which mode to use](#which-mode-to-use)
  - [Search syntax](#search-syntax)
  - [Narrowing results](#narrowing-results)
- [Keeping it current](#keeping-it-current)
- [Where things live](#where-things-live)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)
- [More](#more)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

Search your past Kiro CLI sessions.
Every conversation, command, and error you have had with the agent, indexed and queryable —
so you can recover *why* something was decided, not just what a summary says about it.

Handles Chinese, English, and code. Zero dependencies: python3 stdlib only.

## Install

```bash
./setup.sh
```

Builds the index and installs a `session-history` skill, so the agent can search your
history on its own when you ask "did we discuss this before?".

After setup this repo is not needed at runtime — everything is copied into the skill
directory. Re-run `setup.sh` after pulling changes.

```bash
./setup.sh --no-index         # install without building the index
./setup.sh --skill-dir DIR    # install somewhere else
```

Optionally put the tools on your PATH:

```bash
ln -sf ~/.kiro/skills/session-history/scripts/ksi-query ~/.local/bin/ksi-query
ln -sf ~/.kiro/skills/session-history/scripts/ksi-index ~/.local/bin/ksi-index
```

## Use

Examples below use the short names, which assume the symlinks above.
Without them, use the full path `~/.kiro/skills/session-history/scripts/ksi-query`.

```bash
ksi-query "记忆 遗忘"              # search what was said
ksi-query -t "connection refused"  # search command output and errors
ksi-query -l "abaseSy"             # exact substring, for code fragments
ksi-query --status                 # how much is indexed, and is it current
```

Results show when it happened, which project, a readable snippet, and a citation you can
quote back.

### Which mode to use

| You want | Command |
|---|---|
| A past discussion or decision | default |
| An error or command output you saw before | `-t` |
| A fragment inside an identifier, like `UserName` in `getUserName` | `-l` |
| The contents of a file that still exists | none of these — use ripgrep |

### Search syntax

| Input | Meaning |
|---|---|
| `记忆 memory` | both terms present |
| `"遗忘机制"` | exact phrase |
| `token*` | starts with `token` |
| `memory -pipeline` | first present, second absent |

Chinese matches inside words: `忘机` finds `遗忘机制`.

### Narrowing results

```bash
ksi-query "pipeline" --project my-project   # one project
ksi-query "pipeline" --since 2026-07-01       # recent only
ksi-query "pipeline" --role user              # only what you said
ksi-query -t "error" --tool-status error      # only failed commands
ksi-query "pipeline" -n 30 -w 80              # more hits, wider snippets
ksi-query "pipeline" --json                   # machine-readable
```

Anything more specific can be asked in SQL:

```bash
ksi-query --sql "SELECT s.created_at, s.title, t.path
  FROM tool_calls t JOIN sessions s USING(session_id)
  WHERE t.path LIKE '%memory%' ORDER BY s.created_at"
```

## Keeping it current

Nothing to do — every query checks the source logs first and updates what changed,
which takes about 0.3 s. The session you are in right now is searchable within seconds.

To refresh or rebuild by hand:

```bash
ksi-index          # refresh
ksi-index --full   # rebuild from scratch, ~3 s
```

## Where things live

| What | Where |
|---|---|
| The index | `~/.cache/kiro-session-index/` |
| Installed tools and skill | `~/.kiro/skills/session-history/` |
| Your session logs (read-only) | `~/.kiro/sessions/cli/` |

The index is a cache. Deleting it is safe — the next query rebuilds it from your logs,
which are never modified.

## Privacy

The index contains your session content, including anything sensitive you discussed.

It is stored outside any git repository, so it cannot be committed by accident,
with `0600` permissions on the database and `0700` on its directory —
better protected than the source logs themselves, which are `0644`.
Nothing is ever sent anywhere.

## Troubleshooting

**A search returns nothing but you are sure you discussed it.**
Try the other corpus (`-t`), then `-l`, then a different wording.
The conversation may have used different words than your query.

**`ksi-index` exits non-zero.**
It names the sessions it could not index; the rest of the index is still current.

**`sh: set: Illegal option -o pipefail`.**
An older copy of `setup.sh`. Pull the current one; it runs correctly under
`./setup.sh`, `sh setup.sh`, and `bash setup.sh` alike.

## Credits

Inspired by [obelisk](https://github.com/tommy0103/obelisk) by
[@tommy0103](https://github.com/tommy0103), which does this for Claude Code and Codex
sessions. Ideas taken from it:

- **The core premise** — session transcripts are already a complete, immutable evidence
  layer, so index the originals instead of distilling them into summaries that rot. A
  distilled memory then becomes a cache with provenance, which is what makes it safe to
  invalidate and re-derive.
- **Let the agent write queries** — no fixed query API. Query shapes cannot be enumerated
  in advance, so expose SQL and let the agent compose it (obelisk calls this CodeAct).
- **Reactive and proactive triggering** — a history tool that only answers direct questions
  misses most of its value. Obelisk's skill declares both, and its wording is what
  prompted adding the proactive clause here.
- **Retrieval discipline** — narrow before broadening, keep results bounded, answer with
  stable IDs and short snippets rather than volume.

Independently implemented; no code was taken, so obelisk's AGPL-3.0 does not extend here.
It differs in target (Kiro CLI rather than Claude Code and Codex), in dependencies
(python3 stdlib, no Node or Electron), in Chinese support (character-level indexing, which
obelisk tracks as an open issue), and in scope — obelisk has a memory layer for persisting
conclusions, which this does not yet.

If you use Claude Code or Codex rather than Kiro, use obelisk instead of this.

## More

- [DESIGN.md](DESIGN.md) — internals and rationale, for changing this code
- [skill/SKILL.md](skill/SKILL.md) — the agent-facing reference

```bash
python3 -m unittest discover -s tests -t .   # run the tests
```
