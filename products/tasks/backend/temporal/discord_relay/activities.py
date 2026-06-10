from typing import Any

from temporalio import activity

from posthog.temporal.common.logger import get_logger
from posthog.temporal.common.utils import close_db_connections

from products.tasks.backend.temporal.slack_relay.activities import RelaySlackMessageInput, _split_markdown_for_slack

logger = get_logger(__name__)

# Discord caps message content at 2000 chars; leave headroom for the mention prefix.
DISCORD_MESSAGE_TEXT_LIMIT = 1900


class _RelayAlreadyRecorded(Exception):
    """Raised when a relay was already recorded while holding the row lock."""


@activity.defn
@close_db_connections
def relay_discord_message(input: RelaySlackMessageInput) -> None:
    """Post an agent message into the Discord thread that launched the run.

    Discord renders standard markdown natively, so unlike the Slack relay no mrkdwn
    conversion is needed — only chunking to the message-size limit. No-ops when the
    run has no Discord thread mapping (the relay workflow fans out to both transports
    and lets each decide). Idempotent per relay_id via TaskRun.state.
    """
    from products.discord_app.backend.discord_thread import DiscordThreadContext, DiscordThreadHandler
    from products.discord_app.backend.models import DiscordThreadTaskMapping
    from products.tasks.backend.models import TaskRun

    try:
        task_run = TaskRun.objects.get(id=input.run_id)
    except TaskRun.DoesNotExist:
        logger.warning("discord_relay_run_not_found", run_id=input.run_id, relay_id=input.relay_id)
        return

    state = task_run.state or {}
    if input.relay_id in (state.get("discord_sent_relay_ids") or []):
        logger.info("discord_relay_duplicate_skipped", run_id=input.run_id, relay_id=input.relay_id)
        return

    mapping = DiscordThreadTaskMapping.objects.unscoped().filter(task_run=task_run).first()
    if mapping is None:
        logger.info("discord_relay_mapping_not_found", run_id=input.run_id, relay_id=input.relay_id)
        return

    text = (input.text or "").strip()
    if not text:
        logger.info("discord_relay_empty_text", run_id=input.run_id, relay_id=input.relay_id)
        return

    chunks = _split_markdown_for_slack(text, limit=DISCORD_MESSAGE_TEXT_LIMIT)

    context = DiscordThreadContext(
        integration_id=mapping.integration_id,
        channel_id=mapping.channel_id,
        thread_id=mapping.thread_id,
        anchor_message_id=mapping.anchor_message_id or None,
        discord_user_id=mapping.discord_user_id or None,
    )
    handler = DiscordThreadHandler(context)

    mention_prefix = f"<@{mapping.discord_user_id}> " if mapping.discord_user_id else ""
    for index, chunk in enumerate(chunks):
        prefix = mention_prefix if index == 0 else ""
        handler.post_thread_message(f"{prefix}{chunk}")
    if input.reaction_emoji is not None:
        handler.update_reaction(input.reaction_emoji)

    def _record_sent_relay(run_state: dict[str, Any]) -> None:
        sent_relay_ids = run_state.get("discord_sent_relay_ids") or []
        if input.relay_id in sent_relay_ids:
            raise _RelayAlreadyRecorded
        sent_relay_ids.append(input.relay_id)
        # Rolling window bounds state size while keeping idempotency for recent relays.
        run_state["discord_sent_relay_ids"] = sent_relay_ids[-30:]

    try:
        TaskRun.mutate_state_atomic(input.run_id, _record_sent_relay)
    except _RelayAlreadyRecorded:
        logger.info("discord_relay_duplicate_skipped", run_id=input.run_id, relay_id=input.relay_id)
