import { IconCollapse45, IconCopy, IconExpand45, IconListCheck } from '@posthog/icons'
import { LemonButton } from '@posthog/lemon-ui'

import { copyToClipboard } from 'lib/utils/copyToClipboard'
import { cn } from 'lib/utils/css-classes'

import { MarkdownMessage } from '../messages/MarkdownMessage'
import { FilePath } from './tool/FilePath'

export interface PlanPayload {
    plan?: string
    planFilePath?: string
}

/**
 * Pull the plan markdown + plan file path out of an `ExitPlanMode` tool input. The agent-server sends
 * `rawInput: { plan, planFilePath, toolName }` — `plan` matching `/code`'s `toolCall.rawInput.plan`.
 */
export function getPlanPayload(input: Record<string, unknown> | undefined): PlanPayload {
    const plan = input?.plan
    const planFilePath = input?.planFilePath
    return {
        plan: typeof plan === 'string' && plan.trim() ? plan : undefined,
        planFilePath: typeof planFilePath === 'string' && planFilePath.trim() ? planFilePath : undefined,
    }
}

export interface PlanPreviewProps {
    plan: string
    /** Stable id for the memoized markdown blocks. */
    id: string
    /** The markdown file the agent wrote the plan to, rendered as a file chip in the header. */
    planFilePath?: string
    /** Controlled open state — the owner (the stream logic) holds it so the layout can react too. */
    expanded: boolean
    onExpandedChange: (expanded: boolean) => void
}

/**
 * The agent's plan (the `ExitPlanMode` payload), presented as a document sheet — `/code`'s `PlanContent`
 * translated to LemonUI: a header bar with the plan title, the plan file chip, and copy/close controls,
 * over a scrollable markdown body that grows to fill the available height (the open plan approval expands
 * its container over the whole thread area). Closed, it collapses to just the header bar so the approval
 * actions stay at hand while the thread is visible again.
 */
export function PlanPreview({ plan, id, planFilePath, expanded, onExpandedChange }: PlanPreviewProps): JSX.Element {
    return (
        <div
            className={cn(
                'flex min-h-0 flex-col overflow-hidden rounded border bg-surface-primary',
                expanded && 'flex-1'
            )}
        >
            <div
                className={cn(
                    'flex items-center justify-between gap-2 bg-surface-secondary px-2 py-1',
                    expanded && 'border-b'
                )}
            >
                <div className="flex min-w-0 items-center gap-2">
                    <IconListCheck className="size-4 shrink-0 text-muted" />
                    <span className="text-sm font-medium">Plan</span>
                    {planFilePath && <FilePath path={planFilePath} />}
                </div>
                <div className="flex shrink-0 items-center gap-1">
                    <LemonButton
                        size="xsmall"
                        icon={<IconCopy />}
                        tooltip="Copy plan to clipboard"
                        onClick={() => void copyToClipboard(plan, 'plan')}
                    />
                    <LemonButton
                        size="xsmall"
                        icon={expanded ? <IconCollapse45 /> : <IconExpand45 />}
                        onClick={() => onExpandedChange(!expanded)}
                        aria-expanded={expanded}
                    >
                        {expanded ? 'Close' : 'Open plan'}
                    </LemonButton>
                </div>
            </div>
            {expanded && (
                <div className="min-h-0 flex-1 overflow-y-auto p-4">
                    <div className="mx-auto w-full max-w-160">
                        <MarkdownMessage content={plan} id={id} />
                    </div>
                </div>
            )}
        </div>
    )
}
