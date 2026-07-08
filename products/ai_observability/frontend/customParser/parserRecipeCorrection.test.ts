import { formatParserRecipeCorrection } from './parserRecipeCorrection'

describe('formatParserRecipeCorrection', () => {
    it('returns null when the recipe compiled and saved so no corrective is sent', () => {
        expect(formatParserRecipeCorrection({ valid: true, recipe_id: 'r1' } as any)).toBeNull()
    })

    it('embeds the compiler error so the agent can fix the YAML and retry', () => {
        const message = formatParserRecipeCorrection({ valid: false, error: 'unknown rule key "emitt"' })

        expect(message).toContain('failed to compile')
        expect(message).toContain('unknown rule key "emitt"')
        expect(message).toContain('call the tool again')
    })

    it('explains the open event changed on a wrong-event verdict', () => {
        const message = formatParserRecipeCorrection({ valid: false, wrong_event: true, error: 'different event' })

        expect(message).toContain('different trace event')
        expect(message).toContain('call the tool again')
    })
})
