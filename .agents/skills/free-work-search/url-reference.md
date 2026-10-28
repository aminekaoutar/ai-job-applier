# Free-Work URL Reference

Investigated 2026-10-28. Free-Work's frontend is backed by an API Platform (Hydra
JSON-LD) API on the same host. All endpoints below are public, need no auth, and
are not disallowed by `https://www.free-work.com/robots.txt` for `User-agent: *`.
The honest UA `Mozilla/5.0 (compatible; free-work-search-cli/1.0)` is accepted.

Request with `Accept: application/ld+json` to get the Hydra envelope
(`hydra:member`, `hydra:totalItems`); `application/json` returns a bare array.

## Search

`GET https://www.free-work.com/api/job_postings`

| Parameter | Values | Notes |
|-----------|--------|-------|
| `searchKeywords` | free text | Full-text over title, description and skills. Accents fine. |
| `locationKeys` | Free-Work location key, e.g. `fr~ile-de-france~~`, `fr~ile-de-france~paris~`, `fr~auvergne-rhone-alpes~rhone~lyon`, `fr~~~` | Includes sub-areas. Resolve names via the autocomplete endpoint below. |
| `contracts` | comma list of `permanent`, `fixed-term`, `contractor`, `internship`, `apprenticeship` | Matches postings offering **any** of them. `contracts[]=` returns 400. |
| `remoteMode` | `full`, `partial`, `none` | `remoteMode[]=` returns 400. |
| `publishedSince` | `less_than_24_hours`, `less_than_7_days`, `less_than_14_days`, `less_than_30_days` | Any other value is silently ignored (returns all ages) - hence the CLI maps `--jobage` itself. |
| `itemsPerPage` | int | CLI uses 20. |
| `page` | int, 1-indexed | |

`order[...]` returns 400; results come back in relevance order.

## Detail

`GET https://www.free-work.com/api/job_postings/<slug>`

The numeric `id` returns 404 here - the slug is the item identifier. `?slug=` and
`?id=` on the collection are ignored (return everything), so never use them.

## Location autocomplete

`GET https://www.free-work.com/api/locations/autocomplete?search=<place>`

Returns a bare array of locations with `key`, `label`, `countryCode`. Same-named
places abroad appear (`Lyon` -> also `us~minnesota~lyon~`), so prefer `countryCode: FR`.

## Public posting URL

`https://www.free-work.com/fr/tech-it/<job.slug>/job-mission/<slug>`

`job.slug` is the posting's job-category slug (e.g. `lead-developer`, `directeur-de-projet`).

## Posting fields used

| Field | Notes |
|-------|-------|
| `slug` | CLI `id`; detail key |
| `title` | plain text |
| `company.name` | |
| `location.label` | e.g. `Paris, Île-de-France`; `locality` may be null for region-level postings |
| `publishedAt` | ISO 8601 with offset |
| `expiredAt` | ISO 8601; past = closed |
| `status`, `published` | `"published"`, `true` while live |
| `contracts` | array of the contract values above |
| `remoteMode` | `full` / `partial` / `none` / null |
| `experienceLevel` | `junior` / `intermediate` / `senior` / `expert` / null |
| `min/maxAnnualSalary`, `min/maxDailySalary`, `currency` | daily = TJM for contractor postings |
| `durationValue`, `durationPeriod`, `startsAt` | mission length / start |
| `description`, `candidateProfile`, `companyDescription` | HTML (`<p>`, `<ul><li><p>`), third-party content |
| `skills[].name` | tags |
| `applicationUrl` | external apply link when set (usually null - apply on Free-Work) |
