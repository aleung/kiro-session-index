# AGENTS.md

Guidance for agents working in this repo. See [README.md](README.md) for usage and
[DESIGN.md](DESIGN.md) for internals.

## This repo is published

It indexes work sessions, so it is written *about* private material while being public
itself. Nothing employer-specific may enter tracked files, commit messages, or history.

Note the recursion: this file is published too. Describe categories here, never the actual
terms. The concrete deny-list lives in `.publish-deny`, which is untracked.

## Never commit

- Employer, product, or internal repo names, and internal ticket identifiers.
- Internal hostnames, domains, or IP addresses.
- Internal tooling names — CI systems, credential helpers, proxies, issue trackers.
- Corporate usernames, absolute home paths, or work email addresses.
- Any session log or index artifact. `.gitignore` covers `*.db`, `*.jsonl` and friends;
  do not add exceptions.

Real session content is where genuine internal information lives, which is why the index
is kept in `~/.cache` outside any repo rather than relying on ignore rules alone.

## Use placeholders in examples

`my-project`, `/path/to/repo`, `user`, `example.com`. Never paste a real project name from
the corpus into documentation, even as an illustration — that is how the two leaks in this
repo's history got there.

Tests must assert generic patterns, not specific values: match `/home/[^/]+/` rather than
one real path, so the assertion holds for anyone and names no one.

## Check before committing

```bash
git add -A
./scripts/check-publish-safe.sh
```

It scans tracked files, all commit messages, and every diff in history, and exits non-zero
on a hit. Run it after staging, before committing.

## Fixing the working tree does not fix history

`git log -p` is published with the repo, so a term removed by a later commit is still
exposed. If the check reports a hit in history, the affected commits must be rewritten
before the first push — and history rewriting needs the user's explicit approval.

Once a remote exists and commits are pushed, rewriting is no longer a private operation.
Run the check before pushing, not after.

## Git identity

Commits here are unsigned and use a personal identity, set by a conditional include in the
user's global git config for this directory tree. That is deliberate. Do not force signing
and do not override the author.
