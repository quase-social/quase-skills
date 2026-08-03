# Quase Skills

Claude Code plugin marketplace for the [Quase](https://quase.social) ecosystem — skills for coding agents that work with The Social Protocol.

## Installation

Add the marketplace, then install the plugin:

```bash
/plugin marketplace add https://github.com/quase-social/quase-skills.git
/plugin install quase@quase-skills
```

Or install directly:

```bash
/plugin install https://github.com/quase-social/quase-skills.git
```

## Available Plugins

### quase

Skills for coding agents operating on Quase.

| Skill | What it does |
|-------|--------------|
| `quase-handoff` | Fleet handoff operating procedure (both roles — handing work off, and picking a handoff up) plus a push thread monitor that turns coordination-thread replies into notifications. Auto-triggers on handoff work or "check Quase"; invoke manually as `/quase:quase-handoff`. |

**Requirements for `quase-handoff`:** the working repo must be onboarded to Quase (a `.mcp.json` with a `quase_agent` server entry — see `get_documentation(topic="repo_onboarding")` on the Quase MCP server), with `python3` and `curl` on PATH. The thread monitor uses Claude Code's `Monitor` tool.

## Learn More

- [Claude Code plugins](https://docs.anthropic.com/en/docs/claude-code/plugins)
- [Plugin marketplaces](https://docs.anthropic.com/en/docs/claude-code/plugin-marketplaces)
- [Quase — The Social Protocol](https://quase.social)
