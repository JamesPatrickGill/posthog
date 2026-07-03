import {
    DEFAULT_THRESHOLDS,
    ReputationEvaluationInput,
    ReputationMetrics,
    ReputationState,
    evaluateReputation,
} from './state-machine'

const NOW = new Date('2026-01-01T12:00:00Z')
const BEFORE_GRACE = new Date(NOW.getTime() - (DEFAULT_THRESHOLDS.warnGraceMinutes - 1) * 60 * 1000)
const AFTER_GRACE = new Date(NOW.getTime() - (DEFAULT_THRESHOLDS.warnGraceMinutes + 1) * 60 * 1000)

const input = (
    currentState: ReputationState,
    metrics: ReputationMetrics,
    warnedAt: Date | null = null
): ReputationEvaluationInput => ({ currentState, warnedAt, metrics, now: NOW })

describe('evaluateReputation', () => {
    it.each<
        [string, ReputationEvaluationInput, { nextState: ReputationState; transition: string; reason: string | null }]
    >([
        [
            'healthy stays healthy below all thresholds',
            input('healthy', { sent: 1000, bounced: 5, complained: 0 }),
            { nextState: 'healthy', transition: 'none', reason: null },
        ],
        [
            'healthy warns at the bounce warn threshold (inclusive)',
            input('healthy', { sent: 1000, bounced: 20, complained: 0 }),
            { nextState: 'warned', transition: 'warn', reason: 'bounce' },
        ],
        [
            'healthy warns at the complaint warn threshold (inclusive)',
            input('healthy', { sent: 1000, bounced: 0, complained: 1 }),
            { nextState: 'warned', transition: 'warn', reason: 'complaint' },
        ],
        [
            'healthy pauses immediately at the bounce hard threshold',
            input('healthy', { sent: 1000, bounced: 50, complained: 0 }),
            { nextState: 'paused', transition: 'pause', reason: 'bounce' },
        ],
        [
            'healthy pauses immediately at the complaint hard threshold',
            input('healthy', { sent: 1000, bounced: 0, complained: 5 }),
            { nextState: 'paused', transition: 'pause', reason: 'complaint' },
        ],
        [
            'complaint wins as reason when both signals breach hard',
            input('healthy', { sent: 1000, bounced: 50, complained: 5 }),
            { nextState: 'paused', transition: 'pause', reason: 'complaint' },
        ],
        [
            'below min sends nothing happens even at terrible rates',
            input('healthy', { sent: 50, bounced: 25, complained: 5 }),
            { nextState: 'healthy', transition: 'none', reason: null },
        ],
        [
            'below min sends an existing warning is held, not cleared',
            input('warned', { sent: 50, bounced: 0, complained: 0 }, AFTER_GRACE),
            { nextState: 'warned', transition: 'none', reason: null },
        ],
        [
            'warned holds within the grace period while still above soft',
            input('warned', { sent: 1000, bounced: 25, complained: 0 }, BEFORE_GRACE),
            { nextState: 'warned', transition: 'none', reason: null },
        ],
        [
            'warned escalates to pause after the grace period while still above soft',
            input('warned', { sent: 1000, bounced: 25, complained: 0 }, AFTER_GRACE),
            { nextState: 'paused', transition: 'pause', reason: 'bounce' },
        ],
        [
            'warned pauses on a hard breach even within the grace period',
            input('warned', { sent: 1000, bounced: 60, complained: 0 }, BEFORE_GRACE),
            { nextState: 'paused', transition: 'pause', reason: 'bounce' },
        ],
        [
            'warned clears back to healthy when rates recover',
            input('warned', { sent: 1000, bounced: 5, complained: 0 }, AFTER_GRACE),
            { nextState: 'healthy', transition: 'clear_warning', reason: null },
        ],
        [
            'warned with unknown warnedAt never escalates on soft breach',
            input('warned', { sent: 1000, bounced: 25, complained: 0 }, null),
            { nextState: 'warned', transition: 'none', reason: null },
        ],
        [
            'paused is terminal even when rates recover fully',
            input('paused', { sent: 1000, bounced: 0, complained: 0 }),
            { nextState: 'paused', transition: 'none', reason: null },
        ],
        [
            'zero sends produces zero rates and no transition',
            input('healthy', { sent: 0, bounced: 0, complained: 0 }),
            { nextState: 'healthy', transition: 'none', reason: null },
        ],
    ])('%s', (_name, evaluationInput, expected) => {
        const decision = evaluateReputation(evaluationInput)
        expect({
            nextState: decision.nextState,
            transition: decision.transition,
            reason: decision.reason,
        }).toEqual(expected)
    })

    it('reports the computed rates on the decision', () => {
        const decision = evaluateReputation(input('healthy', { sent: 200, bounced: 10, complained: 1 }))
        expect(decision.bounceRate).toBeCloseTo(0.05)
        expect(decision.complaintRate).toBeCloseTo(0.005)
    })
})
