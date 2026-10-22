# skills-viewer

A single local web page listing every agent skill on your machine: the skills each coding agent
loads in every session, the ones that live in project repos, and the ones sitting in skill repos you
have cloned. Click a card for its paths, git branch and commit, context cost in tokens, spec checks,
bundled files and the rendered `SKILL.md`.

Works with any agent that uses the [Agent Skills](https://agentskills.io/specification) format
(a folder with a `SKILL.md`): Claude Code, Codex, OpenCode, Cursor, Gemini CLI, Pi, Copilot.

## Run

```bash
git clone <this repo> && cd skills-viewer
python3 build_skills_viewer.py --open     # scan, write skills.html, open it
python3 build_skills_viewer.py --watch    # rebuild whenever a skill is added or edited
```

Python 3.9+ and the standard library only. Optional: `pip install tiktoken` for token counts from a
real tokenizer (otherwise about 4 characters per token), and PyYAML for stricter frontmatter parsing.

## What it scans

With no configuration it looks in the per-user skill folders of common agents (`~/.claude/skills`,
Claude Code plugins, `~/.agents/skills`, `~/.codex/skills`, `~/.config/opencode/skills`,
`~/.pi/agent/skills`, `~/.copilot/skills`) and for project skills (`.claude/skills`, `.agents/skills`)
in repos under `~/GitHub`, `~/src`, `~/code` and `~/projects`.

To change that, copy `sources.example.json` to `sources.json` and edit it. Each source has a `kind`:

| kind | meaning |
|---|---|
| `installed` | an agent lists these skills in every session |
| `project` | loaded only inside that repo |
| `catalog` | cloned but not installed (for example a team's skill repo) |

Installed folders are scanned one level deep, which is what agents load; catalogs are scanned
recursively. A symlinked install shows up as one card with both locations. For a one-off scan:
`python3 build_skills_viewer.py --no-config --root <folder>`.

## What the numbers mean

Context cost follows the progressive-disclosure model of the Agent Skills spec:

1. **Always loaded**: name + description. Every installed skill costs this in every session.
2. **When the skill triggers**: the `SKILL.md` body.
3. **Only if opened**: bundled files (`references/`, `scripts/`, `assets/`).

The bar at the top adds up tier 1 per agent and compares it with a description budget (default
8,000 characters for Claude Code and for Codex, the limits the
[genesis-skills](https://github.com/AI-ModCon/genesis-skills) validator models). Treat the budgets as
estimates and set your own in `sources.json`.

## Checks

From the spec: name format and length, name matching its folder, description present and at most
1,024 characters, compatibility at most 500, body under 500 lines and about 5,000 tokens. It also
flags skills that pre-approve any shell command (`allowed-tools: Bash(*)`), frontmatter keys outside
the spec, broken symlinks, two installed skills with the same name, and a project skill that shares a
name with a user-level one (the agent sees both inside that project).

## Privacy

Everything stays on your machine: the script reads local folders and writes one HTML file; it makes
no network requests. That file embeds your local paths and the full text of every skill it finds,
including skills from private repos, so `skills.html` and your personal `sources.json` are in
`.gitignore`. Don't publish the generated page unless every skill on it is public.

## Files

| File | |
|---|---|
| `build_skills_viewer.py` | the scanner and page builder |
| `viewer_template.html` | the page; the script inserts the data as JSON |
| `sources.example.json` | template for your own `sources.json` |

## License

MIT
