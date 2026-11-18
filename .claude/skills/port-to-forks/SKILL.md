---
name: port-to-forks
description: Ports a merged ai-boilerplate change (a PR or a commit range) to the forks castanha-bot and curupira-bot by handing it to an agent inside each fork, which adapts it by hand and opens a PR there. Use when the user asks to port, backport or carry a change to the forks.
disable-model-invocation: true
argument-hint: "<PR number | commit range> [castanha-bot|curupira-bot]"
---

# Port a change to the forks

`castanha-bot` and `curupira-bot` (both `terrakrya`, default branch `main`) were built from this repo but have diverged and have no upstream remote. A change is **ported by hand by an agent inside each fork**, so that fork's own rules, skills and hooks apply. Never copy files across, never edit fork files from here, and never cherry-pick blindly.

Arguments: `$ARGUMENTS`: a PR number or URL, or a commit range (`abc123..def456`), optionally followed by one fork name. Without a fork name, port to both.

## 1. Collect the change

```bash
OUT="${TMPDIR:-/tmp}/port-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$OUT"
# A PR:
gh pr view <PR> --json number,title,body,url,mergeCommit > "$OUT/pr.json"
gh pr diff <PR> > "$OUT/change.diff"
# Or a commit range:
git log --format='%h %s%n%n%b' <range> > "$OUT/log.txt"
git diff <range> > "$OUT/change.diff"
```

Write `$OUT/brief.md` for the fork agents: what the change does and why (from the PR body or commit messages), the files it touches, and which parts are probably **not** for a given fork:
- `castanha-bot` has no Telegram client (skip `packages/telegram-client/**` there).
- Rules files (`CLAUDE.md`, `AGENTS.md`, folder docs) are ported as meaning, in the fork's own wording and its existing structure.
- `.env.example` additions keep the fork's existing keys and comments.

## 2. Check each fork

```bash
for fork in castanha-bot curupira-bot; do
  git -C /home/ubuntu/projects/$fork status --short | head
  git -C /home/ubuntu/projects/$fork branch --show-current
done
```

A dirty checkout or a feature branch is the user's work in progress. The fork agent must work on a new branch off `origin/main` (in a worktree if the checkout isn't clean), never on top of that work. Mention it to the user.

## 3. Hand it to an agent in each fork

The task, for each fork (fill in `<fork>`, `<OUT>` and a short `<slug>`):

> Port an ai-boilerplate change to this repo. The diff is `<OUT>/change.diff` and the context `<OUT>/brief.md` (plus `<OUT>/pr.json` or `<OUT>/log.txt`). This repo has diverged from ai-boilerplate: port the change by hand, adapting it to this repo's code, names and language, and skip what doesn't apply here (say what and why). Never copy files over wholesale. Follow this repo's CLAUDE.md in full (Devflow task, tests, lint, build, the FleetView contract check if contract files change). Work on a new branch `port/<slug>` off `origin/main`, in a git worktree if the main checkout is dirty or on another branch; don't touch the checkout's current work. Commit, push the branch and open a PR against `main` whose body links the original PR or commits. Never merge. Report the PR link, what you ported, what you skipped and why, and the checks that passed.

How to run it depends on where you are:
- **Inside herdr (`HERDR_ENV=1`):** use `/delegate-claude` with one worker per fork, sized by the diff (`small` for a contained change, `large` when it spans several subsystems or needs adapting to diverged code). Its PRs then get reviewed like any other mesh work.
- **Otherwise:** run both headless, in parallel and in the background (each can take 10–30 minutes), always with `--dangerously-skip-permissions`:
  ```bash
  cd /home/ubuntu/projects/<fork> && claude -p "<task>" --dangerously-skip-permissions > "<OUT>/<fork>.log" 2>&1
  ```
  (`agy -p` instead of `claude -p` if you are Antigravity, or if the user asks for it.)

## 4. Report

One line per fork: the PR link, or why nothing was ported. Then what each agent skipped and why, and anything left for the user to check (the forks' own reviews, a migration to apply, a FleetView verdict).
