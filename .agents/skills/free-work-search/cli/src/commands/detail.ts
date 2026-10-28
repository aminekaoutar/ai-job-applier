import { API_BASE, apiFetch, stripInvisible, toDetail, writeError, type RawPosting } from "../helpers.js"

export interface DetailOpts {
  id: string
  format: "json" | "plain"
}

const FREE_WORK_HOST = /(^|\.)free-work\.com$/i
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

/**
 * Accept a posting slug (the `id` search emits) or a free-work.com posting URL
 * (`/fr/tech-it/<category>/job-mission/<slug>`). A URL on any other host is
 * rejected, read through real URL parsing so look-alike hosts
 * (free-work.com.evil.io) and userinfo tricks fall on the reject side.
 */
export function normalizeId(input: string): string | null {
  const trimmed = input.trim()
  if (SLUG.test(trimmed)) return trimmed

  const hasScheme = /^https?:\/\//i.test(trimmed)
  if (hasScheme || /^[a-z0-9.-]+\.[a-z]{2,}(\/|$)/i.test(trimmed)) {
    let parsed: URL
    try {
      parsed = new URL(hasScheme ? trimmed : `https://${trimmed}`)
    } catch {
      return null
    }
    if (!FREE_WORK_HOST.test(parsed.hostname)) return null
    const m = parsed.pathname.match(/\/job-mission\/([a-z0-9-]+)\/?$/i)
    return m && SLUG.test(m[1]) ? m[1] : null
  }
  return null
}

export async function runDetail(opts: DetailOpts): Promise<number> {
  const slug = normalizeId(opts.id)
  if (!slug) {
    writeError(`Could not parse a Free-Work posting slug from "${opts.id}"`, "BAD_ID")
    return 1
  }
  try {
    const raw = await apiFetch<RawPosting>(`${API_BASE}/job_postings/${slug}`)
    if (!raw || !raw.slug) {
      writeError("Job not found", "NOT_FOUND")
      return 1
    }
    const job = stripInvisible(toDetail(raw))

    if (opts.format === "plain") {
      const lines = [
        job.title,
        `${job.company || "—"} · ${job.location || "—"}`,
        "",
        job.contracts.length ? `Contracts: ${job.contracts.join(", ")}` : "",
        job.remoteMode ? `Remote: ${job.remoteMode}` : "",
        job.experienceLevel ? `Experience: ${job.experienceLevel}` : "",
        job.salary ? `Salary: ${job.salary}` : "",
        job.duration ? `Duration: ${job.duration}` : "",
        job.startsAt ? `Starts: ${job.startsAt}` : "",
        job.date ? `Published: ${job.date}` : "",
        job.expiresAt ? `Expires: ${job.expiresAt}` : "",
        job.skills.length ? `Skills: ${job.skills.join(", ")}` : "",
        `Status: ${job.isActive ? "ACTIVE" : "CLOSED / EXPIRED"}`,
        "",
        job.description || "(no description)",
        job.candidateProfile ? `\nProfil recherché:\n${job.candidateProfile}` : "",
        job.companyDescription ? `\nEntreprise / contexte:\n${job.companyDescription}` : "",
        "",
        `URL: ${job.url}`,
        job.applicationUrl ? `Apply: ${job.applicationUrl}` : "",
      ].filter((l) => l !== "")
      process.stdout.write(lines.join("\n") + "\n")
    } else {
      process.stdout.write(JSON.stringify(job, null, 2) + "\n")
    }
    return 0
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "DETAIL_FAILED")
    return 1
  }
}
