# Changelog

<!--
Writing rules (30-second read — release gate enforces these):

  ✅ Each stable release gets ONE `## v<semver>` section (e.g. `## v1.2.0`).
  ✅ Write user-facing prose: what the user sees change, not which function moved.
  ✅ Use `-` or `*` bullets. One sentence per bullet. PR link optional.
  ❌ No commit hashes (7-40 hex chars are blocked by `scripts/validate_changelog.py`).
  ❌ Don't paste raw commit messages.
  ❌ Don't ship empty placeholder sections.

Good:
  - Squad rename in the sidebar (#42)
  - Fix thread auto-scroll flicker when many posts land at once
  - events.jsonl now appends incrementally — server starts 3x faster

Bad (will be rejected by release gate):
  - fix bug in abc1234           ← contains commit hash
  - WIP                          ← under 30 non-whitespace chars
  - (empty section)              ← no bullet, no body

Nightly tags (`vYYYY.MM.DD[-hash]`) skip this gate and use GitHub
auto-generated notes — but stable semver tags must pass.
-->

## Unreleased

- Thread view no longer flickers on every refresh. The post list is now diffed in place (keyed by post id), so unchanged posts keep their existing DOM nodes — SSE bursts, the 8s poll fallback, and cross-tab broadcasts can all fire together without repainting the column. When SSE is healthy the per-thread poll is suppressed entirely. The post column also reserves the scrollbar gutter so the Slack-style hover-reveal scrollbar no longer reflows the column when the pointer enters/leaves.
- _Add entries here as they ship. They get cut into the next `v<semver>` section at release time._

<!--
Example for the first real stable release:

## v1.0.0

- Initial public release of the OpenForge slack-shaped multi-agent workbench
- One-line install via `npx @symbolstar/openforge install` (mac, launchd-backed)
- Daily backup snapshots + automatic rollback on failed updates
-->
