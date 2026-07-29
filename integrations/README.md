# Agent integrations

The tools themselves are plain commands (`draw`, `mcm`, `quote`) — these files
just teach coding agents how to use them well. Copy to taste:

- **Claude Code**: copy `claude-code/*.md` into `~/.claude/commands/` to get
  `/draw`, `/mcm`, `/browse`, and `/quote` slash commands in every session.
  Note `/browse` and `/mcm` are different tools: `/mcm` is McMaster part data
  over the site's own JSON (fast, per-part), `/browse` reads any supplier's
  rendered catalog pages (categories, materials, filter facets).
- **Codex**: append `codex/AGENTS.md` to `~/.codex/AGENTS.md` (global) or drop
  it into any project's `AGENTS.md`.
- **Anything else**: point your agent at the repo's `README.md`, `AGENTS.md`
  (Protolabs contract), and `SITEAPI.md` (McMaster routes + rate-limit rules).

Note: examples in these files reference `C:\code\unified` — adjust if you
cloned elsewhere. The commands themselves work from any folder as long as the
repo directory is on PATH.
