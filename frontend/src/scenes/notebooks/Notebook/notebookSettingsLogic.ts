import { actions, kea, path, reducers } from 'kea'

import type { notebookSettingsLogicType } from './notebookSettingsLogicType'

// This logic contains settings that should persist across all notebooks
export const notebookSettingsLogic = kea<notebookSettingsLogicType>([
    path(['scenes', 'notebooks', 'notebooks', 'notebookSettingsLogic']),
    actions({
        setIsMarkdownExpanded: (expanded: boolean) => ({ expanded }),
        setShowKernelInfo: (showKernelInfo: boolean) => ({ showKernelInfo }),
    }),
    reducers(() => ({
        isMarkdownExpanded: [
            true,
            { persist: true },
            {
                setIsMarkdownExpanded: (_, { expanded }) => expanded,
            },
        ],
        showKernelInfo: [
            false,
            {
                setShowKernelInfo: (_, { showKernelInfo }) => showKernelInfo,
            },
        ],
    })),
])
