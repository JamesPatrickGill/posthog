import type { z } from 'zod'

import { SuggestAiTraceParserSchema } from '@/schema/tool-inputs'
import type { Context, ToolBase } from '@/tools/types'

interface SuggestAiTraceParserResult {
    status: 'sent_to_open_page'
    note: string
    recipe: Record<string, unknown>
}

/**
 * suggest-ai-trace-parser is a schema-echo reserved for the PostHog AI conversation sandbox (gated by
 * the `posthog_ai_frontend:read` scope). The custom parser recipe DSL compiler only exists in the
 * browser, so nothing is compiled or persisted server-side: the tool echoes the proposal and the
 * PostHog AI browser side panel is the real consumer. It validates the recipe against the open trace
 * event and saves it on success; on a compile failure it sends a follow-up message with the compiler
 * error so the agent can fix the YAML and call again.
 */
export const suggestAiTraceParser = (): ToolBase<typeof SuggestAiTraceParserSchema, SuggestAiTraceParserResult> => ({
    name: 'suggest-ai-trace-parser',
    schema: SuggestAiTraceParserSchema,
    handler: (
        _context: Context,
        params: z.infer<typeof SuggestAiTraceParserSchema>
    ): Promise<SuggestAiTraceParserResult> =>
        Promise.resolve({
            status: 'sent_to_open_page',
            note:
                "This recipe has been sent to the user's browser, which validates it against the open trace event " +
                'and saves it on success. If compilation fails, a follow-up message carrying the compiler error ' +
                'will arrive: fix the YAML and call this tool again. If no follow-up arrives, the recipe compiled ' +
                'and was saved, so do not call the tool again. The proposal is echoed below.',
            recipe: params as Record<string, unknown>,
        }),
})
