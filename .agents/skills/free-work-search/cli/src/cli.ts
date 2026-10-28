#!/usr/bin/env bun
// Self-contained CLI for searching Free-Work (free-work.com), France's largest
// tech/IT job board - CDI, CDD, freelance missions, internships and
// apprenticeships. Reads the site's public JSON API; no external CLI
// framework, so it runs anywhere `bun` is available with zero install.
//
// Personal use only. Keep volume low and do not use it commercially or for
// bulk data collection. Run it on your own responsibility.

import { runSearch, type SearchOpts } from "./commands/search.js"
import { runDetail, type DetailOpts } from "./commands/detail.js"
import { CONTRACTS, contractValue, remoteValue } from "./helpers.js"

interface Flags {
  _: string[]
  [k: string]: string | boolean | string[]
}

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] }
  const alias: Record<string, string> = { q: "query", l: "location", n: "limit", c: "contract" }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a.startsWith("--") || a.startsWith("-")) {
      const key = alias[a.replace(/^-+/, "")] ?? a.replace(/^-+/, "")
      const next = argv[i + 1]
      if (next === undefined || next.startsWith("-")) {
        flags[key] = true
      } else {
        flags[key] = next
        i++
      }
    } else {
      ;(flags._ as string[]).push(a)
    }
  }
  return flags
}

const HELP = `free-work-cli — search tech/IT jobs and freelance missions on Free-Work (France)

USAGE
  bun run src/cli.ts search [flags]
  bun run src/cli.ts detail <slug|url> [--format json|plain]

SEARCH FLAGS
  --query, -q <text>      Keywords (title, skill, stack), French or English. Recommended.
  --location, -l <text>   Place name resolved via Free-Work's autocomplete ("Paris", "Lyon",
                          "Île-de-France", "France"), or a raw key like "fr~ile-de-france~~".
  --contract, -c <list>   Comma list: permanent|cdi, fixed-term|cdd, contractor|freelance,
                          internship|stage, apprenticeship|alternance. Default: all.
  --remote <mode>         full|remote, partial|hybrid, none|onsite.
  --jobage <days>         Posted within N days (server buckets 1/7/14/30, trimmed client-side).
  --page <n>              1-indexed page (20 results/page). Default 1.
  --limit, -n <n>         Cap results emitted (client-side).
  --format <fmt>          json (default) | table | plain.

EXAMPLES
  bun run src/cli.ts search -q "IA générative" -l "Paris" --jobage 14 --format table
  bun run src/cli.ts search -q "LLM engineer" -l "Île-de-France" -c cdi --format table
  bun run src/cli.ts search -q "agents IA" -l "France" -c freelance --remote full --format table
  bun run src/cli.ts search -q "RAG LangChain" -l "Lyon" --jobage 30 --format plain
  bun run src/cli.ts detail directeur-de-projet-ia-agentique --format plain

Personal use only — keep volume low.
`

// Long-form flag names each command accepts (parseFlags resolves the short
// aliases q/l/n/c to these before validation). "help"/"h" pass so
// `search --help` still prints usage.
const KNOWN_FLAGS: Record<string, Set<string>> = {
  search: new Set(["query", "location", "contract", "remote", "jobage", "page", "limit", "format", "help", "h"]),
  detail: new Set(["format", "help", "h"]),
}

function badArg(error: string, code = "BAD_ARG"): number {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
  return 1
}

async function main(): Promise<number> {
  const argv = process.argv.slice(2)
  const flags = parseFlags(argv)
  const cmd = (flags._ as string[])[0]

  if (!cmd || flags.help || flags.h) {
    process.stdout.write(HELP)
    return cmd ? 0 : 1
  }

  // Reject unknown flags instead of silently discarding them: a discarded
  // filter changes what the search returns with no error.
  const knownFlags = KNOWN_FLAGS[cmd]
  if (knownFlags) {
    for (const key of Object.keys(flags)) {
      if (key === "_" || knownFlags.has(key)) continue
      return badArg(
        `unknown flag --${key} for '${cmd}' - flags are never silently ignored, because a discarded filter changes what the search returns; see --help for the supported flags`,
        "UNKNOWN_FLAG",
      )
    }
  }

  if (cmd === "search") {
    // A present --query/--location needs a value: parseFlags stores a bare
    // flag as true, which would otherwise search all of Free-Work.
    for (const name of ["query", "location"]) {
      if (flags[name] !== undefined && (typeof flags[name] !== "string" || !(flags[name] as string).trim())) {
        return badArg(`--${name} requires a non-empty value`)
      }
    }
    const fmt = flags.format !== undefined ? String(flags.format) : "json"
    if (!["json", "table", "plain"].includes(fmt)) {
      return badArg(`--format must be one of json|table|plain, got "${fmt}"`)
    }

    let contracts: string[] | undefined
    if (flags.contract !== undefined) {
      if (typeof flags.contract !== "string") return badArg("--contract requires a value")
      contracts = []
      for (const part of flags.contract.split(",")) {
        const v = contractValue(part)
        if (!v) return badArg(`--contract values must be from ${CONTRACTS.join("|")} (or cdi|cdd|freelance|stage|alternance), got "${part.trim()}"`)
        if (!contracts.includes(v)) contracts.push(v)
      }
    }

    let remote: string | undefined
    if (flags.remote !== undefined) {
      const v = typeof flags.remote === "string" ? remoteValue(flags.remote) : null
      if (!v) {
        const got = typeof flags.remote === "string" ? `"${flags.remote}"` : "no value"
        return badArg(`--remote must be one of full|partial|none (or remote|hybrid|onsite), got ${got}`)
      }
      remote = v
    }

    const parseIntFlag = (name: string, raw: string | boolean | string[]): number | null => {
      // Number(), not parseInt(): parseInt truncates "0.5" to 0. Whole numbers >= 1 only.
      const val = typeof raw === "string" ? Number(raw.trim()) : NaN
      if (!Number.isInteger(val) || val < 1) {
        badArg(`--${name} must be a whole number of at least 1, got "${raw}"`)
        return null
      }
      return val
    }

    let jobage = 9999
    if (flags.jobage !== undefined) {
      const v = parseIntFlag("jobage", flags.jobage)
      if (v === null) return 1
      jobage = v
    }
    let page = 1
    if (flags.page !== undefined) {
      const v = parseIntFlag("page", flags.page)
      if (v === null) return 1
      page = v
    }
    let limit: number | undefined
    if (flags.limit !== undefined) {
      const v = parseIntFlag("limit", flags.limit)
      if (v === null) return 1
      limit = v
    }

    const opts: SearchOpts = {
      query: typeof flags.query === "string" ? flags.query : undefined,
      location: typeof flags.location === "string" ? flags.location : undefined,
      jobage,
      contracts,
      remote,
      page,
      limit,
      format: fmt as SearchOpts["format"],
    }
    return runSearch(opts)
  }

  if (cmd === "detail") {
    const id = (flags._ as string[])[1]
    if (!id) return badArg("detail requires a <slug|url>", "NO_ID")
    const fmt = flags.format !== undefined ? String(flags.format) : "json"
    if (!["json", "plain"].includes(fmt)) {
      return badArg(`--format must be one of json|plain, got "${fmt}"`)
    }
    const opts: DetailOpts = { id, format: fmt as DetailOpts["format"] }
    return runDetail(opts)
  }

  return badArg(`Unknown command "${cmd}"`, "BAD_CMD")
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    process.stderr.write(
      JSON.stringify({
        error: e instanceof Error ? e.message : String(e),
        code: "INTERNAL_ERROR",
      }) + "\n",
    )
    process.exit(1)
  })
