import {
    activeJestEntries,
    findMatch,
    parseQuarantine,
    productPathPrefix,
    QuarantineEntry,
    repoRelativePath,
    selectorMatches,
} from '../../jest.quarantine'

const TODAY = '2026-06-10'

function entry(overrides: Partial<QuarantineEntry> = {}): QuarantineEntry {
    return {
        id: 'frontend/src/x.test.ts',
        mode: 'run',
        reason: 'flaky',
        owner: '@web',
        issue: '',
        expires: '2026-06-20',
        ...overrides,
    }
}

describe('jest.quarantine', () => {
    describe('selectorMatches', () => {
        // The JS reimplements core.py's grammar, so it can drift independently — these lock it.
        test.each<[string, string, string, boolean]>([
            ['file covers a test in it', 'frontend/src/x.test.ts', 'frontend/src/x.test.ts::A loads', true],
            ['directory prefix', 'frontend/src', 'frontend/src/x.test.ts::A loads', true],
            ['directory trailing slash', 'frontend/src/', 'frontend/src/x.test.ts::A loads', true],
            ['describe prefix via space', 'frontend/src/x.test.ts::A', 'frontend/src/x.test.ts::A loads data', true],
            ['exact full name', 'frontend/src/x.test.ts::A loads', 'frontend/src/x.test.ts::A loads', true],
            ['partial describe word', 'frontend/src/x.test.ts::A lo', 'frontend/src/x.test.ts::A loads', false],
            ['product selector', 'product:batch-exports', 'products/batch_exports/frontend/x.test.ts::A', true],
            ['product mismatch', 'product:batch-exports', 'frontend/src/x.test.ts::A', false],
            ['unrelated sibling file', 'frontend/src/x.test.ts', 'frontend/src/xy.test.ts::A', false],
        ])('%s', (_label, selector, testId, expected) => {
            expect(selectorMatches(selector, testId)).toBe(expected)
        })
    })

    describe('activeJestEntries', () => {
        test('keeps only unexpired jest entries', () => {
            const raw = [
                { id: 'a', runner: 'jest', expires: '2026-06-20' },
                { id: 'b', runner: 'jest', expires: '2026-06-01' }, // expired
                { id: 'c', expires: '2026-06-20' }, // defaults to pytest
                { id: 'd', runner: 'pytest', expires: '2026-06-20' },
                { id: 'e', runner: 'jest', mode: 'pause', expires: '2026-06-20' }, // invalid mode dropped
                { id: 'f', runner: 'jest', mode: 'skip', expires: '2026-06-20' },
            ]
            expect(activeJestEntries(raw, TODAY).map((e) => e.id)).toEqual(['a', 'f'])
        })
    })

    describe('findMatch', () => {
        test('the most specific selector wins so a narrow skip overrides a broad run', () => {
            const entries = [
                entry({ id: 'frontend/src/x.test.ts', mode: 'run' }),
                entry({ id: 'frontend/src/x.test.ts::A loads', mode: 'skip' }),
            ]
            expect(findMatch(entries, 'frontend/src/x.test.ts::A loads')?.mode).toBe('skip')
            expect(findMatch(entries, 'frontend/src/x.test.ts::B other')?.mode).toBe('run')
        })

        test('returns null when nothing matches', () => {
            expect(findMatch([entry()], 'frontend/src/other.test.ts::A')).toBeNull()
        })
    })

    describe('parseQuarantine', () => {
        test('reads v1 entries and drops ones without an id', () => {
            const text = JSON.stringify({
                version: 1,
                entries: [{ id: 'frontend/src/x.test.ts', runner: 'jest' }, { runner: 'jest' }],
            })
            expect(parseQuarantine(text).map((e) => e.id)).toEqual(['frontend/src/x.test.ts'])
        })

        test.each<[string, string]>([
            ['unsupported version', JSON.stringify({ version: 2, entries: [{ id: 'a' }] })],
            ['entries not a list', JSON.stringify({ version: 1, entries: {} })],
        ])('returns [] for %s', (_label, text) => {
            expect(parseQuarantine(text)).toEqual([])
        })
    })

    describe('repoRelativePath', () => {
        test('makes an absolute test path repo-root-relative with forward slashes', () => {
            expect(repoRelativePath('/repo/frontend/src/x.test.ts', '/repo')).toBe('frontend/src/x.test.ts')
        })
    })

    describe('productPathPrefix', () => {
        test('maps a dashed product name to the underscored directory', () => {
            expect(productPathPrefix('product:batch-exports')).toBe('products/batch_exports/')
        })
    })
})
