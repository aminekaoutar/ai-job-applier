// Data source: Free-Work's public JSON API (API Platform / Hydra), the same
// endpoints www.free-work.com's own job search calls. No authentication.
//   search:   GET /api/job_postings?searchKeywords=...&contracts=...&locationKeys=...
//   detail:   GET /api/job_postings/<slug>
//   location: GET /api/locations/autocomplete?search=<place>  -> Free-Work location key
// robots.txt (checked 2026-10-28) disallows none of these paths for User-agent: *.

export const SITE = "https://www.free-work.com"
export const API_BASE = `${SITE}/api`

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

const UA = "Mozilla/5.0 (compatible; free-work-search-cli/1.0)"

/** Fetch JSON with exponential backoff on 429/5xx. Returns null on a 404. */
export async function apiFetch<T = unknown>(url: string): Promise<T | null> {
  const maxRetries = 6
  let delay = 500
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const response = await fetch(url, {
      headers: {
        "User-Agent": UA,
        Accept: "application/ld+json",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
      },
      redirect: "follow",
      signal: AbortSignal.timeout(15000),
    })
    if (response.status === 429 || response.status >= 500) {
      if (attempt === maxRetries) {
        throw new Error(`Request failed: ${response.status} ${response.statusText}`)
      }
      const jitter = Math.floor(Math.random() * 500)
      await new Promise((r) => setTimeout(r, delay + jitter))
      delay = Math.min(delay * 2, 8000)
      continue
    }
    if (response.status === 404) return null
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status} ${response.statusText}`)
    }
    return (await response.json()) as T
  }
  throw new Error("Request failed after max retries")
}

/**
 * Convert a Unicode code point to a string. Uses `fromCodePoint` (not
 * `fromCharCode`) so supplementary-plane code points (e.g. emoji, U+1F600)
 * decode correctly, and drops out-of-range values instead of throwing.
 */
function numericEntity(cp: number): string {
  return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : ""
}

function decodeHtmlEntities(text: string): string {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    // Numeric character references: decimal (&#233;) and hexadecimal (&#xE9;).
    .replace(/&#(\d+);/g, (_, dec) => numericEntity(parseInt(dec, 10)))
    .replace(/&#[xX]([0-9a-fA-F]+);/g, (_, hex) => numericEntity(parseInt(hex, 16)))
    .replace(/&nbsp;/g, " ")
}

/**
 * Characters a reader of the posting never sees but a model reading this
 * output does: C0/C1 controls other than tab, newline and carriage return, the
 * soft hyphen, bidi marks/embeddings/overrides/isolates, zero-width characters
 * and invisible operators, the BOM, variation selectors, and Unicode tag
 * characters (U+E0000-E007F), which can spell out a whole hidden instruction.
 * Listing text is third-party data, and these arrive raw in a portal's JSON or
 * markup as easily as through a numeric entity.
 */
const INVISIBLE_CHARS =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F­؜᠎​-‏‪-‮⁠-⁯︀-️﻿\u{E0000}-\u{E007F}\u{E0100}-\u{E01EF}]/gu

/** Strip INVISIBLE_CHARS from every string in a result, recursively: the CLI's output boundary. */
export function stripInvisible<T>(value: T): T {
  if (typeof value === "string") return value.replace(INVISIBLE_CHARS, "") as T
  if (Array.isArray(value)) return value.map(stripInvisible) as T
  if (value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, stripInvisible(v)])) as T
  }
  return value
}

const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"])

/** Whether an opening tag's attributes keep it from rendering: `hidden`, or an inline style that hides it. */
function isHiddenTag(attrs: string): boolean {
  for (const m of attrs.matchAll(/([^\s"'=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g)) {
    const name = m[1].toLowerCase()
    const value = m[2] ?? m[3] ?? m[4] ?? ""
    if (name === "hidden") return true
    if (name === "style" && /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all)\b/i.test(value)) {
      return true
    }
  }
  return false
}

/**
 * Drop markup a browser never renders - comments (including Word's
 * conditional ones), <script>/<style>/<template> blocks, and elements marked
 * hidden - before tags are stripped. A plain tag strip keeps their text, which
 * hands the model words the posting's reader never saw. CSS-only tricks
 * (off-screen, zero font size) need a renderer and are not caught here.
 */
export function visibleHtml(html: string): string {
  const src = html
    .replace(/<!--[\s\S]*?(?:-->|$)/g, "")
    .replace(/<(script|style|template)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gi, "")
  // One pass, collecting what is kept: slicing the string per hidden element
  // would make a page full of them quadratic.
  const open = /<([a-zA-Z][a-zA-Z0-9-]*)(\s[^>]*)?>/g
  const kept: string[] = []
  let from = 0
  let m: RegExpExecArray | null
  while ((m = open.exec(src)) !== null) {
    if (!m[2] || !isHiddenTag(m[2])) continue
    const tag = m[1].toLowerCase()
    let end = open.lastIndex
    if (!VOID_TAGS.has(tag) && !m[0].endsWith("/>")) {
      // Skip to the matching close tag, counting nested same-name elements. An
      // unclosed hidden element runs to the end: failing closed drops visible
      // text, failing open would leak the hidden text.
      const same = new RegExp(`<(/?)${tag}\\b[^>]*>`, "gi")
      same.lastIndex = end
      end = src.length
      let depth = 1
      let s: RegExpExecArray | null
      while ((s = same.exec(src)) !== null) {
        if (s[1]) depth--
        else if (!s[0].endsWith("/>")) depth++
        if (depth === 0) {
          end = same.lastIndex
          break
        }
      }
    }
    kept.push(src.slice(from, m.index))
    from = end
    open.lastIndex = end
  }
  kept.push(src.slice(from))
  return kept.join("")
}

function stripTags(html: string): string {
  return visibleHtml(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()
}

function clean(html: string): string {
  return decodeHtmlEntities(stripTags(html))
}


/**
 * Render a posting's HTML body as readable text: hidden markup dropped first,
 * then one line per paragraph/list item, entities decoded.
 */
export function htmlToText(html: string | null | undefined): string | null {
  if (!html) return null
  // Hidden markup goes before the line split: a hidden element can span the
  // breaks, and cleaning line by line would only see half of it.
  const withBreaks = visibleHtml(html)
    .replace(/\s+/g, " ")
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<li\b[^>]*>/gi, "\n- ")
    .replace(/<\/(p|li|ul|ol|div|h\d)>/gi, "\n")
  const text = withBreaks
    .split("\n")
    .map((line) => (line.startsWith("- ") ? "- " + clean(line.slice(2)) : clean(line)))
    // Free-Work bodies nest <p> inside <li>, so every closing tag yields a
    // break; one line per block reads cleanly.
    .filter((line) => line !== "" && line !== "-")
    .join("\n")
    .trim()
  return text || null
}

// --- Free-Work API shapes (only the fields this CLI reads) ---

export interface RawLocation {
  locality?: string | null
  adminLevel1?: string | null
  adminLevel2?: string | null
  country?: string | null
  countryCode?: string | null
  key?: string | null
  label?: string | null
  shortLabel?: string | null
}

export interface RawPosting {
  id: number
  slug: string
  title: string
  description?: string | null
  candidateProfile?: string | null
  companyDescription?: string | null
  experienceLevel?: string | null
  minAnnualSalary?: number | null
  maxAnnualSalary?: number | null
  minDailySalary?: number | null
  maxDailySalary?: number | null
  currency?: string | null
  durationValue?: number | null
  durationPeriod?: string | null
  remoteMode?: string | null
  applicationUrl?: string | null
  location?: RawLocation | null
  startsAt?: string | null
  company?: { name?: string | null; slug?: string | null } | null
  job?: { slug?: string | null } | null
  skills?: { name?: string | null }[] | null
  publishedAt?: string | null
  expiredAt?: string | null
  status?: string | null
  published?: boolean | null
  contracts?: string[] | null
}

export interface HydraCollection<T> {
  "hydra:member"?: T[]
  "hydra:totalItems"?: number
}

export interface JobCard {
  id: string
  title: string
  company: string | null
  location: string | null
  date: string | null
  url: string
  contracts: string[]
  remoteMode: string | null
  experienceLevel: string | null
  salary: string | null
  skills: string[]
}

export interface JobDetail extends JobCard {
  description: string | null
  candidateProfile: string | null
  companyDescription: string | null
  duration: string | null
  startsAt: string | null
  expiresAt: string | null
  applicationUrl: string | null
  isActive: boolean
}

/** Public posting page. The site routes on the job-category slug plus the posting slug. */
export function postingUrl(raw: RawPosting): string {
  const category = raw.job?.slug || "emploi"
  return `${SITE}/fr/tech-it/${category}/job-mission/${raw.slug}`
}

function locationLabel(loc: RawLocation | null | undefined): string | null {
  if (!loc) return null
  return loc.label || [loc.locality, loc.adminLevel1, loc.country].filter(Boolean).join(", ") || null
}

function formatSalary(raw: RawPosting): string | null {
  const cur = raw.currency || "EUR"
  const range = (min?: number | null, max?: number | null): string | null => {
    if (min && max && min !== max) return `${min}-${max}`
    if (min || max) return String(min || max)
    return null
  }
  const annual = range(raw.minAnnualSalary, raw.maxAnnualSalary)
  const daily = range(raw.minDailySalary, raw.maxDailySalary)
  const parts: string[] = []
  if (annual) parts.push(`${annual} ${cur}/an`)
  if (daily) parts.push(`${daily} ${cur}/jour`)
  return parts.length ? parts.join(" · ") : null
}

export function toCard(raw: RawPosting): JobCard {
  return {
    id: raw.slug,
    title: clean(raw.title || "") || "(untitled)",
    company: raw.company?.name ? clean(raw.company.name) || null : null,
    location: locationLabel(raw.location),
    date: raw.publishedAt ? raw.publishedAt.slice(0, 10) : null,
    url: postingUrl(raw),
    contracts: raw.contracts ?? [],
    remoteMode: raw.remoteMode ?? null,
    experienceLevel: raw.experienceLevel ?? null,
    salary: formatSalary(raw),
    skills: (raw.skills ?? []).map((s) => (s.name ? clean(s.name) : "")).filter(Boolean),
  }
}

export function toDetail(raw: RawPosting, now: Date = new Date()): JobDetail {
  const expired = raw.expiredAt ? new Date(raw.expiredAt).getTime() < now.getTime() : false
  const duration =
    raw.durationValue && raw.durationPeriod ? `${raw.durationValue} ${raw.durationPeriod}` : null
  return {
    ...toCard(raw),
    description: htmlToText(raw.description),
    candidateProfile: htmlToText(raw.candidateProfile),
    companyDescription: htmlToText(raw.companyDescription),
    duration,
    startsAt: raw.startsAt ? raw.startsAt.slice(0, 10) : null,
    expiresAt: raw.expiredAt ? raw.expiredAt.slice(0, 10) : null,
    applicationUrl: raw.applicationUrl ?? null,
    // Absence of a closed marker is not proof the posting is open; this only
    // reports that the API still lists it as published and not past expiry.
    isActive: raw.published !== false && (raw.status ?? "published") === "published" && !expired,
  }
}

// --- Flag mappings ---

/**
 * Free-Work's `publishedSince` only takes four windows. Round the requested
 * age UP to the next window so nothing in range is dropped server-side; the
 * search command then trims to the exact day count client-side.
 */
export function jobageToPublishedSince(days: number): string | null {
  if (!days || days <= 0 || days >= 9999) return null
  if (days <= 1) return "less_than_24_hours"
  if (days <= 7) return "less_than_7_days"
  if (days <= 14) return "less_than_14_days"
  if (days <= 30) return "less_than_30_days"
  return null
}

export const CONTRACTS = ["permanent", "fixed-term", "contractor", "internship", "apprenticeship"] as const

/** Accept English and French contract names; return the API value or null. */
export function contractValue(input: string): string | null {
  const v = input.trim().toLowerCase()
  const map: Record<string, string> = {
    permanent: "permanent",
    cdi: "permanent",
    "fixed-term": "fixed-term",
    cdd: "fixed-term",
    contractor: "contractor",
    freelance: "contractor",
    internship: "internship",
    stage: "internship",
    apprenticeship: "apprenticeship",
    alternance: "apprenticeship",
  }
  return map[v] ?? null
}

/** --remote values: the API's own names plus the hybrid/onsite/remote synonyms other portal CLIs use. */
export function remoteValue(input: string): string | null {
  const map: Record<string, string> = {
    full: "full",
    remote: "full",
    partial: "partial",
    hybrid: "partial",
    none: "none",
    onsite: "none",
    "on-site": "none",
  }
  return map[input.trim().toLowerCase()] ?? null
}

/** A raw Free-Work location key looks like "fr~ile-de-france~paris~". */
export function isLocationKey(input: string): boolean {
  return /^[a-z]{2}~[a-z0-9-]*~[a-z0-9-]*~[a-z0-9-]*$/.test(input.trim())
}

/**
 * Resolve a place name ("Paris", "Lyon", "Île-de-France", "France") to a
 * Free-Work location key via the site's own autocomplete. French matches win
 * over same-named places abroad (Lyon, Minnesota). Returns null when nothing
 * matches, so the caller fails loudly instead of searching all of Free-Work.
 */
export async function resolveLocation(place: string): Promise<{ key: string; label: string } | null> {
  if (isLocationKey(place)) return { key: place.trim(), label: place.trim() }
  const list = await apiFetch<RawLocation[] | HydraCollection<RawLocation>>(
    `${API_BASE}/locations/autocomplete?search=${encodeURIComponent(place.trim())}`,
  )
  const items = Array.isArray(list) ? list : (list?.["hydra:member"] ?? [])
  const withKey = items.filter((l) => l.key)
  const pick = withKey.find((l) => l.countryCode === "FR") ?? withKey[0]
  return pick?.key ? { key: pick.key, label: pick.label || pick.key } : null
}
