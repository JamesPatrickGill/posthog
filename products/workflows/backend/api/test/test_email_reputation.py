from posthog.test.base import APIBaseTest
from unittest.mock import patch

from django.test import override_settings

from rest_framework import status

from products.notifications.backend.facade.api import NotificationType, Priority
from products.workflows.backend.models import EmailReputationState, HogFlow


class TestEmailReputationAPI(APIBaseTest):
    def _create_flow(self, status_: str = "active") -> HogFlow:
        return HogFlow.objects.create(
            team=self.team,
            name="Email workflow",
            status=status_,
            trigger={"type": "event"},
            edges=[],
            actions=[],
            billable_action_types=["function_email"],
        )

    def _create_reputation(self, hog_flow: HogFlow | None, **kwargs) -> EmailReputationState:
        state = EmailReputationState(
            team=self.team,
            hog_flow=hog_flow,
            scope=EmailReputationState.Scope.WORKFLOW if hog_flow else EmailReputationState.Scope.TEAM,
            **kwargs,
        )
        state.save()
        return state

    def test_reenable_restores_flow_and_resets_reputation_state(self):
        flow = self._create_flow(status_="paused")
        self._create_reputation(
            flow,
            state=EmailReputationState.State.PAUSED,
            pause_reason=EmailReputationState.Reason.BOUNCE,
            previous_flow_status="active",
            bounce_rate=0.07,
        )
        self._create_reputation(None, state=EmailReputationState.State.PAUSED)

        response = self.client.post(f"/api/projects/{self.team.id}/hog_flows/{flow.id}/reputation/reenable")

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["status"] == "active"

        flow.refresh_from_db()
        assert flow.status == "active"

        workflow_state = EmailReputationState.objects.unscoped().get(hog_flow_id=flow.id)
        assert workflow_state.state == EmailReputationState.State.HEALTHY
        assert workflow_state.pause_reason is None
        assert workflow_state.paused_at is None
        assert workflow_state.previous_flow_status is None

        # Last paused workflow re-enabled: the team-level pause clears too
        team_state = EmailReputationState.objects.unscoped().get(team_id=self.team.id, hog_flow__isnull=True)
        assert team_state.state == EmailReputationState.State.HEALTHY

        # The reputation snapshot rides the workflow serializer for the banner
        assert response.json()["reputation"]["state"] == "healthy"

    def test_reenable_rejected_when_flow_is_not_paused(self):
        flow = self._create_flow(status_="active")

        response = self.client.post(f"/api/projects/{self.team.id}/hog_flows/{flow.id}/reputation/reenable")

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_plain_status_patch_cannot_leave_or_enter_paused(self):
        paused_flow = self._create_flow(status_="paused")
        response = self.client.patch(f"/api/projects/{self.team.id}/hog_flows/{paused_flow.id}/", {"status": "active"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        paused_flow.refresh_from_db()
        assert paused_flow.status == "paused"

        active_flow = self._create_flow(status_="active")
        response = self.client.patch(f"/api/projects/{self.team.id}/hog_flows/{active_flow.id}/", {"status": "paused"})
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        active_flow.refresh_from_db()
        assert active_flow.status == "active"

    def test_team_reputation_endpoint_returns_aggregate_row_or_null(self):
        response = self.client.get(f"/api/projects/{self.team.id}/hog_flows/reputation")
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {"reputation": None}

        self._create_reputation(None, state=EmailReputationState.State.WARNED, bounce_rate=0.03)
        response = self.client.get(f"/api/projects/{self.team.id}/hog_flows/reputation")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["reputation"]["state"] == "warned"
        assert response.json()["reputation"]["scope"] == "team"

    @override_settings(INTERNAL_API_SECRET="test-secret")
    @patch("products.workflows.backend.reputation.notifications.create_notification")
    def test_internal_notify_endpoint_creates_notifications(self, mock_create_notification):
        flow = self._create_flow()
        payload = {
            "transitions": [
                {
                    "team_id": self.team.id,
                    "scope": "workflow",
                    "new_state": "paused",
                    "reason": "bounce",
                    "rate": 0.06,
                    "threshold": 0.05,
                    "hog_flow_id": str(flow.id),
                    "hog_flow_name": flow.name,
                },
                {"team_id": self.team.id},  # invalid: skipped, not fatal
            ]
        }

        self.client.logout()
        response = self.client.post(
            "/api/internal/hog_flows/email_reputation_notify",
            payload,
            content_type="application/json",
            HTTP_X_INTERNAL_API_SECRET="test-secret",
        )

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json() == {"notified": 1}

        assert mock_create_notification.call_count == 1
        notification = mock_create_notification.call_args[0][0]
        assert notification.team_id == self.team.id
        assert notification.notification_type == NotificationType.EMAIL_REPUTATION
        assert notification.priority == Priority.CRITICAL
        assert "paused" in notification.title
        assert notification.source_url == f"/workflows/{flow.id}/metrics"

    @override_settings(INTERNAL_API_SECRET="test-secret")
    def test_internal_notify_endpoint_rejects_bad_secret(self):
        self.client.logout()
        response = self.client.post(
            "/api/internal/hog_flows/email_reputation_notify",
            {"transitions": []},
            content_type="application/json",
            HTTP_X_INTERNAL_API_SECRET="wrong-secret",
        )
        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
