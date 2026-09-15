---
name: commit-with-message
description: Generate a conventional commit message, commit staged changes, and push to the current branch
---

# Commit with message

Generate a commit message in BrewOps team style, commit changes, and push.

## What it does

Analyzes staged changes and writes a conventional commit message following your team's style, then commits and pushes to the current branch.

## Steps

1. Run `git status` to see what's staged
2. Run `git diff --cached` to review the actual changes
3. Generate a commit message in conventional format (e.g., `feat:`, `fix:`, `chore:`)
   - Keep it concise but descriptive
   - Include ticket references if applicable (e.g., `ticket 005`)
   - Add shortened details about what changed
4. Stage changes with `git add`, excluding temp files and environment files
   - Do not stage: `.env*`, `*.tmp`, `__pycache__`, `.pyc`, `node_modules`, `.DS_Store`
   - Stage only project code and configuration files
5. Run `git commit` with the generated message (includes attribution)
6. Run `git push` to push to the current branch
7. Report the commit hash and branch

## Message style

Follow your team's conventions:
- Start with type: `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`
- Keep description short (under 60 chars after type)
- Reference tickets inline if relevant
- Auto-include attribution line

## Example

```
feat: add dashboard date-range filter (ticket 005)
```

## Important

- Only stages and commits what's already staged (`git add`)
- Will not stage unstaged changes
- Requires a clean working tree (no conflicts)
- Pushes to the current branch

## Version

0.1.0
