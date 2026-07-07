import { useState } from 'react'

import { IconChevronDown, IconPause, IconPencil, IconShield, IconUnlock } from '@posthog/icons'
import {
    Button,
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuGroup,
    DropdownMenuItem,
    DropdownMenuLabel,
    DropdownMenuTrigger,
    Tooltip,
    TooltipContent,
    TooltipProvider,
    TooltipTrigger,
} from '@posthog/quill-primitives'

// IconRobot is not exported from @posthog/icons — it lives only in the legacy lib icon set.
import { IconRobot } from 'lib/lemon-ui/icons'

import { getModeLabel, MODE_OPTIONS, type PermissionMode } from 'products/posthog_ai/frontend/utils/composerModes'
import { InitialPermissionModeEnumApi } from 'products/tasks/frontend/generated/api.schemas'

interface ModeStyle {
    icon: JSX.Element
    className: string
}

// Port of `/code`'s `modeStyles` — same icon vocabulary and color coding per mode.
const MODE_STYLES: Record<PermissionMode, ModeStyle> = {
    [InitialPermissionModeEnumApi.Auto]: { icon: <IconRobot />, className: 'text-accent' },
    [InitialPermissionModeEnumApi.Default]: { icon: <IconPencil />, className: 'text-secondary' },
    [InitialPermissionModeEnumApi.AcceptEdits]: { icon: <IconShield />, className: 'text-success' },
    [InitialPermissionModeEnumApi.Plan]: { icon: <IconPause />, className: 'text-warning' },
    [InitialPermissionModeEnumApi.BypassPermissions]: { icon: <IconUnlock />, className: 'text-danger' },
}

export interface ComposerModePickerProps {
    selectedMode: PermissionMode
    onModeChange: (mode: PermissionMode) => void
    /** Restrict the menu to a subset of modes (e.g. the modes a plan approval offers). Defaults to all. */
    modes?: PermissionMode[]
}

/**
 * Controlled, logic-free permission-mode picker for a composer footer — the parity counterpart to `/code`'s
 * `ModeSelector`, including its styling: a subtle trigger tinted in the mode color, and a menu of icon + label
 * rows with the mode description in a tooltip. The caller owns the selection and its side effects (the run
 * composer syncs it to the running agent at send time; the new-task composer seeds the first run with it).
 * Also reused by the plan-approval card, where `modes` narrows the menu to the wire-offered modes.
 */
export function ComposerModePicker({ selectedMode, onModeChange, modes }: ComposerModePickerProps): JSX.Element {
    const [open, setOpen] = useState(false)
    const currentStyle = MODE_STYLES[selectedMode]
    const options = modes ? MODE_OPTIONS.filter((option) => modes.includes(option.value)) : MODE_OPTIONS

    return (
        <DropdownMenu open={open} onOpenChange={setOpen}>
            <DropdownMenuTrigger
                render={
                    <Button variant="default" size="sm" aria-label="Mode">
                        <span className={currentStyle.className}>{currentStyle.icon}</span>
                        <span className={currentStyle.className}>{getModeLabel(selectedMode)}</span>
                        <IconChevronDown className="text-muted" />
                    </Button>
                }
            />
            <DropdownMenuContent align="start" side="top" sideOffset={6} className="min-w-[200px]">
                {/* Base UI's GroupLabel (what DropdownMenuLabel renders) requires a Group ancestor. */}
                <DropdownMenuGroup>
                    <DropdownMenuLabel>Mode</DropdownMenuLabel>
                    <TooltipProvider>
                        {options.map((option) => (
                            <Tooltip key={option.value}>
                                <TooltipTrigger
                                    render={
                                        <DropdownMenuItem
                                            onClick={() => {
                                                onModeChange(option.value)
                                                setOpen(false)
                                            }}
                                        />
                                    }
                                >
                                    <span className={MODE_STYLES[option.value].className}>
                                        {MODE_STYLES[option.value].icon}
                                    </span>
                                    <span className="whitespace-nowrap">{option.label}</span>
                                </TooltipTrigger>
                                <TooltipContent side="right">{option.description}</TooltipContent>
                            </Tooltip>
                        ))}
                    </TooltipProvider>
                </DropdownMenuGroup>
            </DropdownMenuContent>
        </DropdownMenu>
    )
}
