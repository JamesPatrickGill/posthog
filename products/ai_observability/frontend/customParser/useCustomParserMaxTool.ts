import { useActions, useValues } from 'kea'
import { useCallback, useMemo } from 'react'

import { lemonToast } from '@posthog/lemon-ui'

import { useFeatureFlag } from 'lib/hooks/useFeatureFlag'
import { MAX_SIDE_PANEL_ID } from 'scenes/max/components/PhaiSidePanelChat'
import { useMaxTool } from 'scenes/max/useMaxTool'
import { teamLogic } from 'scenes/teamLogic'

import { composerSeedLogic, useAttachedContext, useMcpToolApplyBack } from 'products/posthog_ai/frontend/api/logics'

import { llmAnalyticsParserRecipesCreate } from '../generated/api'
import { parserRecipesLogic } from '../settings/parserRecipesLogic'
import { formatParserRecipeCorrection, type ParserRecipeVerdict } from './parserRecipeCorrection'
import { sampleForContext } from './sampleForContext'
import { handleCreateParserRecipeCall } from './validateRecipe'

const MAX_EXISTING_RECIPES_CONTEXT_LENGTH = 4000

export interface CustomParserMaxToolOptions {
    eventId: string
    input: unknown
    output: unknown
    tools?: unknown
    inputRecognized: boolean
    outputRecognized: boolean
    isLoading: boolean
    isGeneration: boolean
}

/**
 * Registers the create_ai_trace_parser Max tool for an event that fell back to raw JSON.
 * The clientExecution handler validates the agent's YAML against this exact event, saves it
 * on success, and resumes the conversation with the verdict. Returns openMax (null while inactive).
 */
export function useCustomParserMaxTool({
    eventId,
    input,
    output,
    tools,
    inputRecognized,
    outputRecognized,
    isLoading,
    isGeneration,
}: CustomParserMaxToolOptions): (() => void) | null {
    const customParsersEnabled = useFeatureFlag('LLM_ANALYTICS_CUSTOM_PARSERS')
    const { currentTeamId } = useValues(teamLogic)
    const { storedForMerge, customItems } = useValues(parserRecipesLogic)
    const { loadRecipes } = useActions(parserRecipesLogic)

    const unrecognized =
        !inputRecognized && !outputRecognized
            ? 'both'
            : !inputRecognized
              ? 'input'
              : !outputRecognized
                ? 'output'
                : null
    const active = customParsersEnabled && unrecognized !== null && !isLoading

    const context = useMemo(() => {
        if (!active) {
            return undefined
        }
        // Truncate on recipe boundaries — a mid-recipe cut would put broken YAML in the prompt
        const existingBlocks: string[] = []
        let remainingBudget = MAX_EXISTING_RECIPES_CONTEXT_LENGTH
        let omittedCount = 0
        for (const item of customItems) {
            const block = `--- ${item.name} ---\n${item.source}`
            if (block.length <= remainingBudget) {
                existingBlocks.push(block)
                remainingBudget -= block.length
            } else {
                omittedCount += 1
            }
        }
        if (omittedCount > 0) {
            existingBlocks.push(`… (${omittedCount} more recipes omitted for length)`)
        }
        return {
            event_uuid: eventId,
            event_type: isGeneration ? 'generation' : 'span',
            unrecognized,
            sample_input: sampleForContext(input),
            sample_output: sampleForContext(output),
            existing_recipes: existingBlocks.length > 0 ? existingBlocks.join('\n') : '(none)',
        }
    }, [active, eventId, isGeneration, unrecognized, input, output, customItems])

    const validateAndSave = useCallback(
        async (args: Record<string, any>): Promise<Record<string, unknown>> =>
            handleCreateParserRecipeCall(args, {
                eventId,
                existingRecipes: storedForMerge,
                sample: { input, output, tools, inputRecognized, outputRecognized },
                saveRecipe: async (name, source) => {
                    const created = await llmAnalyticsParserRecipesCreate(String(currentTeamId), { name, source })
                    // Reloading applies the recipe to the live normalizer, re-rendering the open trace
                    loadRecipes()
                    lemonToast.success(`Custom parser "${name}" saved`)
                    return created.id
                },
            }),
        [eventId, storedForMerge, input, output, tools, inputRecognized, outputRecognized, currentTeamId, loadRecipes]
    )

    useAttachedContext(
        active
            ? [
                  { type: 'llm_trace_event', key: eventId },
                  {
                      type: 'ai_trace_parser_context',
                      value: JSON.stringify({
                          event_type: isGeneration ? 'generation' : 'span',
                          unrecognized,
                          sample_input: sampleForContext(input),
                          sample_output: sampleForContext(output),
                      }),
                      label: 'Trace event sample',
                  },
              ]
            : null
    )

    // Sandbox-runtime counterpart of the legacy `create_ai_trace_parser` MaxTool below: the agent proposes a
    // recipe via the `suggest-ai-trace-parser` MCP echo tool, and the browser validates + saves it here
    // through the same `validateAndSave` path. On success the existing success toast is the confirmation and
    // the agent hears nothing; on failure we surface the compiler error so the user can send a corrective
    // follow-up and the agent retries. There is no single follow-up dispatch that reaches both the task-based
    // side panel and the legacy-chrome sandbox conversation from here, so this is the graceful fallback: a
    // toast plus a best-effort composer seed (no auto-submit) into the task-based side panel when it is open.
    useMcpToolApplyBack({
        tools: ['suggest-ai-trace-parser'],
        applyOn: 'completed',
        onApply: (_event, { innerInput }): void => {
            if (!active || !innerInput) {
                return
            }
            void validateAndSave(innerInput)
                .then((verdict) => {
                    const corrective = formatParserRecipeCorrection(verdict as ParserRecipeVerdict)
                    if (!corrective) {
                        return
                    }
                    // Only seeds when the new task-based side panel is mounted, so a stale corrective never
                    // lingers to prefill the composer on the legacy-chrome surface (or a later session). The
                    // corrective text is written for the agent; the toast speaks to the user, so it points at
                    // the prepared correction when one was seeded and carries the detail only when it wasn't.
                    const panelSeed = composerSeedLogic.findMounted({ panelId: MAX_SIDE_PANEL_ID })
                    if (panelSeed) {
                        panelSeed.actions.setSeed({ prompt: corrective, autoSubmit: false })
                        lemonToast.error(
                            'The generated custom parser did not apply. A correction is ready in the PostHog AI composer, send it to let the agent retry.'
                        )
                    } else {
                        lemonToast.error(corrective)
                    }
                })
                .catch(() => {
                    // `handleCreateParserRecipeCall` folds its own errors into a verdict, so this only fires on
                    // an unexpected throw. Surface it rather than dropping the failure silently.
                    lemonToast.error('Could not validate the parser recipe. Please try again.')
                })
        },
    })

    const { openMax } = useMaxTool({
        identifier: 'create_ai_trace_parser',
        active,
        context,
        clientExecution: validateAndSave,
        initialMaxPrompt: '!Set up a custom parser so this event displays properly',
    })

    return openMax
}
