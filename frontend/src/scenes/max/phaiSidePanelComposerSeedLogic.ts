import { afterMount, connect, kea, key, listeners, path, props } from 'kea'

import { sidePanelStateLogic } from '~/layout/navigation-3000/sidepanel/sidePanelStateLogic'
import { SidePanelTab } from '~/types'

import { ComposerSeed, composerSeedLogic } from 'products/posthog_ai/frontend/api/logics'

import { parseCommandString } from './maxLogic'
import type { phaiSidePanelComposerSeedLogicType } from './phaiSidePanelComposerSeedLogicType'

export interface PhaiSidePanelComposerSeedLogicProps {
    /** The embedded composer's panel key — the same one passed to its `taskTrackerSceneLogic`/`composerSeedLogic`. */
    panelId: string
}

// Parse the side-panel option string exactly as legacy Max does (`mode=` stripped, leading `!` = auto-run)
// and forward the prompt to the surface seam. Reusing `parseCommandString` keeps the `!` convention
// byte-for-byte identical to the legacy consumer; only the prompt + auto-submit flag cross into the surface.
function forwardSeed(options: string | null | undefined, setSeed: (seed: ComposerSeed) => void): void {
    if (typeof options !== 'string') {
        return
    }
    const { autoRun, question } = parseCommandString(options)
    if (!question) {
        return
    }
    setSeed({ prompt: question, autoSubmit: autoRun })
}

/**
 * Bridges the legacy `initialMaxPrompt` side-panel option into the new PostHog AI composer. `openMax` (and any
 * other producer) still rides the prompt on `openSidePanel(SidePanelTab.Max, prompt)` — the option only the
 * legacy `maxLogic` used to read. Mounted by the new view's panel (`PhaiSidePanelChat`), this reads the same
 * option and seeds the surface's `composerSeedLogic`, so a single consumer fixes every producer without
 * touching `openMax` or the legacy consumption path. The two orderings both hold: a CTA that fires before this
 * panel mounts leaves the option on `sidePanelStateLogic` for the afterMount read; a CTA fired while the panel
 * is already open is caught by the `openSidePanel` listener.
 */
export const phaiSidePanelComposerSeedLogic = kea<phaiSidePanelComposerSeedLogicType>([
    path(['scenes', 'max', 'phaiSidePanelComposerSeedLogic']),
    props({} as PhaiSidePanelComposerSeedLogicProps),
    key((props) => props.panelId),

    connect((props: PhaiSidePanelComposerSeedLogicProps) => ({
        values: [sidePanelStateLogic, ['selectedTab', 'selectedTabOptions']],
        actions: [composerSeedLogic({ panelId: props.panelId }), ['setSeed']],
    })),

    listeners(({ actions }) => ({
        [sidePanelStateLogic.actionTypes.openSidePanel]: ({ tab, options }) => {
            if (tab !== SidePanelTab.Max) {
                return
            }
            forwardSeed(options, actions.setSeed)
        },
    })),

    afterMount(({ actions, values }) => {
        // A CTA fires `openSidePanel` before this panel mounts, so the prompt is already sitting on
        // `sidePanelStateLogic` by the time we mount — pick it up (mirrors legacy maxLogic's afterMount read).
        if (values.selectedTab === SidePanelTab.Max) {
            forwardSeed(values.selectedTabOptions, actions.setSeed)
        }
    }),
])
