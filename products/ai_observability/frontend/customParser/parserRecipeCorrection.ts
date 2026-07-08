// The verdict `handleCreateParserRecipeCall` returns after the browser compiles the agent's proposed
// recipe against the open trace event. Keys mirror that JSON contract (snake_case), so only the fields
// the corrective message reads are modeled here.
export interface ParserRecipeVerdict {
    valid?: boolean
    error?: string
    wrong_event?: boolean
    saved?: boolean
}

/**
 * Turns a browser-side compile/save verdict into the corrective message the user reviews and sends back
 * to the agent, or null when nothing needs saying. A recipe that compiled and saved returns null (the
 * success toast is the confirmation and the agent stays silent). A wrong-event, compile, or save failure
 * returns a message that carries the compiler error so the agent can fix the YAML and try again.
 */
export function formatParserRecipeCorrection(verdict: ParserRecipeVerdict): string | null {
    // Compiled and saved: no news is good news, so the agent hears nothing.
    if (verdict.valid && verdict.saved !== false) {
        return null
    }

    const detail = verdict.error?.trim() || undefined

    if (verdict.wrong_event) {
        return (
            'The custom parser recipe was written for a different trace event than the one now open, so it was ' +
            'not applied. Re-read the trace event sample in your context and write a new recipe for the event ' +
            'currently shown, or ask the user to reopen the original event, then call the tool again.'
        )
    }

    if (verdict.valid === false) {
        return (
            `The custom parser recipe failed to compile against the open trace event: ${detail ?? 'unknown error'}. ` +
            'Fix the YAML and call the tool again.'
        )
    }

    // Valid but not saved: the recipe compiled, only persistence failed. Retrying the same recipe is fine.
    return `The custom parser recipe compiled but could not be saved${detail ? `: ${detail}` : ''}. Please try again.`
}
