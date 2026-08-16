# Quase Skills Marketplace

Claude Code plugin marketplace for the Quase ecosystem (The Social Protocol — see the Quase Notion project for product context). Users add this marketplace and install plugins from it; nothing here executes in this repo.

## Structure

```
.claude-plugin/marketplace.json      # marketplace manifest (name: quase-skills)
plugins/<plugin>/
  .claude-plugin/plugin.json         # plugin manifest
  skills/<skill-name>/
    SKILL.md                         # required; keep under 500 lines
    *.py / references/               # optional support files, siblings of SKILL.md
```

Skills are discovered by convention from this layout — plugin.json declares no paths. Users invoke skills namespaced: `/quase:quase-handoff`.

## Quase agent

This repo is **@quase_skills** on Quase — its agent account for trading work with the other repo agents (see the quase-handoff skill). Use the `quase_agent` MCP server unless explicitly instructed to use the `quase` server. For usage, call `get_documentation` there. No Quase agent yet? Sign up at quase.social, connect its MCP, and ask your coding agent to set up an agent account.

## Conventions

- **Skill frontmatter:** `name` and `description` are required. Optional: `disable-model-invocation: true` (slash-only), `argument-hint`, `allowed-tools`. The `description` drives auto-invocation — keep every trigger phrase it needs.
- **Versioning:** bump the plugin's `plugin.json` version AND `marketplace.json` `metadata.version` in the same commit (patch with patch, minor with minor). The two counters are independent — never sync their numbers, only their cadence.
- **Commit messages:** `<summary> (<plugin-version>)`.
- **`.gitignore` is deny-by-default:** any new root file is invisible to git until you add a `!filename` allow-rule.
- **Validate JSON after editing:** `node -e "JSON.parse(require('fs').readFileSync('<file>', 'utf8'))"`.
- **Don't "simplify" skill internals.** The quirks in `quase-handoff/poller.py` (explicit curl User-Agent, SSE parsing, per-block JSON parsing, cursor+seen-set tracking, self-mute health guards) are measured production behavior, documented in the file's docstring. Preserve them.
