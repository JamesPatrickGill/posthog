import { afterMount, connect, kea, path, selectors } from 'kea'
import { loaders } from 'kea-loaders'

import { projectLogic } from 'scenes/projectLogic'

import { hogFlowsReputationRetrieve } from 'products/workflows/frontend/generated/api'
import type {
    EmailReputationSnapshotApi,
    TeamEmailReputationResponseApi,
    WorkflowEmailReputationSnapshotApi,
} from 'products/workflows/frontend/generated/api.schemas'

import type { workflowsReputationLogicType } from './workflowsReputationLogicType'

export const workflowsReputationLogic = kea<workflowsReputationLogicType>([
    path(['products', 'workflows', 'frontend', 'Workflows', 'Reputation', 'workflowsReputationLogic']),
    connect(() => ({
        values: [projectLogic, ['currentProjectId']],
    })),
    loaders(({ values }) => ({
        reputationResponse: [
            null as TeamEmailReputationResponseApi | null,
            {
                loadReputation: async () => {
                    if (!values.currentProjectId) {
                        return null
                    }
                    return await hogFlowsReputationRetrieve(String(values.currentProjectId))
                },
            },
        ],
    })),
    selectors({
        teamReputation: [
            (s) => [s.reputationResponse],
            (response): EmailReputationSnapshotApi | null => response?.reputation ?? null,
        ],
        history: [
            (s) => [s.reputationResponse],
            (response): readonly EmailReputationSnapshotApi[] => response?.history ?? [],
        ],
        workflowSnapshots: [
            (s) => [s.reputationResponse],
            (response): readonly WorkflowEmailReputationSnapshotApi[] => response?.workflows ?? [],
        ],
    }),
    afterMount(({ actions }) => {
        actions.loadReputation()
    }),
])
