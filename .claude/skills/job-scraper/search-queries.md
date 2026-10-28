# Search Queries for Job Scraper

<!-- Market: France. Focus: AI / GenAI / LLM / agentic-AI engineering and development.
     Adjust the Location Filter below to your own base once /setup has filled CLAUDE.md. -->

## Installed portal CLIs (primary for `/scrape`)

`/scrape` discovers every portal skill under `.agents/skills/*/SKILL.md` and runs its CLI first. For the French market the relevant CLIs are:

| CLI | Covers | How to call it for this strategy |
|-----|--------|----------------------------------|
| `free-work-search` | Free-Work - French tech/IT board: CDI, CDD, freelance, stage, alternance | `-q "<keyword>" -l "France"` (or `-l "Paris"` / `-l "Île-de-France"` / `-l "Lyon"`), `--jobage 14`, `--limit 20`. Add `-c cdi` or `-c freelance` when the user wants one contract type. |
| `linkedin-search` | LinkedIn, any country | `-q "<keyword>" -l "France"` or `-l "Paris, Île-de-France, France"`, `--jobage 14`, `--limit 20` |
| `freehire-search` | freehire.me ATS aggregator (catches startup postings from Ashby/Lever/Greenhouse) | `-q "<keyword>" --country FR`, `--jobage 14`, `--limit 20` |

The Danish demo portals (`jobindex`, `jobnet`, `jobbank`, `jobdanmark`) ship `enabled: false` and only return Danish postings - leave them off for this strategy.

The `site:` query templates below are the **WebSearch fallback** - for French boards without a CLI (Welcome to the Jungle, APEC, France Travail, HelloWork, Indeed, JobTeaser, Station F) and for company career pages.

**Language scope:** write every query category in every language listed in your CLAUDE.md Languages table. For France that is normally **French and English** - French AI postings mix both ("Ingénieur IA générative", "AI Engineer (H/F)"), so each category below carries both. A posting requiring a language you have *not* declared is excluded before scoring; a posting requiring a *higher level* than you declared is flagged, not excluded - see `04-job-evaluation.md`'s Language Gate.

## Search Sites

Primary (CLI-backed):
- **free-work.com** - French IT/tech board, the main source for CDI and freelance AI/data roles at ESNs, consultancies and end clients (`free-work-search` CLI)
- **linkedin.com/jobs** - filter `France` / `Paris, Île-de-France, France` (`linkedin-search` CLI)

Primary (WebSearch fallback only - no CLI: these boards disallow automated search in robots.txt or sit behind bot protection):
- **welcometothejungle.com** - the reference board for French startups and scale-ups, including most Paris AI startups
- **apec.fr** - cadres (executive/engineer-level) postings, CDI-heavy
- **candidat.francetravail.fr** - France Travail (ex-Pôle emploi), the national public board
- **hellowork.com** - large general French board
- **fr.indeed.com** - aggregator
- **jobteaser.com** - internships, alternance and junior roles (school-linked)
- **jobs.stationf.co** - jobs at Station F's startups (Paris startup campus)
- **lesjeudis.com** - IT-specialist board

Company career pages (WebSearch `site:` - French AI labs, AI-native startups and AI-heavy scale-ups):

| Company | Focus | Careers |
|---------|-------|---------|
| Mistral AI | LLMs, agents platform | mistral.ai/careers, welcometothejungle.com/fr/companies/mistral-ai |
| Hugging Face | open-source ML / LLM tooling | apply.workable.com/huggingface |
| H Company | agentic AI / computer-use agents | hcompany.ai/careers, jobs.ashbyhq.com/hcompany |
| Dust | enterprise AI agents | jobs.ashbyhq.com/dust |
| Photoroom | generative image AI | jobs.ashbyhq.com/photoroom |
| Owkin | AI for biomedical research | jobs.ashbyhq.com/owkin |
| Dataiku | enterprise AI / ML platform | job-boards.greenhouse.io/dataiku |
| Giskard | LLM / agent testing & evaluation | welcometothejungle.com/fr/companies/giskard |
| Alan | health insurance, heavy internal AI use | jobs.ashbyhq.com/alan |
| Doctolib | health tech, AI features | careers.doctolib.com |
| Kyutai | open AI research lab (non-profit) | kyutai.org |

ATS-wide sweeps catch the many smaller Paris AI startups that post on the same hosts:
`site:jobs.ashbyhq.com`, `site:jobs.lever.co`, `site:job-boards.greenhouse.io`, `site:apply.workable.com` + `Paris` + the keyword.

## Query Categories

Grouped by priority and organised by **function, not job title** - French postings use many titles for the same work (an "Ingénieur IA" at one company is a "Développeur LLM", "AI Engineer" or "Consultant IA générative" elsewhere). CLI keyword lists come first (pass each as `-q`), then the WebSearch `site:` fallbacks.

### Priority 1: Building GenAI / LLM applications and AI agents

Engineers who build products on top of LLMs: agents, RAG, tool use / MCP, evaluation, LLM integration.

CLI keywords (FR + EN):
```
agents IA            | AI agent
IA agentique         | agentic AI
IA générative        | generative AI
ingénieur IA         | AI engineer
développeur IA       | AI developer
ingénieur LLM        | LLM engineer
RAG                  | MCP
développeur LLM      | applied AI engineer
```

WebSearch fallback:
```
site:welcometothejungle.com "AI Engineer" OR "Ingénieur IA" Paris
site:welcometothejungle.com "agents IA" OR "AI agents" CDI
site:welcometothejungle.com "IA générative" développeur
site:apec.fr "ingénieur IA générative"
site:candidat.francetravail.fr "intelligence artificielle" développeur
site:hellowork.com "ingénieur IA" OR "développeur IA"
site:jobs.stationf.co "AI engineer" OR "LLM"
site:jobs.ashbyhq.com "AI Engineer" Paris
site:jobs.lever.co "LLM" Paris
site:linkedin.com/jobs "AI Engineer" France
```

### Priority 2: ML engineering, MLOps and LLMOps

Training, fine-tuning, serving and running models in production.

CLI keywords (FR + EN):
```
ML engineer          | machine learning engineer
ingénieur machine learning
MLOps                | LLMOps
fine-tuning          | deep learning
data scientist IA    | NLP
```

WebSearch fallback:
```
site:welcometothejungle.com "Machine Learning Engineer" Paris
site:welcometothejungle.com MLOps CDI
site:apec.fr "ingénieur machine learning"
site:job-boards.greenhouse.io "Machine Learning" Paris
site:apply.workable.com "Machine Learning Engineer" Paris
site:linkedin.com/jobs "MLOps" France
```

### Priority 3: Software engineering at AI-first companies

Backend / full-stack / platform roles where the product is AI - a strong entry route into the AI labs and agent startups above.

CLI keywords (FR + EN):
```
développeur Python IA      | Python developer AI
développeur fullstack IA   | full-stack engineer AI
backend LLM                | software engineer AI
```

WebSearch fallback:
```
site:jobs.ashbyhq.com "Software Engineer" Paris AI
site:welcometothejungle.com "Software Engineer" "IA" Paris startup
site:mistral.ai/careers "Software Engineer"
site:jobs.stationf.co "Software Engineer"
```

### Priority 4: AI consulting, solutions and adoption

Consulting / forward-deployed / solutions roles: deploying GenAI and agents at clients, AI tooling enablement (Claude Code, Copilot), AI architecture. Common at ESNs and consultancies - mostly on Free-Work.

CLI keywords (FR + EN):
```
consultant IA générative  | AI consultant
architecte IA             | AI architect
forward deployed engineer | solutions engineer AI
Claude Code               | copilot IA
```

WebSearch fallback:
```
site:welcometothejungle.com "consultant IA" OR "AI consultant"
site:apec.fr "consultant IA générative"
site:linkedin.com/jobs "Forward Deployed Engineer" Paris
```

### Priority 5: Internships, alternance and junior AI roles

Only if the user is early-career or wants a work-study contract.

CLI: `free-work-search` with `-c stage,alternance` and the Priority 1-2 keywords.

WebSearch fallback:
```
site:jobteaser.com "stage" "intelligence artificielle"
site:jobteaser.com "alternance" "IA générative"
site:welcometothejungle.com "stage" "AI engineer" Paris
site:hellowork.com "alternance" "développeur IA"
```

## Location Filter

Most French AI jobs are in Paris and Île-de-France; set the rest to match your own base and mobility once CLAUDE.md is filled in.
- Paris & Île-de-France (incl. La Défense, Saclay, Vélizy) - main AI hub
- Lyon, Grenoble, Toulouse, Nantes, Lille, Sophia-Antipolis/Nice - secondary tech hubs
- Remote within France (`free-work-search --remote full`; `linkedin-search --remote remote`) - check whether the posting requires French residency
- Outside France - out of scope for this strategy

## Language Filter

Your working languages and levels are in CLAUDE.md's Languages table. Apply `04-job-evaluation.md`'s Language Gate: a posting requiring a language you haven't declared at all is excluded; a posting requiring a higher level than you declared in a language you do work in is flagged, not excluded. Paris AI labs and startups often post and work in English, while ESN/consultancy postings are mostly in French and usually require French for client work - judge by the stated requirement, not the posting's language.

## Contract Types (France)

Record the contract on every result - it changes the evaluation:
- **CDI** (permanent) / **CDD** (fixed-term) - salaried
- **Freelance / portage** - daily rate (TJM); Free-Work shows it as `EUR/jour`
- **Stage** (internship, max 6 months) / **Alternance** (work-study)
- **ESN** postings (consultancy placing you at a client) - note the end client when the description names it

## Date Filter

Only include jobs posted within the last 14 days, or with an application deadline that has not yet passed. If a posting date cannot be determined, include it but flag as "date unknown".

## Adapting Queries

If the user specifies a focus area, select queries from the matching category and also generate 2-3 custom queries for that focus. For example:
- "/scrape agents" -> Priority 1 agent keywords + `"Claude Agent SDK"`, `"LangGraph"`, `"multi-agent"`
- "/scrape freelance" -> every category through `free-work-search -c freelance`
- "/scrape Lyon" -> every category with `-l "Lyon"` on the CLIs and `Lyon` on the fallbacks
