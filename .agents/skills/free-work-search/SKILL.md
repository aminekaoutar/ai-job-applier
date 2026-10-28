---
name: free-work-search
version: 1.0.0
description: >
  Use this skill whenever the user wants to search for tech, IT, data or AI jobs in
  France, or freelance/contract missions there - CDI, CDD, freelance, stage or
  alternance - even if they don't mention free-work.com explicitly. Strong fit for
  AI / GenAI / LLM / agentic roles (AI engineer, ingénieur IA, développeur agents IA,
  MLOps, data scientist, architecte IA) in Paris, Île-de-France, Lyon, Toulouse,
  Nantes, Lille, Marseille, or remote across France. Trigger phrases: free-work,
  freework, jobs in France, tech jobs Paris, AI jobs France, offres d'emploi,
  recherche d'emploi, emploi informatique, emploi IA, mission freelance, offre CDI,
  ingénieur IA, développeur IA, IA générative, agents IA, look up this Free-Work posting.
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/free-work-search/cli/src/cli.ts *)
---

# Free-Work Search Skill

Search live tech/IT job postings and freelance missions on **Free-Work**
(free-work.com), a major French IT-specialist job board (formerly Freelance-Info
and Carriere-Info). It covers permanent (CDI), fixed-term (CDD), freelance/contractor,
internship and apprenticeship postings - and it is where most French ESN/consulting
and many end-client GenAI, LLM and agentic-AI roles are posted.

No authentication, no API key, and **zero runtime dependencies** - it runs with just
`bun` against the site's own public JSON API.

## ⚠️ Personal use only

This reads Free-Work's public API (robots.txt allows it as of 2026-10-28), but it
is not an official, documented API. **Keep volume low and don't use it commercially
or for bulk data collection.** Run it on your own responsibility.

## When to use this skill

- Search for tech / data / AI jobs anywhere in France (or remote)
- Filter by contract type (CDI, CDD, freelance, stage, alternance), remote mode, and posting age
- Get the full description of a specific Free-Work posting

## Commands

### Search job listings

```bash
bun run .agents/skills/free-work-search/cli/src/cli.ts search [flags]
```

Key flags:
- `--query <text>` / `-q <text>` - keywords (title, skill, stack). French and English both work (`"IA générative"`, `"LLM engineer"`, `"agents IA"`). Recommended.
- `--location <text>` / `-l <text>` - a place name, resolved through Free-Work's own autocomplete (`"Paris"`, `"Lyon"`, `"Île-de-France"`, `"France"`), or a raw Free-Work key like `"fr~ile-de-france~~"`. French places win over same-named places abroad. An unknown place exits 1 (`BAD_LOCATION`) rather than searching everywhere.
- `--contract <list>` / `-c <list>` - comma list of `permanent|cdi`, `fixed-term|cdd`, `contractor|freelance`, `internship|stage`, `apprenticeship|alternance`. A posting matches if it offers **any** listed contract (many postings offer both CDI and freelance). Omit for all.
- `--remote <mode>` - `full|remote`, `partial|hybrid`, `none|onsite`.
- `--jobage <days>` - posted within N days. The API only has 1/7/14/30-day buckets; the CLI rounds up server-side then trims to your exact N client-side. Values above 30 return all ages.
- `--page <n>` - page number (1-indexed, 20 results per page).
- `--limit <n>` / `-n <n>` - cap total results emitted (client-side).
- `--format json|table|plain` - default `json`.

### Fetch full job detail

```bash
bun run .agents/skills/free-work-search/cli/src/cli.ts detail <slug|url> [--format json|plain]
```

`slug` is the `id` from `search` results (e.g. `tech-lead-ia-generative-agentique-f-h`).
A full `https://www.free-work.com/.../job-mission/<slug>` URL also works; URLs on any
other host are rejected. Returns the description, candidate profile, company/context
text, contracts, remote mode, experience level, salary or daily rate (TJM), duration,
start date, expiry, and an `isActive` flag (still published and not past expiry).

## Usage examples

```bash
# Generative-AI roles in Paris, last 14 days
bun run .agents/skills/free-work-search/cli/src/cli.ts search -q "IA générative" -l "Paris" --jobage 14 --format table

# Permanent LLM engineering roles anywhere in France
bun run .agents/skills/free-work-search/cli/src/cli.ts search -q "LLM engineer" -l "France" -c cdi --format table

# Agentic-AI freelance missions, fully remote
bun run .agents/skills/free-work-search/cli/src/cli.ts search -q "agents IA" -l "France" -c freelance --remote full --format table

# RAG / LangChain roles in Lyon, last 30 days
bun run .agents/skills/free-work-search/cli/src/cli.ts search -q "RAG LangChain" -l "Lyon" --jobage 30 --format plain

# AI internships / apprenticeships in Île-de-France
bun run .agents/skills/free-work-search/cli/src/cli.ts search -q "intelligence artificielle" -l "Île-de-France" -c stage,alternance --format table

# Full details for one posting
bun run .agents/skills/free-work-search/cli/src/cli.ts detail tech-lead-ia-generative-agentique-f-h --format plain
```

## Output formats

| Format | Best for |
|--------|----------|
| `json` | Default - programmatic use, passing `id` slugs to `detail` |
| `table` | Quick human-readable scanning |
| `plain` | Reading a single job's full detail (`detail` command) |

Each search result carries `id`, `title`, `company`, `location`, `date`, `url` (missing
values are `null`), plus `contracts`, `remoteMode`, `experienceLevel`, `salary`, `skills`.
`meta` also reports `total` (server-side match count) and the resolved `location` key.

All errors are written to **stderr** as `{ "error": "...", "code": "..." }` and the process exits with code `1`.

## Notes

- Many postings are from ESNs/consultancies (Alten, Celad, Sopra Steria…) placing staff at an end client; the end client is often named only in the description. Factor that into fit and culture evaluation.
- `contractor` postings quote a daily rate (TJM, `EUR/jour`), not a salary.
- Postings are almost all in French; the role itself may still be English-speaking - check the description before applying the Language Gate.
- Keyword search is full-text (title, description, skills), so broad terms like `IA` also match "AI adoption" communication roles. Prefer specific terms (`LLM`, `agents IA`, `RAG`, `MLOps`, `IA générative`) and filter by title.
- Endpoint details, parameters and response fields are in `url-reference.md`.
