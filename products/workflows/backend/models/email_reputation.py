from django.db import models

from posthog.models.scoping.root_mixin import TeamScopedRootMixin
from posthog.models.utils import UUIDModel


class EmailReputationState(TeamScopedRootMixin, UUIDModel):
    """
    Tracks per-workflow and per-tenant email deliverability reputation so a single bad sender can be
    warned and paused before AWS SES reacts at the shared-account level.

    One row per workflow (``hog_flow`` set, ``scope=WORKFLOW``) plus one aggregate row per team
    (``hog_flow`` null, ``scope=TEAM``). Written primarily by the Node Temporal evaluator via raw SQL;
    read by Django for the workflow reputation banner and the manual re-enable action.
    """

    class Meta:
        db_table = "posthog_emailreputationstate"
        constraints = [
            models.UniqueConstraint(
                fields=["team", "hog_flow"],
                condition=models.Q(hog_flow__isnull=False),
                name="unique_workflow_reputation",
            ),
            models.UniqueConstraint(
                fields=["team"],
                condition=models.Q(hog_flow__isnull=True),
                name="unique_team_reputation",
            ),
        ]
        indexes = [
            models.Index(fields=["team", "state"]),
        ]

    class Scope(models.TextChoices):
        WORKFLOW = "workflow"
        TEAM = "team"

    class State(models.TextChoices):
        HEALTHY = "healthy"
        WARNED = "warned"
        PAUSED = "paused"

    class Reason(models.TextChoices):
        BOUNCE = "bounce"
        COMPLAINT = "complaint"

    # db_constraint=False: posthog_team is a hot table; a real FK constraint would lock it on deploy.
    team = models.ForeignKey("posthog.Team", on_delete=models.CASCADE, db_constraint=False)
    # Null hog_flow marks the team-level aggregate row.
    hog_flow = models.ForeignKey("workflows.HogFlow", on_delete=models.CASCADE, null=True, blank=True)
    scope = models.CharField(max_length=20, choices=Scope)
    state = models.CharField(max_length=20, choices=State, default=State.HEALTHY)

    # Rates and sample size from the most recent evaluation window.
    bounce_rate = models.FloatField(default=0.0)
    complaint_rate = models.FloatField(default=0.0)
    emails_sent = models.BigIntegerField(default=0)

    window_end = models.DateTimeField(null=True, blank=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)
    state_changed_at = models.DateTimeField(null=True, blank=True)
    warned_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)

    pause_reason = models.CharField(max_length=20, choices=Reason, null=True, blank=True)
    # Status each workflow held before the evaluator auto-paused it, so manual re-enable can restore it.
    previous_flow_status = models.CharField(max_length=20, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        target = f"hog_flow {self.hog_flow_id}" if self.hog_flow_id else "team"
        return f"EmailReputationState({target}, {self.state})"
