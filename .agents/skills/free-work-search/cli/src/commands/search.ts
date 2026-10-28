import {
  API_BASE,
  apiFetch,
  jobageToPublishedSince,
  resolveLocation,
  stripInvisible,
  toCard,
  writeError,
  type HydraCollection,
  type JobCard,
  type RawPosting,
} from "../helpers.js"

export interface SearchOpts {
  query?: string
  location?: string
  jobage: number
  contracts?: string[] // API values: permanent | fixed-term | contractor | internship | apprenticeship
  remote?: string // API values: full | partial | none
  page: number
  limit?: number
  format: "json" | "table" | "plain"
}

/** Server page size; Free-Work accepts larger values but 20 matches /scrape's per-call cap. */
const PAGE_SIZE = 20

export function buildUrl(opts: SearchOpts, locationKey?: string): string {
  const params = new URLSearchParams()
  if (opts.query) params.set("searchKeywords", opts.query)
  if (locationKey) params.set("locationKeys", locationKey)
  if (opts.contracts?.length) params.set("contracts", opts.contracts.join(","))
  if (opts.remote) params.set("remoteMode", opts.remote)
  const since = jobageToPublishedSince(opts.jobage)
  if (since) params.set("publishedSince", since)
  params.set("itemsPerPage", String(PAGE_SIZE))
  params.set("page", String(opts.page))
  return `${API_BASE}/job_postings?${params.toString()}`
}

/** Trim to the exact --jobage window: the API only offers 1/7/14/30-day buckets. */
export function withinJobage(card: JobCard, days: number, now: Date = new Date()): boolean {
  if (!days || days >= 9999 || !card.date) return true
  const cutoff = now.getTime() - days * 86400 * 1000
  return new Date(card.date + "T23:59:59Z").getTime() >= cutoff
}

function renderTable(cards: JobCard[]): string {
  if (cards.length === 0) return "No results."
  const rows = cards.map((c) => {
    const title = (c.title || "").slice(0, 44).padEnd(44)
    const company = (c.company || "—").slice(0, 22).padEnd(22)
    const loc = (c.location || "—").slice(0, 22).padEnd(22)
    const contract = (c.contracts.join(",") || "—").slice(0, 20).padEnd(20)
    return `${title} ${company} ${loc} ${contract} ${c.date || "—"}`
  })
  const header =
    "TITLE".padEnd(44) + " " + "COMPANY".padEnd(22) + " " + "LOCATION".padEnd(22) + " " + "CONTRACTS".padEnd(20) + " DATE"
  return [header, "-".repeat(header.length), ...rows].join("\n")
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  try {
    let locationKey: string | undefined
    let locationLabel: string | null = null
    if (opts.location) {
      const resolved = await resolveLocation(opts.location)
      if (!resolved) {
        writeError(
          `Free-Work knows no location matching "${opts.location}" - try a French city or region name, or a raw key like "fr~ile-de-france~~"`,
          "BAD_LOCATION",
        )
        return 1
      }
      locationKey = resolved.key
      locationLabel = resolved.label
    }

    const data = await apiFetch<HydraCollection<RawPosting>>(buildUrl(opts, locationKey))
    const raw = data?.["hydra:member"] ?? []
    let cards = stripInvisible(raw.map(toCard)).filter((c) => withinJobage(c, opts.jobage))
    if (opts.limit !== undefined && opts.limit >= 0) cards = cards.slice(0, opts.limit)

    if (opts.format === "table") {
      process.stdout.write(renderTable(cards) + "\n")
    } else if (opts.format === "plain") {
      process.stdout.write(
        cards
          .map(
            (c) =>
              `${c.title}\n  ${c.company || "—"} · ${c.location || "—"} · ${c.contracts.join(", ") || "—"} · ${c.date || "—"}\n  id: ${c.id}\n  ${c.url}`,
          )
          .join("\n\n") + "\n",
      )
    } else {
      process.stdout.write(
        JSON.stringify(
          {
            meta: {
              count: cards.length,
              page: opts.page,
              total: data?.["hydra:totalItems"] ?? null,
              location: locationKey ? { key: locationKey, label: locationLabel } : null,
            },
            results: cards,
          },
          null,
          2,
        ) + "\n",
      )
    }
    return 0
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "SEARCH_FAILED")
    return 1
  }
}
