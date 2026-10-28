# free-work-cli

CLI for searching tech/IT jobs and freelance missions on **Free-Work**
(free-work.com), a major French IT-specialist job board.

**Data source**: Free-Work's public JSON API (`/api/job_postings`, `/api/job_postings/<slug>`, `/api/locations/autocomplete`).
**Authentication**: None required.
**Dependencies**: None (plain `bun` + `fetch`). `bun install` is optional and only pulls dev type defs.

> **Personal use only.** Keep volume low, don't use it commercially or for bulk data
> collection, and run it on your own responsibility.

## Installation

```bash
cd .agents/skills/free-work-search/cli
bun install   # optional — only installs TypeScript dev types
```

## Commands

| Command | Description |
|---------|-------------|
| `search` | Search for job listings (keywords, location, contract, remote, age) |
| `detail` | Fetch full detail for a single posting by slug or URL |

`search` accepts `--format json|table|plain` (default `json`); `detail` accepts `--format json|plain`.

```bash
bun run src/cli.ts search -q "IA générative" -l "Paris" --jobage 14 --format table
bun run src/cli.ts detail tech-lead-ia-generative-agentique-f-h --format plain
```

## Tests

```bash
bun test                    # offline: parsing, flag mapping, CLI contract (mocked fetch)
FREE_WORK_LIVE=1 bun test   # also hits free-work.com for a live smoke test
```

See `../url-reference.md` for endpoints and parameters.
