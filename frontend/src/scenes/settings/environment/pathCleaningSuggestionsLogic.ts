import { actions, afterMount, connect, kea, listeners, path, reducers, selectors } from 'kea'
import { loaders } from 'kea-loaders'

import { lemonToast } from '@posthog/lemon-ui'

import api from 'lib/api'
import { teamLogic } from 'scenes/teamLogic'

import { webAnalyticsPathCleaningSuggestionsApply } from 'products/web_analytics/frontend/generated/api'
import type { PathCleaningSuggestionIssueApi } from 'products/web_analytics/frontend/generated/api.schemas'

import type { pathCleaningSuggestionsLogicType } from './pathCleaningSuggestionsLogicType'

export const PATH_CLEANING_SUGGESTIONS_KIND = 'path_cleaning_suggestions'

interface HealthIssueRecord {
    id: string
    created_at: string
    payload: Record<string, any>
}

const toSuggestion = (issue: HealthIssueRecord): PathCleaningSuggestionIssueApi => ({
    id: issue.id,
    created_at: issue.created_at,
    rules: issue.payload?.rules ?? [],
    model: issue.payload?.model ?? '',
    sampled_path_count: issue.payload?.sampled_path_count ?? 0,
    distinct_path_count: issue.payload?.distinct_path_count ?? 0,
})

export const pathCleaningSuggestionsLogic = kea<pathCleaningSuggestionsLogicType>([
    path(['scenes', 'settings', 'environment', 'pathCleaningSuggestionsLogic']),
    connect(() => ({
        values: [teamLogic, ['currentTeamId']],
        actions: [teamLogic, ['loadCurrentTeam']],
    })),
    actions({
        applySuggestion: (id: string) => ({ id }),
        dismissSuggestion: (id: string) => ({ id }),
        unhandleSuggestion: (id: string) => ({ id }),
    }),
    loaders(({ values }) => ({
        suggestions: [
            [] as PathCleaningSuggestionIssueApi[],
            {
                loadSuggestions: async () => {
                    if (!values.currentTeamId) {
                        return []
                    }
                    // Suggestions are stored as health issues; the newest active, non-dismissed one is
                    // the actionable suggestion.
                    const response = await api.get<{ results: HealthIssueRecord[] }>(
                        `api/projects/${values.currentTeamId}/health_issues/?kind=${PATH_CLEANING_SUGGESTIONS_KIND}&status=active&dismissed=false`
                    )
                    return response.results.map(toSuggestion)
                },
            },
        ],
    })),
    reducers({
        // Optimistically hide a suggestion the moment it's applied or dismissed.
        handledIds: [
            [] as string[],
            {
                applySuggestion: (state, { id }) => [...state, id],
                dismissSuggestion: (state, { id }) => [...state, id],
                unhandleSuggestion: (state, { id }) => state.filter((handledId) => handledId !== id),
            },
        ],
    }),
    selectors({
        latestSuggestion: [
            (s) => [s.suggestions, s.handledIds],
            (suggestions, handledIds): PathCleaningSuggestionIssueApi | null =>
                suggestions.find((suggestion) => !handledIds.includes(suggestion.id)) ?? null,
        ],
    }),
    listeners(({ values, actions }) => ({
        applySuggestion: async ({ id }) => {
            if (!values.currentTeamId) {
                return
            }
            try {
                const result = await webAnalyticsPathCleaningSuggestionsApply(String(values.currentTeamId), id)
                lemonToast.success(`Applied ${result.applied} path cleaning rule${result.applied === 1 ? '' : 's'}`)
                // Refresh the team so the rules table reflects the merged path_cleaning_filters.
                actions.loadCurrentTeam()
            } catch {
                // Roll back the optimistic hide and tell the user, so nothing is silently lost.
                actions.unhandleSuggestion(id)
                lemonToast.error('Could not apply the path cleaning suggestions. Please try again.')
            }
        },
        dismissSuggestion: async ({ id }) => {
            if (!values.currentTeamId) {
                return
            }
            try {
                await api.update(`api/projects/${values.currentTeamId}/health_issues/${id}/`, { dismissed: true })
            } catch {
                actions.unhandleSuggestion(id)
                lemonToast.error('Could not dismiss the suggestion. Please try again.')
            }
        },
    })),
    afterMount(({ actions }) => {
        actions.loadSuggestions()
    }),
])
