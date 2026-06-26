import { getCookie } from 'lib/api'

// The impersonation reason is required by the backend but never persisted server-side
// (it's only emitted to analytics). We cache it here so "Change user" and the read-write
// upgrade can reuse the original reason without re-prompting the operator. Each reason is
// keyed under a per-user-id localStorage key so a stale entry can never be applied to the
// wrong person, and every key is swept on logout.
const IMPERSONATION_REASON_KEY_PREFIX = 'ph_impersonation_reason_'

export function setStoredImpersonationReason(userId: number, reason: string): void {
    try {
        localStorage.setItem(IMPERSONATION_REASON_KEY_PREFIX + userId, reason)
    } catch {
        // localStorage can throw (e.g. Safari private mode) — caching is best-effort.
    }
}

export function getStoredImpersonationReason(userId: number | null | undefined): string | null {
    if (userId == null) {
        return null
    }
    try {
        return localStorage.getItem(IMPERSONATION_REASON_KEY_PREFIX + userId)
    } catch {
        return null
    }
}

export function clearAllStoredImpersonationReasons(): void {
    try {
        // Remove every cached reason (one per impersonated user) so nothing lingers after logout.
        for (const key of Object.keys(localStorage)) {
            if (key.startsWith(IMPERSONATION_REASON_KEY_PREFIX)) {
                localStorage.removeItem(key)
            }
        }
    } catch {
        // Best-effort — see setStoredImpersonationReason.
    }
}

async function ensureAdminOAuth2(): Promise<void> {
    const authCheckResponse = await fetch('/admin/auth_check', {
        method: 'GET',
        credentials: 'same-origin',
        redirect: 'manual',
    })

    if (authCheckResponse.ok) {
        return
    }

    const width = 600
    const height = 700
    const left = window.screen.width / 2 - width / 2
    const top = window.screen.height / 2 - height / 2

    const authWindow = window.open(
        '/admin/oauth2/success',
        'admin_oauth2',
        `width=${width},height=${height},top=${top},left=${left},toolbar=no,location=no,directories=no,status=no,menubar=no,scrollbars=yes,resizable=yes`
    )

    if (!authWindow) {
        throw new Error('Popup blocked. Please allow popups for this site and try again.')
    }

    // Resolve ONLY once the popup confirms success via `oauth2_complete`. A popup that closes
    // without sending it means the admin session was never established — proceeding anyway would
    // fire the impersonation POST against a stale session, which silently fails and forces a retry.
    await new Promise<void>((resolve, reject) => {
        let checkClosed: ReturnType<typeof setInterval>
        let gracePeriodTimeout: ReturnType<typeof setTimeout> | null = null
        let completed = false

        const cleanup = (): void => {
            clearInterval(checkClosed)
            if (gracePeriodTimeout) {
                clearTimeout(gracePeriodTimeout)
            }
            window.removeEventListener('message', handleMessage)
        }

        const handleMessage = (event: MessageEvent): void => {
            if (event.origin !== window.location.origin) {
                return
            }
            if (event.data?.type === 'oauth2_complete') {
                completed = true
                cleanup()
                resolve()
            }
        }
        window.addEventListener('message', handleMessage)

        checkClosed = setInterval(() => {
            if (authWindow.closed) {
                clearInterval(checkClosed)
                // The success message can land a tick after the popup closes — give it a brief
                // grace period before deciding this was a cancellation.
                gracePeriodTimeout = setTimeout(() => {
                    if (!completed) {
                        cleanup()
                        reject(new Error('Admin authentication was cancelled. Please try impersonating again.'))
                    }
                }, 500)
            }
        }, 500)
    })
}

export interface AdminLoginAsParams {
    userId: number
    reason: string
    readOnly: boolean
}

export async function adminLoginAs({ userId, reason, readOnly }: AdminLoginAsParams): Promise<void> {
    await ensureAdminOAuth2()

    const loginResponse = await fetch(`/admin/login/user/${userId}/`, {
        method: 'POST',
        credentials: 'same-origin',
        mode: 'cors',
        headers: {
            'X-CSRFToken': getCookie('posthog_csrftoken') as string,
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: new URLSearchParams({
            read_only: readOnly ? 'true' : 'false',
            reason,
        }),
    })

    if (!loginResponse.ok) {
        throw new Error(`django-loginas request resulted in status ${loginResponse.status}`)
    }

    setStoredImpersonationReason(userId, reason)
}
