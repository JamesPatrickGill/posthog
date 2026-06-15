from unittest.mock import MagicMock, patch

from posthog.temporal.ai.posthog_code_discord_followup import (
    PostHogCodeDiscordFollowupInputs,
    forward_discord_followup_activity,
)
from posthog.temporal.ai.posthog_code_discord_mention import (
    PostHogCodeDiscordMentionWorkflowInputs,
    create_discord_task_activity,
)


class TestCommandContextWiring:
    def test_create_task_injects_context_into_description(self):
        inputs = PostHogCodeDiscordMentionWorkflowInputs(
            interaction={
                "options": {"prompt": "review this"},
                "context": [{"author": {"username": "alice"}, "content": "here is the PR"}],
            },
            integration_id=1,
            guild_id="g1",
            user_id=42,
            discord_user_id="du1",
        )
        integration = MagicMock(id=7, team_id=5)
        task = MagicMock(id="task1")
        task.latest_run = MagicMock(id="r1")
        with (
            patch(
                "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
                return_value=(integration, MagicMock()),
            ),
            patch("products.tasks.backend.models.Task.create_and_run", return_value=task) as create,
            patch("products.tasks.backend.models.TaskRun.update_state_atomic"),
            patch("products.discord_app.backend.models.DiscordThreadTaskMapping.objects.update_or_create"),
            patch("products.tasks.backend.temporal.client.execute_task_processing_workflow"),
            patch("posthog.models.scoping.team_scope"),
        ):
            create_discord_task_activity(inputs, None, "anchor1", "t1")

        description = create.call_args.kwargs["description"]
        assert "here is the PR" in description
        assert description.strip().endswith("review this")
        # Title stays the bare prompt, not the context-laden description.
        assert create.call_args.kwargs["title"] == "review this"

    def test_create_task_without_context_uses_bare_prompt(self):
        inputs = PostHogCodeDiscordMentionWorkflowInputs(
            interaction={"options": {"prompt": "fix the bug"}},
            integration_id=1,
            guild_id="g1",
            user_id=42,
            discord_user_id="du1",
        )
        task = MagicMock(id="task1")
        task.latest_run = MagicMock(id="r1")
        with (
            patch(
                "posthog.temporal.ai.posthog_code_discord_mention._get_integration_and_client",
                return_value=(MagicMock(id=7, team_id=5), MagicMock()),
            ),
            patch("products.tasks.backend.models.Task.create_and_run", return_value=task) as create,
            patch("products.tasks.backend.models.TaskRun.update_state_atomic"),
            patch("products.discord_app.backend.models.DiscordThreadTaskMapping.objects.update_or_create"),
            patch("products.tasks.backend.temporal.client.execute_task_processing_workflow"),
            patch("posthog.models.scoping.team_scope"),
        ):
            create_discord_task_activity(inputs, None, "anchor1", "t1")

        assert create.call_args.kwargs["description"] == "fix the bug"


class TestFollowupContextWiring:
    def _mapping(self):
        task_run = MagicMock(is_terminal=False, state={"sandbox_url": "http://sbx"})
        mapping = MagicMock(integration_id=7, thread_id="t1", anchor_message_id="a1", discord_user_id="du1")
        mapping.task_run = task_run
        mapping.task.created_by = MagicMock(id=42, distinct_id="d42")
        return mapping

    def test_followup_frames_history_and_reply_into_message(self):
        inputs = PostHogCodeDiscordFollowupInputs(
            guild_id="g1",
            thread_id="t1",
            text="do that",
            discord_user_id="du1",
            context=[{"author": {"username": "alice"}, "content": "earlier chatter"}],
            replied_to={"author": {"username": "bob"}, "content": "the target"},
        )
        chain = MagicMock()
        chain.filter.return_value.select_related.return_value.first.return_value = self._mapping()
        send_result = MagicMock(success=True, retryable=False, status_code=200)
        with (
            patch(
                "products.discord_app.backend.models.DiscordThreadTaskMapping.objects.unscoped",
                return_value=chain,
            ),
            patch(
                "products.tasks.backend.services.agent_command.send_user_message",
                return_value=send_result,
            ) as send,
            patch(
                "products.tasks.backend.services.connection_token.create_sandbox_connection_token",
                return_value="tok",
            ),
            patch("products.discord_app.backend.discord_thread.DiscordThreadHandler"),
        ):
            forward_discord_followup_activity(inputs)

        sent = send.call_args.args[1]
        assert sent.index("earlier chatter") < sent.index("the target") < sent.index("do that")
        assert sent.endswith("do that")

    def test_followup_without_context_sends_bare_text(self):
        inputs = PostHogCodeDiscordFollowupInputs(guild_id="g1", thread_id="t1", text="ship it", discord_user_id="du1")
        chain = MagicMock()
        chain.filter.return_value.select_related.return_value.first.return_value = self._mapping()
        send_result = MagicMock(success=True, retryable=False, status_code=200)
        with (
            patch(
                "products.discord_app.backend.models.DiscordThreadTaskMapping.objects.unscoped",
                return_value=chain,
            ),
            patch(
                "products.tasks.backend.services.agent_command.send_user_message",
                return_value=send_result,
            ) as send,
            patch(
                "products.tasks.backend.services.connection_token.create_sandbox_connection_token",
                return_value="tok",
            ),
            patch("products.discord_app.backend.discord_thread.DiscordThreadHandler"),
        ):
            forward_discord_followup_activity(inputs)

        assert send.call_args.args[1] == "ship it"
