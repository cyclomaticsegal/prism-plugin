---
name: prism-release
description: Cut a new release of the PRISM plugin — bump the semver version in plugin.json and server/package.json, commit, push, tag. Project-local skill, only available when working in the prism-plugin repo. Invoke explicitly; never auto-trigger.
---

# PRISM Release

Cut a new release of the PRISM plugin. Project-local skill — only loaded when working in this repo.

## When to use

Run before pushing a commit that should reach existing PRISM users. Cowork doesn't auto-update users unless the `version` field in `.claude-plugin/plugin.json` changes between commits, so a code change without a version bump only reaches users who manually reinstall.

Don't auto-trigger this on every commit — only when the change is intended for users (bug fixes that affect them, new features, breaking changes).

## Bump rules (semver)

- **patch** — bug fixes, internal cleanups, doc edits that affect installed-user behaviour. Default if the user doesn't specify.
- **minor** — new features or new MCP tools, backward compatible.
- **major** — breaking changes: renamed or removed tools, schema migration users must do, behavioural changes existing users would notice.

Default to patch when in doubt. If considering major, surface it explicitly to the user before bumping.

## Steps

1. Read the current version from `.claude-plugin/plugin.json` (`version` field).
2. Compute the new version per the bump rule.
3. Update **both** files so they stay aligned:
   - `.claude-plugin/plugin.json` — `version` field.
   - `server/package.json` — `version` field.
4. Stage only those two files. Don't bundle unrelated edits in the same commit.
5. Commit with `Release vX.Y.Z` as the subject. Body can list the user-visible changes since the previous release.
6. Push to `main`.
7. Tag the commit with `git tag vX.Y.Z && git push origin vX.Y.Z` so the release is git-visible. Untagged releases are invisible to anyone browsing GitHub releases.

## What never to do

- Never bump major without flagging it to the user first. Major bumps need a CHANGELOG note and a heads-up.
- Never tag without pushing the tag. The tag has to exist on origin to count.
- Never push a code change intended for users without bumping the version. They won't get it.
- Never bundle unrelated edits with the version-bump commit. Keep the release commit boring and reviewable.

## What this skill is not

This is a **project-local** skill — it lives in `.claude/skills/prism-release/` and is only loaded when Cowork or Claude Code is rooted in this repo. It is not part of the published PRISM plugin and is not visible to PRISM's end users.
