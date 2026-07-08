import { actions, kea, path, reducers } from 'kea'

import type { foregroundStreamLogicType } from './foregroundStreamLogicType'

/**
 * Global, unkeyed registry of the single "foreground" stream — the run currently rendered in the side
 * panel the user is watching. Tool apply-back reactions fire only for this stream, never for background
 * runs, full-page surfaces, or replayed history. A surface registers its `streamKey` on mount (and when
 * it switches runs) and clears it on unmount; the tool-event bus reads `foregroundStreamKey` to gate
 * `foregroundOnly` subscriptions at delivery time.
 */
export const foregroundStreamLogic = kea<foregroundStreamLogicType>([
    path(['products', 'posthog_ai', 'frontend', 'logics', 'foregroundStreamLogic']),

    actions({
        setForegroundStream: (streamKey: string) => ({ streamKey }),
        clearForegroundStream: (streamKey: string) => ({ streamKey }),
    }),

    reducers({
        foregroundStreamKey: [
            null as string | null,
            {
                setForegroundStream: (_, { streamKey }) => streamKey,
                // Key-checked clear: null the slot only when the departing key still owns it. Two
                // surfaces can overlap during a mount/unmount race (a toggle re-mounts the panel before
                // the old one tears down), so a late unmount must not clobber the newer registration.
                clearForegroundStream: (state, { streamKey }) => (state === streamKey ? null : state),
            },
        ],
    }),
])
