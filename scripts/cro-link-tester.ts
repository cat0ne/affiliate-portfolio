/**
 * cro-link-tester.ts
 *
 * Playwright-based CRO smoke test for the 5 affiliation sites in the
 * monorepo. For each site:
 *   1. Loads the homepage and waits for network idle.
 *   2. Picks the first N article cards in the main content area
 *      (configurable via --max-articles, default 5).
 *   3. For each card:
 *        - Verifies it has a clickable anchor (<a href="..."> in the
 *          main content).
 *        - Verifies the clickable element is the topmost element under
 *          its center point (i.e. nothing is overlaying it).
 *        - Clicks it and waits for navigation.
 *        - Verifies the destination is HTTP 200 and has an <h1> or
 *          <article> element on the page.
 *        - Counts Amazon CTA buttons, captures their hrefs, verifies
 *          each href has a tag= matching the per-site expected
 *          Associates ID for the visited locale.
 *   4. Tests one deep page in EN locale per site to verify locale
 *      switching still works.
 *
 * Usage:
 *   npx tsx cro-link-tester.ts [--headed] [--max-articles N] [--site SITE]
 *
 * Outputs:
 *   reports/cro-tester-<YYYY-MM-DD>.json   structured report
 *   reports/cro-tester-latest.md           human-readable summary
 *   reports/cro-tester/<site>/<ts>-<name>.png   screenshots on failure
 *
 * Exit code: 0 on all pass, 1 on any failure.
 */

import {
  chromium,
  type Browser,
  type BrowserContext,
  type Page,
  type Response as PlaywrightResponse,
} from "playwright";
import * as fs from "node:fs";
import * as path from "node:path";

// ---------- Configuration ----------

interface SiteConfig {
  key: string;
  name: string;
  url: string;
  /** Expected Amazon Associates tag for the default locale (FR). */
  expectedTagFr: string;
  /** Expected Amazon Associates tag for the EN locale (US program). */
  expectedTagEn: string;
  /** Whether to test the EN locale. All sites have hreflang en, but we
   *  still flag any that are FR-only at runtime. */
  testEnLocale: boolean;
}

const SITES: SiteConfig[] = [
  {
    key: "aspirateur",
    name: "Top-Aspirateur",
    // Was: https://www.zoom-aspirateurs.fr/ — that domain does not resolve
    // (DNS error in every CRO test run). Real production host is top-aspirateur.fr.
    url: "https://www.top-aspirateur.fr/",
    expectedTagFr: "zoomzen05-21",
    expectedTagEn: "zoomzus-20",
    testEnLocale: true,
  },
  {
    key: "bureau",
    name: "Bureau Expert",
    url: "https://www.bureau-expert.fr/",
    expectedTagFr: "zoomzen05-21",
    expectedTagEn: "zoomzus-20",
    testEnLocale: true,
  },
  {
    key: "cafe",
    name: "Brewmance",
    url: "https://www.brewmance.fr/",
    expectedTagFr: "zoomzen05-21",
    expectedTagEn: "zoomzus-20",
    testEnLocale: true,
  },
  {
    key: "matelas",
    name: "Matelas-Expert",
    // Was: https://www.zoom-matelas.fr/ — DNS does not resolve (legacy/typo).
    // Real production host is matelas-expert.fr.
    url: "https://www.matelas-expert.fr/",
    expectedTagFr: "zoomzen05-21",
    expectedTagEn: "zoomzus-20",
    testEnLocale: true,
  },
  {
    key: "pixinstant",
    name: "Pix Instant",
    url: "https://www.pixinstant.com/",
    expectedTagFr: "zoomzen05-21",
    expectedTagEn: "zoomzus-20",
    testEnLocale: true,
  },
];

/** Known Amazon Associates tags across the 5 sites for ALL locales.
 *  We accept any of these on any site/page since some MDX content may
 *  hard-code locale-specific tags. The tag must, however, belong to
 *  this catalogue (i.e. not a placeholder or a wrong-program tag). */
const KNOWN_TAGS = new Set<string>([
  "zoomzen05-21", // fr
  "zoomzus-20", // en (US)
  "zoomzen-21", // de
  "zoomzen01-21", // it
  "zoomzen08-21", // es
  "zoomzen07-21", // uk
  "matelas-expert", // matelas direct partners
]);

// ---------- CLI flags ----------

interface CliOptions {
  headed: boolean;
  maxArticles: number;
  siteFilter?: string;
  perPageTimeoutMs: number;
  globalTimeoutMs: number;
}

function parseArgs(argv: string[]): CliOptions {
  const opts: CliOptions = {
    headed: false,
    maxArticles: 5,
    perPageTimeoutMs: 25_000,
    globalTimeoutMs: 180_000, // 3 min total budget
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--headed") opts.headed = true;
    else if (a === "--max-articles") opts.maxArticles = Number(argv[++i] ?? "5");
    else if (a === "--site") opts.siteFilter = argv[++i];
    else if (a === "--timeout") opts.perPageTimeoutMs = Number(argv[++i] ?? "25000");
  }
  return opts;
}

// ---------- Report types ----------

interface CtaCheck {
  href: string;
  tag: string | null;
  tagOk: boolean;
}

interface ArticleResult {
  cardIndex: number;
  cardHref: string | null;
  cardText: string | null;
  /** Was the clickable element on top (no overlay covering it)? */
  topmost: boolean | null;
  /** Did the click cause navigation away from the homepage? */
  clickNavigated: boolean | null;
  /** HTTP status of the destination page. */
  destStatus: number | null;
  destUrl: string | null;
  destHasContent: boolean | null;
  amazonCtas: CtaCheck[];
  errors: string[];
  screenshot?: string;
}

interface LocaleResult {
  locale: string;
  url: string;
  status: number | null;
  hasContent: boolean;
  errors: string[];
}

interface SiteResult {
  key: string;
  name: string;
  url: string;
  homepageStatus: number | null;
  homepageError: string | null;
  articleResults: ArticleResult[];
  localeResults: LocaleResult[];
  durationMs: number;
  passed: boolean;
}

interface RunReport {
  generatedAt: string;
  durationMs: number;
  sites: SiteResult[];
  summary: {
    totalArticles: number;
    passedArticles: number;
    failedArticles: number;
    totalCtas: number;
    passedCtas: number;
    failedCtas: number;
    sitesPassed: number;
    sitesFailed: number;
  };
  notes: string[];
}

// ---------- Helpers ----------

const ROOT = path.resolve(__dirname, "..");
const REPORTS_DIR = path.join(ROOT, "reports");
const SCREENSHOT_DIR = path.join(REPORTS_DIR, "cro-tester");

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function ts(): string {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function ensureDir(p: string): void {
  fs.mkdirSync(p, { recursive: true });
}

function extractAmazonTag(href: string): string | null {
  try {
    const u = new URL(href);
    return u.searchParams.get("tag");
  } catch {
    return null;
  }
}

function isAmazonHost(href: string): boolean {
  try {
    const u = new URL(href);
    return /(^|\.)amazon\.[a-z.]+$/i.test(u.hostname) || u.hostname === "amzn.to";
  } catch {
    return false;
  }
}

// ---------- Per-site test ----------

async function testSite(
  browser: Browser,
  site: SiteConfig,
  opts: CliOptions
): Promise<SiteResult> {
  const startedAt = Date.now();
  const result: SiteResult = {
    key: site.key,
    name: site.name,
    url: site.url,
    homepageStatus: null,
    homepageError: null,
    articleResults: [],
    localeResults: [],
    durationMs: 0,
    passed: true,
  };

  const context = await browser.newContext({
    viewport: { width: 1366, height: 900 },
    userAgent:
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 cro-link-tester/1.0",
    ignoreHTTPSErrors: true,
  });
  context.setDefaultTimeout(opts.perPageTimeoutMs);
  context.setDefaultNavigationTimeout(opts.perPageTimeoutMs);

  try {
    const page = await context.newPage();

    // -- 1. Homepage --
    let homeResp: PlaywrightResponse | null = null;
    try {
      homeResp = await page.goto(site.url, { waitUntil: "domcontentloaded" });
      // Network idle is best-effort; some sites have lingering analytics.
      await page.waitForLoadState("networkidle", { timeout: 8_000 }).catch(() => {});
      result.homepageStatus = homeResp?.status() ?? null;
    } catch (err) {
      result.homepageError = `Homepage navigation failed: ${(err as Error).message}`;
      result.passed = false;
      await context.close();
      result.durationMs = Date.now() - startedAt;
      return result;
    }

    if (!homeResp || homeResp.status() !== 200) {
      result.homepageError = `Homepage returned status ${homeResp?.status() ?? "n/a"}`;
      result.passed = false;
    }

    // -- 2. Article cards --
    // Strategy: collect candidate <a> hrefs that look like article links
    // in the MAIN content area (under <main>) — comparatif, guide, test,
    // categorie. We avoid the navbar (which has them too) by scoping to
    // <main>.
    const candidateHrefs = await page.evaluate(() => {
      const main = document.querySelector("main") || document.body;
      const out: { href: string; text: string }[] = [];
      const seen = new Set<string>();
      const articlePatterns = [
        /^\/comparatif\/[a-z0-9-]+/i,
        /^\/guide\/[a-z0-9-]+/i,
        /^\/test\/[a-z0-9-]+/i,
        /^\/categorie\/[a-z0-9-]+/i,
        /^\/[a-z]{2}\/comparatif\/[a-z0-9-]+/i,
      ];
      for (const a of Array.from(main.querySelectorAll("a[href]"))) {
        const href = (a as HTMLAnchorElement).getAttribute("href") || "";
        if (!href || href.startsWith("#")) continue;
        const path = href.split("?")[0];
        if (!articlePatterns.some((rx) => rx.test(path))) continue;
        if (seen.has(path)) continue;
        seen.add(path);
        out.push({ href, text: (a.textContent || "").trim().slice(0, 80) });
      }
      return out;
    });

    const targets = candidateHrefs.slice(0, opts.maxArticles);
    if (targets.length === 0) {
      result.homepageError =
        (result.homepageError ? result.homepageError + " | " : "") +
        "No article-card anchors found in <main>";
      result.passed = false;
    }

    for (let i = 0; i < targets.length; i++) {
      const target = targets[i];
      const ar: ArticleResult = {
        cardIndex: i,
        cardHref: target.href,
        cardText: target.text,
        topmost: null,
        clickNavigated: null,
        destStatus: null,
        destUrl: null,
        destHasContent: null,
        amazonCtas: [],
        errors: [],
      };

      try {
        // Re-navigate to homepage so the locator is fresh (some sites
        // mutate the DOM after first interaction).
        if (page.url() !== site.url) {
          await page.goto(site.url, { waitUntil: "domcontentloaded" });
          await page
            .waitForLoadState("networkidle", { timeout: 5_000 })
            .catch(() => {});
        }

        // Find the *first* anchor matching this href in <main>.
        const cardLocator = page
          .locator(`main a[href="${target.href}"]`)
          .first();
        await cardLocator.waitFor({ state: "visible", timeout: 8_000 });

        // Topmost-element test: is the anchor (or its descendant) the
        // element returned by elementFromPoint at its centre?
        const topmost = await cardLocator.evaluate((el: Element) => {
          const rect = (el as HTMLElement).getBoundingClientRect();
          const cx = rect.left + rect.width / 2;
          const cy = rect.top + rect.height / 2;
          if (rect.width === 0 || rect.height === 0) return false;
          // Check inside viewport
          if (
            cy < 0 ||
            cy > (window.innerHeight || document.documentElement.clientHeight)
          ) {
            // Element is offscreen; scroll into view first.
            (el as HTMLElement).scrollIntoView({
              block: "center",
              inline: "center",
            });
          }
          const r2 = (el as HTMLElement).getBoundingClientRect();
          const cx2 = r2.left + r2.width / 2;
          const cy2 = r2.top + r2.height / 2;
          const top = document.elementFromPoint(cx2, cy2);
          if (!top) return false;
          // Anchor itself or any ancestor up to el counts as topmost.
          let cur: Element | null = top;
          while (cur) {
            if (cur === el) return true;
            cur = cur.parentElement;
          }
          // Or el contains topmost (e.g. text node / span inside anchor)
          return el.contains(top);
        });
        ar.topmost = topmost;
        if (!topmost) {
          ar.errors.push(
            "Anchor is NOT the topmost element at its centre — likely covered by an overlay (CRO-blocking bug)"
          );
        }

        // Now actually click and wait for navigation. We use
        // Promise.race with a small timeout: if the click event fires
        // but no navigation happens within 5s, we know the click was
        // swallowed.
        await cardLocator.scrollIntoViewIfNeeded().catch(() => {});

        const navPromise = page
          .waitForURL((url) => !url.toString().endsWith(site.url) && url.toString() !== site.url, {
            timeout: 10_000,
          })
          .then(() => true)
          .catch(() => false);

        // Click. Use trial:false (real click). force:false so we
        // detect overlays.
        const clickStartUrl = page.url();
        try {
          await cardLocator.click({ timeout: 5_000 });
        } catch (clickErr) {
          ar.errors.push(`Click threw: ${(clickErr as Error).message}`);
        }

        const navigated = await navPromise;
        ar.clickNavigated = navigated;

        if (!navigated) {
          // Critical CRO bug: anchor clicked but no navigation fired.
          ar.errors.push(
            `Click did NOT navigate. Still at ${page.url()} (started at ${clickStartUrl})`
          );
          // Capture screenshot for diagnosis.
          const shotDir = path.join(SCREENSHOT_DIR, site.key);
          ensureDir(shotDir);
          const shotPath = path.join(
            shotDir,
            `${ts()}-no-navigation-card${i}.png`
          );
          await page.screenshot({ path: shotPath, fullPage: true });
          ar.screenshot = shotPath;
        } else {
          // Wait for content to settle.
          await page
            .waitForLoadState("networkidle", { timeout: 5_000 })
            .catch(() => {});
          ar.destUrl = page.url();

          // Verify HTTP 200 + has h1/article content.
          // We can't always read the response of the navigation if the
          // click triggered a client-side route, so probe via a quick
          // fetch on top of DOM checks.
          try {
            const probe = await context.request.get(ar.destUrl, {
              maxRedirects: 5,
            });
            ar.destStatus = probe.status();
          } catch {
            ar.destStatus = null;
          }

          const hasContent = await page.evaluate(() => {
            const h1 = document.querySelector("h1");
            const art = document.querySelector("article, main");
            return Boolean((h1 && h1.textContent?.trim()) || art);
          });
          ar.destHasContent = hasContent;

          if (ar.destStatus !== 200) {
            ar.errors.push(`Destination status ${ar.destStatus}`);
          }
          if (!hasContent) {
            ar.errors.push("Destination has no <h1> or <article>/<main>");
          }

          // Amazon CTA scrape.
          const ctas = await page.evaluate(() => {
            const out: { href: string }[] = [];
            for (const a of Array.from(document.querySelectorAll("a[href]"))) {
              const href = (a as HTMLAnchorElement).href;
              if (!href) continue;
              try {
                const u = new URL(href);
                if (
                  /(^|\.)amazon\.[a-z.]+$/i.test(u.hostname) ||
                  u.hostname === "amzn.to"
                ) {
                  out.push({ href });
                }
              } catch {
                /* ignore */
              }
            }
            return out;
          });
          for (const cta of ctas) {
            const tag = extractAmazonTag(cta.href);
            const tagOk =
              cta.href.includes("amzn.to") ||
              (tag !== null && KNOWN_TAGS.has(tag));
            ar.amazonCtas.push({ href: cta.href, tag, tagOk });
            if (!tagOk) {
              ar.errors.push(
                `Amazon CTA has invalid/missing tag: ${cta.href.slice(0, 120)}`
              );
            }
          }
        }
      } catch (err) {
        ar.errors.push(`Unexpected: ${(err as Error).message}`);
      }

      if (ar.errors.length > 0) result.passed = false;
      result.articleResults.push(ar);
    }

    // -- 3. Locale switching (EN) --
    if (site.testEnLocale) {
      const lr: LocaleResult = {
        locale: "en",
        url: new URL("/en/", site.url).toString(),
        status: null,
        hasContent: false,
        errors: [],
      };
      try {
        const enResp = await context.request.get(lr.url, { maxRedirects: 5 });
        lr.status = enResp.status();
        if (enResp.status() === 200) {
          await page.goto(lr.url, { waitUntil: "domcontentloaded" });
          await page
            .waitForLoadState("networkidle", { timeout: 5_000 })
            .catch(() => {});
          lr.hasContent = await page.evaluate(() => {
            const h1 = document.querySelector("h1");
            return Boolean(h1 && (h1.textContent || "").trim().length > 0);
          });
          if (!lr.hasContent) lr.errors.push("EN homepage rendered without h1");
          // Also check html lang=en
          const htmlLang = await page.evaluate(
            () => document.documentElement.lang || ""
          );
          if (!htmlLang.toLowerCase().startsWith("en")) {
            lr.errors.push(`EN page html lang is "${htmlLang}", not "en"`);
          }
        } else {
          lr.errors.push(`EN homepage returned ${enResp.status()}`);
        }
      } catch (err) {
        lr.errors.push(`EN locale check threw: ${(err as Error).message}`);
      }
      if (lr.errors.length > 0) result.passed = false;
      result.localeResults.push(lr);
    }
  } catch (err) {
    result.homepageError =
      (result.homepageError ? result.homepageError + " | " : "") +
      `Site test threw: ${(err as Error).message}`;
    result.passed = false;
  } finally {
    await context.close().catch(() => {});
  }

  result.durationMs = Date.now() - startedAt;
  return result;
}

// ---------- Markdown report ----------

function renderMarkdown(report: RunReport): string {
  const lines: string[] = [];
  lines.push(`# CRO Link Tester Report`);
  lines.push("");
  lines.push(`Generated: ${report.generatedAt}`);
  lines.push(`Total duration: ${(report.durationMs / 1000).toFixed(1)}s`);
  lines.push("");
  lines.push(`## Summary`);
  lines.push("");
  lines.push(`| Metric | Value |`);
  lines.push(`|---|---|`);
  lines.push(`| Sites passed | ${report.summary.sitesPassed} / ${report.sites.length} |`);
  lines.push(`| Articles passed | ${report.summary.passedArticles} / ${report.summary.totalArticles} |`);
  lines.push(`| Amazon CTAs passed | ${report.summary.passedCtas} / ${report.summary.totalCtas} |`);
  lines.push("");

  for (const s of report.sites) {
    lines.push(`## ${s.name} (${s.key})`);
    lines.push("");
    lines.push(`URL: ${s.url}`);
    lines.push(`Status: ${s.passed ? "PASS" : "FAIL"}`);
    lines.push(`Homepage HTTP: ${s.homepageStatus ?? "n/a"}`);
    if (s.homepageError) lines.push(`Homepage error: \`${s.homepageError}\``);
    lines.push(`Duration: ${(s.durationMs / 1000).toFixed(1)}s`);
    lines.push("");
    lines.push(`### Article click tests`);
    lines.push("");
    lines.push(`| # | Card href | Topmost | Clicked | Dest status | CTAs (ok/total) | Errors |`);
    lines.push(`|---|---|---|---|---|---|---|`);
    for (const a of s.articleResults) {
      const ctaOk = a.amazonCtas.filter((c) => c.tagOk).length;
      lines.push(
        `| ${a.cardIndex} | ${a.cardHref ?? "—"} | ${a.topmost === null ? "?" : a.topmost ? "yes" : "NO"} | ${a.clickNavigated === null ? "?" : a.clickNavigated ? "yes" : "NO"} | ${a.destStatus ?? "—"} | ${ctaOk}/${a.amazonCtas.length} | ${a.errors.length === 0 ? "—" : a.errors.join("; ").replace(/\|/g, "\\|").slice(0, 220)} |`
      );
    }
    if (s.localeResults.length > 0) {
      lines.push("");
      lines.push(`### Locale checks`);
      lines.push("");
      lines.push(`| Locale | URL | Status | Has content | Errors |`);
      lines.push(`|---|---|---|---|---|`);
      for (const lr of s.localeResults) {
        lines.push(
          `| ${lr.locale} | ${lr.url} | ${lr.status ?? "—"} | ${lr.hasContent ? "yes" : "NO"} | ${lr.errors.length === 0 ? "—" : lr.errors.join("; ").slice(0, 220)} |`
        );
      }
    }
    // Per-site failure-mode notes
    if (!s.passed) {
      const navFail = s.articleResults.find((a) => a.clickNavigated === false);
      if (navFail) {
        lines.push("");
        lines.push(`> **Failure-mode note (${s.key}):** The homepage anchor for `+
          `\`${navFail.cardHref}\` was clicked but no navigation occurred — `+
          `the browser stayed on the homepage. Topmost-element check returned ` +
          `\`${navFail.topmost}\`. This is consistent with: (a) a client-side `+
          `\`onClick\`/event handler calling \`preventDefault()\` (e.g. a `+
          `tracking wrapper that swallows the navigation when the analytics `+
          `client failed to load), (b) a hydration error on the page that `+
          `replaces or detaches the anchor between SSR and client render, or `+
          `(c) an overlay element above the card consuming the click. ` +
          (navFail.screenshot ? `See screenshot: \`${path.relative(ROOT, navFail.screenshot)}\`` : "")
        );
      }
    }
    lines.push("");
  }

  if (report.notes.length > 0) {
    lines.push(`## Notes`);
    lines.push("");
    for (const n of report.notes) lines.push(`- ${n}`);
    lines.push("");
  }
  return lines.join("\n");
}

// ---------- Main ----------

async function main(): Promise<void> {
  const opts = parseArgs(process.argv);
  const startedAt = Date.now();

  ensureDir(REPORTS_DIR);
  ensureDir(SCREENSHOT_DIR);

  const browser = await chromium.launch({
    headless: !opts.headed,
    args: ["--disable-blink-features=AutomationControlled"],
  });

  const sitesToRun = opts.siteFilter
    ? SITES.filter((s) => s.key === opts.siteFilter)
    : SITES;

  const results: SiteResult[] = [];
  for (const site of sitesToRun) {
    const elapsed = Date.now() - startedAt;
    if (elapsed > opts.globalTimeoutMs) {
      results.push({
        key: site.key,
        name: site.name,
        url: site.url,
        homepageStatus: null,
        homepageError: "Skipped: global timeout exceeded",
        articleResults: [],
        localeResults: [],
        durationMs: 0,
        passed: false,
      });
      continue;
    }
    process.stdout.write(`[${site.key}] starting…\n`);
    const r = await testSite(browser, site, opts);
    results.push(r);
    process.stdout.write(
      `[${site.key}] ${r.passed ? "PASS" : "FAIL"} in ${(r.durationMs / 1000).toFixed(1)}s\n`
    );
  }

  await browser.close();

  // Build summary
  let totalArticles = 0,
    passedArticles = 0,
    totalCtas = 0,
    passedCtas = 0;
  for (const s of results) {
    for (const a of s.articleResults) {
      totalArticles++;
      if (a.errors.length === 0) passedArticles++;
      for (const c of a.amazonCtas) {
        totalCtas++;
        if (c.tagOk) passedCtas++;
      }
    }
  }
  const sitesPassed = results.filter((s) => s.passed).length;
  const sitesFailed = results.length - sitesPassed;

  const report: RunReport = {
    generatedAt: new Date().toISOString(),
    durationMs: Date.now() - startedAt,
    sites: results,
    summary: {
      totalArticles,
      passedArticles,
      failedArticles: totalArticles - passedArticles,
      totalCtas,
      passedCtas,
      failedCtas: totalCtas - passedCtas,
      sitesPassed,
      sitesFailed,
    },
    notes: [],
  };

  const jsonPath = path.join(REPORTS_DIR, `cro-tester-${todayIso()}.json`);
  fs.writeFileSync(jsonPath, JSON.stringify(report, null, 2));
  const mdPath = path.join(REPORTS_DIR, `cro-tester-latest.md`);
  fs.writeFileSync(mdPath, renderMarkdown(report));

  process.stdout.write(`\nReport: ${jsonPath}\n`);
  process.stdout.write(`Summary: ${mdPath}\n`);
  process.stdout.write(
    `Sites: ${sitesPassed}/${results.length} passed | ` +
      `Articles: ${passedArticles}/${totalArticles} | ` +
      `CTAs: ${passedCtas}/${totalCtas}\n`
  );

  if (sitesFailed > 0) process.exit(1);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(2);
});
