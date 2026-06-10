import typing
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from posthog.temporal.common.base import PostHogWorkflow

from .activities import RelaySlackMessageInput, relay_slack_message


@workflow.defn(name="posthog-code-agent-relay")
class PostHogCodeAgentRelayWorkflow(PostHogWorkflow):
    @staticmethod
    def parse_inputs(inputs: list[str]) -> typing.Any:
        raise NotImplementedError("PostHogCodeAgentRelayWorkflow is not intended to be started via CLI")

    @workflow.run
    async def run(self, input: RelaySlackMessageInput) -> None:
        # Fan out to both chat transports; each relay no-ops unless the run has its
        # thread mapping, so a run only ever posts to the transport that launched it.
        await workflow.execute_activity(
            relay_slack_message,
            input,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        # Referenced by name: importing the discord activity here would cycle back into
        # this package (discord_relay.activities imports the shared input/splitter from
        # slack_relay.activities).
        await workflow.execute_activity(
            "relay_discord_message",
            input,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
