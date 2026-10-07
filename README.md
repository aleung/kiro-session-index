# kiro-session-index

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Install](#install)
- [Use](#use)
  - [Go back into a session](#go-back-into-a-session)
  - [Ask the agent about past sessions](#ask-the-agent-about-past-sessions)
- [Under the hood](#under-the-hood)
  - [Index update](#index-update)
  - [Where things live](#where-things-live)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)
- [More](#more)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

Search your past Kiro CLI sessions, and go back into them.

Two entry points, for the two things you want from a session you half-remember:

| You want | How | Where |
|---|---|---|
| A past decision quoted into what you are doing now | ask the agent — the `session-history` skill triggers on its own, or invoke it with `/session-history` | inside a Kiro CLI conversation |
| To reopen that session and carry on | run `kiro-resume` | at the shell |

Install with `./setup.sh` (below), then use either one. Handles Chinese, English, and
code. Python3 stdlib only — no packages to install. (`kiro-resume` also wants `kiro-cli`
itself, and will use `fzf` for selection if you have it.)

## Install

```bash
./setup.sh
```

Builds the index, installs the `session-history` skill so the agent can search your
history on its own, and puts `kiro-resume` on your PATH via `~/bin` — plus `ksi-query`,
the tool the skill runs, in case you want to see what it does or run it directly.

After setup this repo is not needed at runtime — everything is copied into the skill
directory, and the PATH symlinks point at that copy. Re-run `setup.sh` after pulling
changes.

```bash
./setup.sh --no-index         # install without building the index
./setup.sh --skill-dir DIR    # install the skill somewhere else
./setup.sh --bin-dir DIR      # put the symlinks somewhere other than ~/bin
```

An existing `kiro-resume` or `ksi-query` in the target directory is replaced.
`ksi-index` is deliberately not linked: every query refreshes the index already, so a
manual rebuild is rare enough to spell out in full.

## Use

### Go back into a session

```bash
kiro-resume                  # sessions started in this directory, newest first
kiro-resume --all-dirs       # from everywhere
kiro-resume 遗忘机制          # only sessions that discussed it (implies --all-dirs)
kiro-resume 记忆 遗忘         # both words present
kiro-resume "遗忘 机制"       # that exact phrase
kiro-resume 'token*'         # starts with token (quote it — the shell globs a bare *)
kiro-resume -- memory -pipeline   # first present, second absent (-- so the dash isn't read as an option)
kiro-resume -n 40            # a longer list
```

Chinese matches inside words: `忘机` finds `遗忘机制`.

Pick one and it resumes, changing directory to wherever that session was working.
Selection uses `fzf` when you have it, a numbered menu otherwise.

Searching shows, under each session, the sentence the match was found in:

```
7d ago      13x ai-workspace         handoff notes for the session index
    …先核实几个我要引用的数字，避免记忆出错。
1h ago       3x kiro-session-index   I have another utility ~/bin/kiro-resume
    …then `记忆的遗忘机制是` becomes one token, and querying…
```

That second line is the point. A title is the opening prompt truncated, so it says
how a session *started*, and what you searched for usually came up later — of the
sessions matching `记忆`, none had the word in the title. Sorting is by hit count,
then recency, so the session most about your topic is first rather than merely the
most recent one that mentioned it. Typing in `fzf` narrows on both lines.

Only your side of the conversation is searched. Not command output — a log that
printed a word is not a discussion of it, and including it roughly doubles the list
(`pipeline` matches 139 sessions in prose and another 134 in output alone); use
`ksi-query -t` for that. Not sub-agent sessions either: every turn in them was
written by the agent, not by you.

Listing does not need the index and works even if you have never built one.
Searching does, and will build it if missing; if it cannot, it says so and exits
non-zero rather than quietly searching worse.

### Ask the agent about past sessions

Just ask. The `session-history` skill triggers on questions like "did we discuss this
before?", "之前讨论过吗", or a reference to earlier work the agent has no context for.
When it does not trigger on its own, invoke it by name: `/session-history <what you
are after>`. Either way it searches the index and quotes back what was actually said —
when it happened, which project, a readable snippet, and a citation you can quote back.

## Under the hood

### Index update

Nothing to do — every query checks the source logs first and updates what changed,
which takes about 0.3 s. The session you are in right now is searchable within seconds.

To refresh or rebuild by hand — `ksi-index` is not on your PATH, so spell it out:

```bash
~/.kiro/skills/session-history/scripts/ksi-index          # refresh
~/.kiro/skills/session-history/scripts/ksi-index --full   # rebuild, ~3 s
```

### Where things live

| What | Where |
|---|---|
| The index | `~/.cache/kiro-session-index/` |
| Installed tools and skill | `~/.kiro/skills/session-history/` |
| `kiro-resume`, `ksi-query` on PATH | `~/bin/` (symlinks into the above) |
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
Ask the agent to try the other corpus, then a literal substring, then a different
wording — the conversation may have used different words than your query.

**`kiro-resume` says it cannot search the index.**
Plain `kiro-resume` still lists your sessions — listing never touches the index.
Rebuild with `~/.kiro/skills/session-history/scripts/ksi-index --full`.

**`kiro-resume` does not list a session you remember.**
Sub-agent sessions are excluded on purpose: nothing in them was written by you.
Sessions from other directories need `--all-dirs`.

**`ksi-index` exits non-zero.**
It names the sessions it could not index; the rest of the index is still current.

**`sh: set: Illegal option -o pipefail`.**
An older copy of `setup.sh`. Pull the current one; it runs correctly under
`./setup.sh`, `sh setup.sh`, and `bash setup.sh` alike.

## Credits

Inspired by [obelisk](https://github.com/tommy0103/obelisk), which does this for Claude
Code and Codex. Borrowed its premise — transcripts are an evidence layer to index, not
distil — along with letting the agent write SQL directly, proactive as well as reactive
triggering, and its retrieval discipline.

Independently implemented; no code taken, so obelisk's AGPL-3.0 does not extend here.
If you use Claude Code or Codex, use obelisk instead.

## More

- [DESIGN.md](DESIGN.md) — internals and rationale, for changing this code
- [skill/SKILL.md](skill/SKILL.md) — the agent-facing reference
