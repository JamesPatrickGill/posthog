"""Turn email-reputation state transitions detected by the Node evaluator into in-app notifications.

The Node Temporal evaluator owns detection, persistence and enforcement; it POSTs the resulting
transitions to the internal endpoint, which calls :func:`notify_reputation_transition` for each so
notifications still flow through the notifications facade rather than being written from Node.
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from posthog.models.team.team import Team

from products.notifications.backend.facade.api import (
    NotificationData,
    NotificationType,
    Priority,
    TargetType,
    create_notification,
)

logger = structlog.get_logger(__name__)

WARNED = "warned"
PAUSED = "paused"
SCOPE_WORKFLOW = "workflow"
SCOPE_TEAM = "team"


@dataclass(frozen=True)
class ReputationTransition:
    """A single reputation state change to notify about."""

    team_id: int
    scope: str  # "workflow" | "team"
    new_state: str  # "warned" | "paused"
    reason: str  # "bounce" | "complaint"
    rate: float
    threshold: float
    hog_flow_id: Optional[str] = None
    hog_flow_name: Optional[str] = None


def _signal_label(reason: str) -> str:
    return "complaint rate" if reason == "complaint" else "bounce rate"


def _build_copy(t: ReputationTransition) -> tuple[str, str]:
    signal = _signal_label(t.reason)
    rate = f"{t.rate * 100:.1f}%"
    threshold = f"{t.threshold * 100:.1f}%"

    if t.scope == SCOPE_WORKFLOW:
        subject = f"Workflow “{t.hog_flow_name or t.hog_flow_id}”"
    else:
        subject = "Email sending for this project"

    if t.new_state == PAUSED:
        title = f"{subject} paused — {signal} {rate}"
        body = (
            f"{signal.capitalize()} reached {rate}, exceeding the {threshold} limit. "
            "Sending has been paused to protect deliverability. Clean the recipient list, then re-enable it."
        )
    else:
        title = f"{subject}: {signal} {rate}"
        body = (
            f"{signal.capitalize()} is at {rate}, above the {threshold} warning threshold. "
            "Review your recipient list — sending will be paused if it does not improve."
        )
    return title, body


def notify_reputation_transition(transition: ReputationTransition) -> None:
    """Create an in-app notification for a single reputation transition. Best-effort and never raises."""
    try:
        title, body = _build_copy(transition)
        priority = Priority.CRITICAL if transition.new_state == PAUSED else Priority.NORMAL

        if transition.scope == SCOPE_WORKFLOW:
            target_type = TargetType.TEAM
            target_id = str(transition.team_id)
            source_url = f"/workflows/{transition.hog_flow_id}/metrics" if transition.hog_flow_id else "/workflows"
        else:
            team = Team.objects.filter(id=transition.team_id).first()
            if team is None:
                logger.warning("reputation_notify_team_not_found", team_id=transition.team_id)
                return
            target_type = TargetType.ORGANIZATION
            target_id = str(team.organization_id)
            source_url = "/workflows"

        create_notification(
            NotificationData(
                team_id=transition.team_id,
                notification_type=NotificationType.EMAIL_REPUTATION,
                priority=priority,
                title=title,
                body=body,
                target_type=target_type,
                target_id=target_id,
                source_url=source_url,
            )
        )
    except Exception:
        logger.exception(
            "reputation_notify_failed",
            team_id=transition.team_id,
            scope=transition.scope,
            new_state=transition.new_state,
        )
