import { useActions, useValues } from 'kea'

import { IconArrowRight } from '@posthog/icons'
import { LemonBanner } from '@posthog/lemon-ui'

import { RestrictionScope, useRestrictedArea } from 'lib/components/RestrictedArea'
import { TeamMembershipLevel } from 'lib/constants'

import { pathCleaningSuggestionsLogic } from './pathCleaningSuggestionsLogic'

export function PathCleaningSuggestionsBanner(): JSX.Element | null {
    const { latestSuggestion, suggestionsLoading } = useValues(pathCleaningSuggestionsLogic)
    const { applySuggestion, dismissSuggestion } = useActions(pathCleaningSuggestionsLogic)
    // Applying writes path_cleaning_filters, an admin-gated team field — mirror the backend gate.
    const restrictedReason = useRestrictedArea({
        scope: RestrictionScope.Project,
        minimumAccessLevel: TeamMembershipLevel.Admin,
    })

    if (suggestionsLoading || !latestSuggestion || latestSuggestion.rules.length === 0) {
        return null
    }

    const ruleCount = latestSuggestion.rules.length

    return (
        <LemonBanner
            type="info"
            className="mb-4"
            action={{
                children: 'Apply all',
                onClick: () => applySuggestion(latestSuggestion.id),
                disabledReason: restrictedReason,
            }}
            onClose={() => dismissSuggestion(latestSuggestion.id)}
        >
            <div className="flex flex-col gap-2">
                <span>
                    We analyzed your traffic and suggest <strong>{ruleCount}</strong> path cleaning{' '}
                    {ruleCount === 1 ? 'rule' : 'rules'} to group similar pages. Review and apply them:
                </span>
                <div className="flex flex-col gap-1">
                    {latestSuggestion.rules.map((rule) => (
                        <div key={rule.order} className="flex flex-wrap items-center gap-2 font-mono text-xs">
                            <code>{rule.regex}</code>
                            <IconArrowRight />
                            <code>{rule.alias}</code>
                            <span className="text-secondary">matches {rule.match_count} paths</span>
                        </div>
                    ))}
                </div>
            </div>
        </LemonBanner>
    )
}
