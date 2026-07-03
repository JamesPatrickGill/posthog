import { FixtureHogFlowBuilder } from '~/cdp/_tests/builders/hogflow.builder'
import { insertHogFlow } from '~/cdp/_tests/fixtures-hogflows'
import { HogFlow } from '~/cdp/schema/hogflow'
import { closeHub, createHub } from '~/common/utils/db/hub'
import { PostgresUse } from '~/common/utils/db/postgres'
import { PubSub } from '~/common/utils/pubsub'
import { createTeam, getTeam, resetTestDatabase } from '~/tests/helpers/sql'
import { Hub } from '~/types'

import { EmailReputationService } from './email-reputation.service'
import { DEFAULT_THRESHOLDS } from './state-machine'

describe('EmailReputationService', () => {
    jest.setTimeout(5000)

    let hub: Hub
    let service: EmailReputationService
    let mockPubSub: { publish: jest.Mock }
    let teamId: number

    const insertEmailFlow = async (status: HogFlow['status'] = 'active'): Promise<HogFlow> => {
        return await insertHogFlow(
            hub.postgres,
            new FixtureHogFlowBuilder()
                .withTeamId(teamId)
                .withStatus(status)
                .withWorkflow({
                    actions: {
                        trigger: { type: 'trigger', config: { type: 'event', filters: {} } },
                        send_email: { type: 'function_email', config: { template_id: 'template-email' } } as any,
                        exit: { type: 'exit', config: {} },
                    },
                    edges: [
                        { from: 'trigger', to: 'send_email', type: 'continue' },
                        { from: 'send_email', to: 'exit', type: 'continue' },
                    ],
                })
                .build()
        )
    }

    const getFlowStatus = async (flowId: string): Promise<string> => {
        const result = await hub.postgres.query<{ status: string }>(
            PostgresUse.COMMON_READ,
            `SELECT status FROM posthog_hogflow WHERE id = $1`,
            [flowId],
            'testGetFlowStatus'
        )
        return result.rows[0].status
    }

    const getReputationRows = async (): Promise<any[]> => {
        const result = await hub.postgres.query(
            PostgresUse.COMMON_READ,
            `SELECT * FROM posthog_emailreputationstate WHERE team_id = $1 ORDER BY scope, hog_flow_id`,
            [teamId],
            'testGetReputationRows'
        )
        return result.rows
    }

    beforeEach(async () => {
        hub = await createHub()
        await resetTestDatabase()
        const team = await getTeam(hub.postgres, 2)
        teamId = await createTeam(hub.postgres, team!.organization_id)
        mockPubSub = { publish: jest.fn().mockResolvedValue(undefined) }
        service = new EmailReputationService(
            {} as any, // ClickHouse client unused: evaluateAndEnforce receives metric rows directly
            hub.postgres,
            mockPubSub as unknown as PubSub,
            {} as any, // InternalFetchService unused: notifyTransitions not exercised here
            { windowHours: 24, thresholds: DEFAULT_THRESHOLDS }
        )
    })

    afterEach(async () => {
        await closeHub(hub)
    })

    it('pauses an active workflow on a hard bounce breach and records the transition', async () => {
        const flow = await insertEmailFlow('active')

        const summary = await service.evaluateAndEnforce([
            { teamId, appSourceId: flow.id, sent: 1000, bounced: 60, complained: 0 },
        ])

        expect(await getFlowStatus(flow.id)).toEqual('paused')
        expect(mockPubSub.publish).toHaveBeenCalledWith(
            'reload-hog-flows',
            JSON.stringify({ teamId, hogFlowIds: [flow.id] })
        )

        const rows = await getReputationRows()
        const workflowRow = rows.find((r) => r.hog_flow_id === flow.id)
        expect(workflowRow).toMatchObject({
            state: 'paused',
            scope: 'workflow',
            pause_reason: 'bounce',
            previous_flow_status: 'active',
            emails_sent: '1000',
        })
        expect(workflowRow.bounce_rate).toBeCloseTo(0.06)

        const workflowTransition = summary.transitions.find((t) => t.scope === 'workflow')
        expect(workflowTransition).toMatchObject({
            team_id: teamId,
            new_state: 'paused',
            reason: 'bounce',
            hog_flow_id: flow.id,
            threshold: DEFAULT_THRESHOLDS.bouncePause,
        })
    })

    it('warns without pausing on a soft breach, and a repeat evaluation adds no duplicate transition', async () => {
        const flow = await insertEmailFlow('active')
        const metrics = [{ teamId, appSourceId: flow.id, sent: 1000, bounced: 30, complained: 0 }]

        const first = await service.evaluateAndEnforce(metrics)
        expect(await getFlowStatus(flow.id)).toEqual('active')
        expect(first.transitions.filter((t) => t.scope === 'workflow')).toHaveLength(1)
        expect(first.transitions[0].new_state).toEqual('warned')

        // Within the grace period the warning holds: no new transition, no notification spam
        const second = await service.evaluateAndEnforce(metrics)
        expect(second.transitions).toHaveLength(0)
        expect(await getFlowStatus(flow.id)).toEqual('active')
    })

    it('pauses all active email workflows when the team aggregate breaches, even if no single flow does', async () => {
        const bigFlow = await insertEmailFlow('active')
        const toxicFlow = await insertEmailFlow('active')

        // bigFlow warns (3%), toxicFlow is below min sends (held healthy), but the aggregate
        // (1090 sent / 105 bounced ≈ 9.6%) breaches the team hard threshold
        const summary = await service.evaluateAndEnforce([
            { teamId, appSourceId: bigFlow.id, sent: 1000, bounced: 30, complained: 0 },
            { teamId, appSourceId: toxicFlow.id, sent: 90, bounced: 75, complained: 0 },
        ])

        expect(await getFlowStatus(bigFlow.id)).toEqual('paused')
        expect(await getFlowStatus(toxicFlow.id)).toEqual('paused')

        const rows = await getReputationRows()
        const teamRow = rows.find((r) => r.hog_flow_id === null)
        expect(teamRow).toMatchObject({ state: 'paused', scope: 'team', pause_reason: 'bounce' })
        for (const flowId of [bigFlow.id, toxicFlow.id]) {
            expect(rows.find((r) => r.hog_flow_id === flowId)).toMatchObject({
                state: 'paused',
                previous_flow_status: 'active',
            })
        }

        const teamTransition = summary.transitions.find((t) => t.scope === 'team')
        expect(teamTransition).toMatchObject({ team_id: teamId, new_state: 'paused', reason: 'bounce' })
    })
})
