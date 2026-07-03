import './ImpersonationNotice.scss'

import { useActions, useValues } from 'kea'
import { useEffect, useRef, useState } from 'react'

import { IconCollapse, IconEllipsis, IconWarning } from '@posthog/icons'
import { LemonButton, LemonCheckbox, LemonCollapse, LemonMenu, Spinner, Tooltip } from '@posthog/lemon-ui'

import { DraggableWithSnapZones, DraggableWithSnapZonesRef, SnapPosition } from 'lib/components/DraggableWithSnapZones'
import { dayjs } from 'lib/dayjs'
import { usePageVisibility } from 'lib/hooks/usePageVisibility'
import { IconDragHandle } from 'lib/lemon-ui/icons'
import { cn } from 'lib/utils/css-classes'
import { userLogic } from 'scenes/userLogic'

import { AdminLoginButtons } from './AdminLoginButtons'
import {
    ExpiredSessionInfo,
    ImpersonationTicketContext,
    TicketMessage,
    impersonationNoticeLogic,
} from './impersonationNoticeLogic'
import { ImpersonationReasonModal, ImpersonationReasonModalCancelButton } from './ImpersonationReasonModal'

const NOTICE_POSITION_PERSIST_KEY = 'impersonation-notice-position'

// DraggableWithSnapZones persists its snap position in localStorage but doesn't expose
// it, so read it back to decide which way the widened ticket panel should grow.
function getPersistedSnapPosition(): SnapPosition | null {
    try {
        const stored = localStorage.getItem(NOTICE_POSITION_PERSIST_KEY)
        return stored ? (JSON.parse(stored).snapPosition ?? null) : null
    } catch {
        return null
    }
}

function CountDown({ datetime, callback }: { datetime: dayjs.Dayjs; callback?: () => void }): JSX.Element {
    const [now, setNow] = useState(() => dayjs())
    const { isVisible: isPageVisible } = usePageVisibility()

    const duration = dayjs.duration(datetime.diff(now))
    const pastCountdown = duration.seconds() < 0

    const countdown = pastCountdown
        ? 'Expired'
        : duration.hours() > 0
          ? duration.format('HH:mm:ss')
          : duration.format('mm:ss')

    useEffect(() => {
        if (!isPageVisible) {
            return
        }

        setNow(dayjs())
        const interval = setInterval(() => setNow(dayjs()), 1000)
        return () => clearInterval(interval)
    }, [isPageVisible])

    useEffect(() => {
        if (pastCountdown) {
            callback?.() // oxlint-disable-line react-hooks/exhaustive-deps
        }
    }, [pastCountdown])

    return <span className="tabular-nums text-warning">{countdown}</span>
}

function TicketMessageBubble({ message }: { message: TicketMessage }): JSX.Element {
    const isCustomer = message.authorType === 'customer'

    return (
        <div className={cn('flex flex-col gap-0.5', isCustomer ? 'items-start' : 'items-end')}>
            <div className="flex items-center gap-1 text-[10px] text-muted-alt px-1">
                <span>{message.authorName}</span>
                <span>·</span>
                <span>{dayjs(message.createdAt).format('MMM D, h:mm A')}</span>
                {message.isPrivate && <span className="text-warning-dark">(private)</span>}
            </div>
            <div
                className={cn(
                    'rounded-lg px-2 py-1 text-xs max-w-[85%] whitespace-pre-wrap',
                    isCustomer ? 'bg-surface-tertiary' : 'bg-primary-highlight'
                )}
            >
                {message.content}
            </div>
        </div>
    )
}

function TicketMessagesContent({ messages, loading }: { messages: TicketMessage[]; loading: boolean }): JSX.Element {
    const messagesEndRef = useRef<HTMLDivElement>(null)

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, [messages.length])

    return (
        <div
            className="overflow-y-auto space-y-2 bg-surface-primary rounded p-2"
            style={{ height: '25vh', maxHeight: '300px' }}
        >
            {loading ? (
                <div className="flex items-center justify-center h-full">
                    <Spinner />
                </div>
            ) : messages.length === 0 ? (
                <div className="flex items-center justify-center h-full text-muted-alt text-xs">No messages yet</div>
            ) : (
                <>
                    {messages.map((message) => (
                        <TicketMessageBubble key={message.id} message={message} />
                    ))}
                    <div ref={messagesEndRef} />
                </>
            )}
        </div>
    )
}

function LoginAsContent({ ticketContext }: { ticketContext: ImpersonationTicketContext }): JSX.Element {
    return (
        <>
            <p className="ImpersonationNotice__message">
                {ticketContext.email ? (
                    <>
                        Customer: <span className="text-success">{ticketContext.email}</span>
                    </>
                ) : (
                    'No customer email on this ticket'
                )}
            </p>
            <AdminLoginButtons />
        </>
    )
}

function ImpersonationExpiredOverlay({ expiredSessionInfo }: { expiredSessionInfo: ExpiredSessionInfo }): JSX.Element {
    const { isReImpersonating, expiredSessionFromTicket, returnTicketLabel, returnTicketReason, isReturningToTicket } =
        useValues(impersonationNoticeLogic)
    const { reImpersonate, returnToPostHog, returnToTicket } = useActions(impersonationNoticeLogic)

    const [readOnly, setReadOnly] = useState(true)

    const cancelButton: ImpersonationReasonModalCancelButton =
        expiredSessionFromTicket && returnTicketLabel
            ? {
                  label: returnTicketLabel,
                  onClick: () => returnToTicket(),
                  loading: isReturningToTicket,
              }
            : {
                  label: 'Return to admin',
                  status: 'danger',
                  onClick: () => {
                      window.location.href = '/admin/'
                  },
                  sideAction: {
                      dropdown: {
                          placement: 'top-end',
                          overlay: (
                              <LemonButton fullWidth onClick={() => returnToPostHog()}>
                                  Return to PostHog
                              </LemonButton>
                          ),
                      },
                  },
              }

    return (
        <ImpersonationReasonModal
            isOpen
            closable={false}
            title="Impersonation session expired"
            description={`Your session impersonating ${expiredSessionInfo.email} has expired.`}
            confirmText="Re-impersonate"
            loading={isReImpersonating}
            defaultReason={expiredSessionFromTicket ? returnTicketReason : ''}
            onConfirm={(reason) => reImpersonate(reason, readOnly)}
            cancelButton={cancelButton}
        >
            <LemonCheckbox checked={readOnly} onChange={setReadOnly} label="Read-only mode (recommended)" />
        </ImpersonationReasonModal>
    )
}

function ImpersonationNoticeContent(): JSX.Element {
    const { user, userLoading } = useValues(userLogic)
    const { logout, loadUser } = useActions(userLogic)
    const {
        isReadOnly,
        isUpgradeModalOpen,
        isImpersonationUpgradeInProgress,
        canReturnToTicket,
        returnTicketLabel,
        returnTicketReason,
        isReturningToTicket,
        impersonationTicket,
        impersonationTicketLoading,
        ticketMessages,
        isTicketExpanded,
    } = useValues(impersonationNoticeLogic)
    const {
        closeUpgradeModal,
        upgradeImpersonation,
        setSessionExpired,
        returnToPostHog,
        returnToTicket,
        toggleTicketExpanded,
    } = useActions(impersonationNoticeLogic)

    const handleSessionExpired = (): void => {
        if (user) {
            setSessionExpired({
                email: user.email,
                userId: user.id,
                isImpersonatedUntil: user.is_impersonated_until ?? null,
            })
        }
    }

    return (
        <>
            <p className="ImpersonationNotice__message">
                Signed in as <span className="text-warning">{user?.email}</span>
                {user?.organization?.name && (
                    <>
                        {' '}
                        from <span className="text-warning">{user.organization.name}</span>
                    </>
                )}
                .
                {user?.is_impersonated_until && (
                    <>
                        {' '}
                        Expires in{' '}
                        <CountDown datetime={dayjs(user.is_impersonated_until)} callback={handleSessionExpired} />.
                    </>
                )}
            </p>
            {impersonationTicket && (
                <LemonCollapse
                    panels={[
                        {
                            key: 'ticket',
                            header: `Working on ticket #${impersonationTicket.ticket_number}`,
                            content: (
                                <TicketMessagesContent messages={ticketMessages} loading={impersonationTicketLoading} />
                            ),
                        },
                    ]}
                    activeKey={isTicketExpanded ? 'ticket' : undefined}
                    onChange={(key) => {
                        if ((key === 'ticket') !== isTicketExpanded) {
                            toggleTicketExpanded()
                        }
                    }}
                    size="small"
                    embedded
                />
            )}
            <div className="flex gap-2 justify-end">
                <LemonButton type="secondary" size="small" onClick={() => loadUser()} loading={userLoading}>
                    Refresh
                </LemonButton>
                {canReturnToTicket ? (
                    <LemonButton
                        type="primary"
                        size="small"
                        loading={isReturningToTicket}
                        onClick={() => returnToTicket()}
                    >
                        {returnTicketLabel}
                    </LemonButton>
                ) : (
                    <LemonButton
                        type="secondary"
                        status="danger"
                        size="small"
                        onClick={() => logout()}
                        sideAction={{
                            dropdown: {
                                placement: 'top-end',
                                overlay: (
                                    <LemonButton fullWidth size="small" onClick={() => returnToPostHog()}>
                                        Log out to PostHog
                                    </LemonButton>
                                ),
                            },
                        }}
                    >
                        Log out to admin
                    </LemonButton>
                )}
            </div>
            {isReadOnly && (
                <ImpersonationReasonModal
                    isOpen={isUpgradeModalOpen}
                    onClose={closeUpgradeModal}
                    onConfirm={upgradeImpersonation}
                    title="Upgrade to read-write impersonation"
                    description="Read-write mode allows you to make changes on behalf of the user. Please provide a reason for this upgrade."
                    confirmText="Upgrade"
                    loading={isImpersonationUpgradeInProgress}
                    defaultReason={canReturnToTicket ? returnTicketReason : ''}
                />
            )}
        </>
    )
}

export function ImpersonationNotice(): JSX.Element | null {
    const { user } = useValues(userLogic)

    const {
        isMinimized,
        isReadOnly,
        isImpersonated,
        isSessionExpired,
        expiredSessionInfo,
        ticketContext,
        isTicketExpanded,
    } = useValues(impersonationNoticeLogic)
    const { minimize, maximize, openUpgradeModal, setPageVisible } = useActions(impersonationNoticeLogic)

    const { isVisible: isPageVisible } = usePageVisibility()

    const draggableRef = useRef<DraggableWithSnapZonesRef>(null)
    const [isDragging, setIsDragging] = useState(false)
    const [snapPosition, setSnapPosition] = useState<SnapPosition | null>(() => getPersistedSnapPosition())

    const handleMinimize = (): void => {
        minimize()
        draggableRef.current?.trySnapTo('bottom-right')
    }

    const handleDragStop = (): void => {
        setIsDragging(false)
        setSnapPosition(getPersistedSnapPosition())
    }

    // Default (no stored position) is the bottom-right snap, which also grows left.
    const expandsLeft = snapPosition?.includes('right') ?? true

    useEffect(() => {
        setPageVisible(isPageVisible)
    }, [isPageVisible, setPageVisible])

    if (isSessionExpired && expiredSessionInfo) {
        return <ImpersonationExpiredOverlay expiredSessionInfo={expiredSessionInfo} />
    }

    // Staff actions (login-as) are now rendered inline in the ticket sidebar via StaffActionsPanel
    const showLoginAs = false

    if (!user || (!isImpersonated && !showLoginAs)) {
        return null
    }

    const title = showLoginAs ? 'Staff actions' : isReadOnly ? 'Read-only impersonation' : 'Read-write impersonation'

    return (
        <DraggableWithSnapZones
            ref={draggableRef}
            handle=".ImpersonationNotice__sidebar"
            defaultSnapPosition="bottom-right"
            persistKey={NOTICE_POSITION_PERSIST_KEY}
            onDragStart={() => setIsDragging(true)}
            onDragStop={handleDragStop}
        >
            <div
                className={cn(
                    'ImpersonationNotice',
                    isDragging && 'ImpersonationNotice--dragging',
                    isMinimized && 'ImpersonationNotice--minimized',
                    showLoginAs
                        ? 'ImpersonationNotice--login-as'
                        : isReadOnly
                          ? 'ImpersonationNotice--read-only'
                          : 'ImpersonationNotice--read-write',
                    !isMinimized && isTicketExpanded && 'ImpersonationNotice--ticket-expanded',
                    !isMinimized && isTicketExpanded && expandsLeft && 'ImpersonationNotice--expands-left'
                )}
            >
                <div className="ImpersonationNotice__sidebar">
                    <IconDragHandle className="ImpersonationNotice__drag-handle" />
                </div>
                {isMinimized && (
                    <Tooltip
                        title={
                            showLoginAs
                                ? 'Staff actions - click to expand'
                                : 'Signed in as a customer - click to expand'
                        }
                    >
                        <div className="ImpersonationNotice__minimized-content" onClick={maximize}>
                            <IconWarning className="ImpersonationNotice__minimized-icon" />
                        </div>
                    </Tooltip>
                )}
                {!isMinimized && (
                    <div className="ImpersonationNotice__main">
                        <div className="ImpersonationNotice__header">
                            <IconWarning className="ImpersonationNotice__warning-icon" />
                            <span className="ImpersonationNotice__title">{title}</span>
                            {isImpersonated && isReadOnly && (
                                <LemonMenu
                                    items={[
                                        {
                                            label: 'Upgrade to read-write',
                                            onClick: openUpgradeModal,
                                        },
                                    ]}
                                >
                                    <LemonButton size="xsmall" icon={<IconEllipsis />} />
                                </LemonMenu>
                            )}
                            <LemonButton size="xsmall" icon={<IconCollapse />} onClick={handleMinimize} />
                        </div>
                        <div className="ImpersonationNotice__content">
                            {showLoginAs ? (
                                <LoginAsContent ticketContext={ticketContext!} />
                            ) : (
                                <ImpersonationNoticeContent />
                            )}
                        </div>
                    </div>
                )}
            </div>
        </DraggableWithSnapZones>
    )
}
