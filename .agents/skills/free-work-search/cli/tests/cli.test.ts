import { describe, expect, test } from "bun:test";
import { parseJSON, runCLI } from "./helpers";

// Argument validation never reaches the network.
describe("CLI contract", () => {
  const rejects = async (args: string[], code: string) => {
    const r = await runCLI(args);
    expect(r.exitCode).toBe(1);
    expect(r.stdout).toBe("");
    expect(JSON.parse(r.stderr).code).toBe(code);
  };

  test("unknown flag exits 1 with a JSON error", () => rejects(["search", "-q", "IA", "--bogus", "x"], "UNKNOWN_FLAG"));
  test("bare --query is rejected", () => rejects(["search", "--query"], "BAD_ARG"));
  test("bad --contract is rejected", () => rejects(["search", "-q", "IA", "-c", "cdi,gig"], "BAD_ARG"));
  test("bad --remote is rejected", () => rejects(["search", "-q", "IA", "--remote", "sometimes"], "BAD_ARG"));
  test("fractional --jobage is rejected", () => rejects(["search", "-q", "IA", "--jobage", "0.5"], "BAD_ARG"));
  test("bad --format is rejected", () => rejects(["search", "-q", "IA", "--format", "xml"], "BAD_ARG"));
  test("detail without an id is rejected", () => rejects(["detail"], "NO_ID"));
  test("detail with a foreign URL is rejected", () => rejects(["detail", "https://evil.io/job-mission/abc"], "BAD_ID"));
  test("unknown command is rejected", () => rejects(["crawl"], "BAD_CMD"));
});

// Live smoke test against free-work.com. Opt-in so CI stays offline:
//   FREE_WORK_LIVE=1 bun test
const live = process.env.FREE_WORK_LIVE === "1";

describe.skipIf(!live)("live smoke test", () => {
  test("search returns real postings, and detail reads one", async () => {
    type Out = { results: { id: string; title: string; url: string }[] };
    const out = parseJSON<Out>(await runCLI(["search", "-q", "IA générative", "-l", "France", "--limit", "3"]));
    expect(out.results.length).toBeGreaterThan(0);
    for (const r of out.results) {
      expect(r.id).toBeTruthy();
      expect(r.title).toBeTruthy();
      expect(r.url).toStartWith("https://www.free-work.com/");
    }
    const detail = parseJSON<{ description: string | null }>(await runCLI(["detail", out.results[0].id]));
    expect(detail.description).toBeTruthy();
  });
});
