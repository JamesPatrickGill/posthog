import { act, cleanup, renderHook } from '@testing-library/react'

import { initKeaTests } from '~/test/init'

import { foregroundStreamLogic } from '../logics/foregroundStreamLogic'
import { toolStreamEventsLogic } from '../logics/toolStreamEventsLogic'
import type { ToolStreamEvent } from '../types/streamTypes'
import { useMcpToolApplyBack } from './useMcpToolApplyBack'

function completed(command: string, toolCallId: string): ToolStreamEvent {
    return {
        streamKey: 'run-1',
        toolCallId,
        toolName: 'create_insight',
        rawToolName: 'exec',
        phase: 'completed',
        invocation: {
            rawServerName: 'posthog',
            rawToolName: 'exec',
            input: { command },
        } as unknown as ToolStreamEvent['invocation'],
        source: 'live',
    }
}

describe('useMcpToolApplyBack', () => {
    beforeEach(() => {
        initKeaTests(false)
    })

    afterEach(() => {
        cleanup()
    })

    // The core terminal-mode contract: it buffers the last matching completion, fires onApply exactly
    // once when the foreground run terminates (with the parsed inner args), and stays silent for a run
    // that isn't the foreground stream. Regressions here (firing early, firing per-completion, applying
    // the wrong/first completion, or reacting to a background run) all slip past the bus-level tests.
    it('applies the last matching completion once at terminal, only for the foreground run', () => {
        const onApply = jest.fn()
        renderHook(() => useMcpToolApplyBack({ tools: ['create_insight'], onApply }))

        // Not the foreground stream yet → completions are withheld and the terminal flushes nothing.
        act(() => {
            toolStreamEventsLogic.actions.emitToolEvent(completed('call create_insight {"name":"early"}', 'early'))
            toolStreamEventsLogic.actions.emitRunLifecycleEvent({ streamKey: 'run-1', status: 'completed' })
        })
        expect(onApply).not.toHaveBeenCalled()

        // Register run-1 as the foreground stream (its own act so the reset effect flushes first).
        act(() => {
            foregroundStreamLogic.actions.setForegroundStream('run-1')
        })

        // Two matching completions arrive; the later one supersedes the earlier.
        act(() => {
            toolStreamEventsLogic.actions.emitToolEvent(completed('call create_insight {"name":"first"}', 'a'))
            toolStreamEventsLogic.actions.emitToolEvent(completed('call create_insight {"name":"second"}', 'b'))
        })
        // Nothing applies until the run reaches a terminal status.
        expect(onApply).not.toHaveBeenCalled()

        act(() => {
            toolStreamEventsLogic.actions.emitRunLifecycleEvent({ streamKey: 'run-1', status: 'completed' })
        })
        expect(onApply).toHaveBeenCalledTimes(1)
        const [event, context] = onApply.mock.calls[0]
        expect(event.toolCallId).toBe('b')
        expect(context.innerInput).toEqual({ name: 'second' })
    })
})
