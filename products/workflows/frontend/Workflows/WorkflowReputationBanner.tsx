import { useActions, useValues } from 'kea'

import { LemonBanner } from 'lib/lemon-ui/LemonBanner'

import type { EmailReputationStateApi } from 'products/workflows/frontend/generated/api.schemas'

import { workflowLogic } from './workflowLogic'

export function describeReputation(reputation: EmailReputationStateApi): string {
    const signal = reputation.pause_reason === 'complaint' ? 'spam complaint rate' : 'bounce rate'
    const rate =
        reputation.pause_reason === 'complaint'
            ? (reputation.complaint_rate * 100).toFixed(2)
            : (reputation.bounce_rate * 100).toFixed(1)
    return `${signal} of ${rate}% over the last ${reputation.emails_sent} emails`
}

export function WorkflowReputationBanner(): JSX.Element | null {
    const { originalWorkflow, reputationReenabling } = useValues(workflowLogic)
    const { reenableReputation } = useActions(workflowLogic)

    const reputation = originalWorkflow?.reputation
    if (!reputation || reputation.state === 'healthy') {
        return null
    }

    if (reputation.state === 'paused') {
        return (
            <LemonBanner
                type="error"
                action={{
                    children: 'Re-enable sending',
                    loading: reputationReenabling,
                    disabledReason: reputationReenabling ? 'Re-enabling…' : undefined,
                    onClick: () => reenableReputation(),
                }}
            >
                <b>Email sending is paused for this workflow.</b> It was automatically paused because of a{' '}
                {describeReputation(reputation)}. Clean up your recipient list before re-enabling — continued high rates
                hurt deliverability for your whole project.
            </LemonBanner>
        )
    }

    const warnSignal = reputation.complaint_rate >= reputation.bounce_rate ? 'spam complaint rate' : 'bounce rate'
    const warnRate =
        warnSignal === 'spam complaint rate'
            ? (reputation.complaint_rate * 100).toFixed(2)
            : (reputation.bounce_rate * 100).toFixed(1)
    return (
        <LemonBanner type="warning">
            <b>This workflow's email reputation needs attention.</b> Its {warnSignal} is {warnRate}% over the last{' '}
            {reputation.emails_sent} emails. If it doesn't improve, sending will be paused automatically.
        </LemonBanner>
    )
}
