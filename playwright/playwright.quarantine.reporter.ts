/**
 * Playwright reporter half of the test quarantine — enforces `mode: "run"`.
 *
 * Playwright has no native non-strict xfail, so a `mode: "run"` quarantined
 * test still executes (keeping its timing/outcome in the html/junit/json
 * reports) and this reporter tolerates a genuine failure: if the ONLY
 * unexpected failures in the run are `mode: "run"` quarantined tests, it
 * overrides the final run status to `passed` so they don't block CI. A single
 * non-quarantined failure keeps the run red.
 *
 * `mode: "skip"` is handled earlier by the auto fixture in
 * `utils/playwright-test-core.ts`; skipped tests never reach here as failures.
 *
 * Fail-open: if the quarantine file is missing or unreadable there are no
 * active entries and this reporter is a no-op.
 *
 * Schema contract + matching: `playwright.quarantine.ts` (and core.py behind it).
 */

import type { FullResult, Reporter, Suite, TestCase } from '@playwright/test/reporter'

import { QuarantineDecision, QuarantineEntry, decideForTest, loadActiveEntries } from './playwright.quarantine'

/** Describe titles from the root down, then the test title — the playwright name parts. */
function nameParts(test: TestCase): string[] {
    const describes: string[] = []
    for (let suite: Suite | undefined = test.parent; suite && suite.type === 'describe'; suite = suite.parent) {
        describes.unshift(suite.title)
    }
    return [...describes, test.title]
}

export default class QuarantineReporter implements Reporter {
    private readonly entries: QuarantineEntry[]
    private rootSuite: Suite | undefined

    constructor() {
        this.entries = loadActiveEntries()
    }

    // We only emit occasional warnings, so let Playwright keep its default terminal reporter.
    printsToStdio(): boolean {
        return false
    }

    onBegin(_config: unknown, suite: Suite): void {
        this.rootSuite = suite
    }

    async onEnd(result: FullResult): Promise<{ status?: FullResult['status'] } | void> {
        if (this.entries.length === 0 || !this.rootSuite) {
            return
        }
        // Only 'failed'/'timedout' runs are candidates; leave interrupted runs alone.
        if (result.status !== 'failed' && result.status !== 'timedout') {
            return
        }

        const tolerated: QuarantineDecision[] = []
        let realFailures = 0
        for (const test of this.rootSuite.allTests()) {
            if (test.outcome() !== 'unexpected') {
                continue // Passed, skipped, or recovered-on-retry (flaky) — not a blocking failure.
            }
            const decision = decideForTest(this.entries, test.location.file, nameParts(test))
            if (decision?.mode === 'run') {
                tolerated.push(decision)
            } else {
                realFailures += 1
            }
        }

        if (tolerated.length === 0) {
            return // Nothing quarantined failed; the run stays red on its own merits.
        }
        for (const decision of tolerated) {
            // eslint-disable-next-line no-console
            console.warn(`[quarantine] tolerated failure in ${decision.label}`)
        }
        if (realFailures > 0) {
            return // Real failures remain — keep the run red.
        }
        // eslint-disable-next-line no-console
        console.warn(
            `[quarantine] overriding run status to passed — the only ${tolerated.length} unexpected ` +
                `failure(s) are quarantined (mode: run)`
        )
        return { status: 'passed' }
    }
}
