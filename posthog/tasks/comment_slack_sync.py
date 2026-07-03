import structlog
import posthoganalytics
from celery import shared_task

from posthog.comment.formatting import slack_to_content_and_rich_content
from posthog.helpers.slack_identity import resolve_posthog_user_for_slack, resolve_slack_user
from posthog.helpers.slack_thread_mirror import post_comment_to_slack_thread, slack_author_from_user
from posthog.models.comment import Comment, CommentSlackThread
from posthog.models.comment.slack_thread import DISCUSSIONS_SLACK_SYNC_FLAG
from posthog.models.integration import SlackIntegration

logger = structlog.get_logger(__name__)

# item_context key holding the source Slack message ts of an ingested reply — the
# idempotency key that makes re-processing the same Slack event a no-op.
SLACK_MESSAGE_TS_KEY = "slack_message_ts"

# item_context key holding the Slack ts of a reply's mirrored copy. Its presence is the
# idempotency marker that keeps Celery retries and backfill re-runs from double-posting.
SLACK_SYNCED_TS_KEY = "slack_synced_ts"


def _sync_killed(team_id: int) -> bool:
    """Kill switch for syncing on existing mirrors: only an explicit flag *off* halts it.

    The fail-closed creation gate is send_to_slack; here a flag-evaluation error or missing
    flag (None) must not silently drop user replies, so only False stops the sync.
    """
    try:
        return posthoganalytics.feature_enabled(DISCUSSIONS_SLACK_SYNC_FLAG, str(team_id)) is False
    except Exception:
        return False


def _mark_reply_synced(reply: Comment, ts: object) -> None:
    item_context = dict(reply.item_context) if isinstance(reply.item_context, dict) else {}
    item_context[SLACK_SYNCED_TS_KEY] = ts if isinstance(ts, str) else ""
    Comment.objects.filter(id=reply.id).update(item_context=item_context)


def _reply_skip_reason(reply: Comment) -> str | None:
    item_context = reply.item_context if isinstance(reply.item_context, dict) else {}
    if item_context.get("from_slack"):
        return "from_slack"  # came in from Slack — echoing it back would loop
    if item_context.get("is_emoji"):
        return "emoji"  # reactions are stored as reply comments but aren't messages
    if SLACK_SYNCED_TS_KEY in item_context:
        return "already_synced"
    return None


@shared_task(bind=True, ignore_result=True, max_retries=3, default_retry_delay=5)
def mirror_comment_reply_to_slack(self, comment_id: str) -> None:
    """Post a newly-created discussion reply into its parent's mirrored Slack thread.

    A discussion mirrors to exactly one Slack thread (1:1). Retries on a Slack failure rather
    than silently dropping the reply, and stamps the posted ts onto the reply so a retry after
    a successful post (e.g. worker death between the Slack ack and the task ack) can't re-post.
    """
    comment = Comment.objects.filter(id=comment_id).select_related("created_by").first()
    if comment is None or not comment.source_comment_id or _reply_skip_reason(comment):
        return
    if _sync_killed(comment.team_id):
        return

    mirror = (
        CommentSlackThread.objects.for_team(comment.team_id)
        .filter(source_comment_id=comment.source_comment_id)
        .select_related("integration")
        .first()
    )
    if mirror is None:
        return
    if not mirror.slack_thread_ts:
        # Reserved but the root post hasn't landed yet (send_to_slack mid-flight) — retry rather
        # than dropping the reply. A failed root post deletes the reservation, so the retry then
        # exits on mirror is None.
        raise self.retry()

    author_name, author_email = slack_author_from_user(comment.created_by)
    try:
        client = SlackIntegration(mirror.integration).client
        posted_ts = post_comment_to_slack_thread(
            client=client,
            channel=mirror.slack_channel_id,
            content=comment.content or "",
            rich_content=comment.rich_content,
            author_name=author_name,
            author_email=author_email,
            thread_ts=mirror.slack_thread_ts,
        )
    except Exception as exc:
        raise self.retry(exc=exc)
    _mark_reply_synced(comment, posted_ts)


@shared_task(bind=True, ignore_result=True, max_retries=3, default_retry_delay=5)
def ingest_slack_discussion_reply(
    self,
    comment_slack_thread_id: str,
    slack_user_id: str,
    text: str,
    blocks: list | None,
    message_ts: str,
) -> None:
    """Save a Slack thread reply as a discussion comment (the inbound mirror half).

    Runs off the webhook request thread: Slack expects the events endpoint to ack in ~3
    seconds and this path makes a ``users.info`` call. Idempotent per source Slack message
    ts, so task retries and duplicate event deliveries can't create duplicate comments.
    """
    mirror = (
        CommentSlackThread.objects.unscoped()
        .filter(id=comment_slack_thread_id)
        .select_related("integration__team")
        .first()
    )
    if mirror is None:
        return
    team = mirror.integration.team
    if _sync_killed(team.id):
        return

    if (
        message_ts
        and Comment.objects.filter(
            team_id=team.id,
            source_comment_id=mirror.source_comment_id,
            item_context__slack_message_ts=message_ts,
        ).exists()
    ):
        return

    content, rich_content = slack_to_content_and_rich_content(text, blocks)
    if not content and not rich_content:
        return

    try:
        client = SlackIntegration(mirror.integration).client
        client.timeout = 10
        user_info = resolve_slack_user(client, slack_user_id)
    except Exception as exc:
        raise self.retry(exc=exc)

    # Only attribute the comment to a PostHog user when Slack confirms the author belongs to
    # this integration's own workspace. In externally-shared (Slack Connect) channels the other
    # workspace's admin controls its users' profile emails, so trusting the email there would
    # let an outsider post as any org member.
    posthog_user = None
    if user_info.get("team_id") and user_info.get("team_id") == mirror.integration.integration_id:
        posthog_user = resolve_posthog_user_for_slack(user_info.get("email"), team)

    Comment.objects.create(
        team=team,
        scope=mirror.scope,
        item_id=mirror.item_id,
        # The reply hangs off the mirrored thread's root comment (None only for whole-item mirrors).
        source_comment_id=mirror.source_comment_id,
        content=content,
        rich_content=rich_content,
        # Slack users without a verified PostHog account stay author-less; their display
        # identity rides in item_context (name + avatar only — no email or Slack user id,
        # which would leak external participants' PII through the comments API).
        created_by=posthog_user,
        item_context={
            "from_slack": True,
            SLACK_MESSAGE_TS_KEY: message_ts,
            "slack_author_name": user_info["name"],
            "slack_author_avatar": user_info.get("avatar"),
        },
    )
    logger.info(
        "slack_discussion_reply_ingested",
        team_id=team.id,
        scope=mirror.scope,
        item_id=mirror.item_id,
        comment_slack_thread_id=comment_slack_thread_id,
    )


@shared_task(ignore_result=True)
def backfill_comment_slack_thread(comment_slack_thread_id: str) -> None:
    """Post a discussion's pre-existing replies into a freshly-mirrored Slack thread.

    Runs once, asynchronously, after send_to_slack — so the request isn't blocked on N sequential
    Slack posts. Bounded to replies created before the mirror: later replies belong exclusively to
    the live post_save signal, so the two paths can't both post the same reply. Best-effort per
    reply — a failure on one is logged and skipped — and each success is stamped with its posted
    ts, so a re-run never double-posts.
    """
    mirror = (
        CommentSlackThread.objects.unscoped().filter(id=comment_slack_thread_id).select_related("integration").first()
    )
    if mirror is None or not mirror.source_comment_id or not mirror.slack_thread_ts:
        return
    if _sync_killed(mirror.team_id):
        return

    try:
        client = SlackIntegration(mirror.integration).client
    except Exception:
        logger.warning("comment_slack_backfill_client_failed", comment_slack_thread_id=comment_slack_thread_id)
        return

    # source_comment_id matches the thread root, so this returns its replies (not the root itself).
    replies = (
        Comment.objects.filter(
            team_id=mirror.team_id,
            source_comment_id=mirror.source_comment_id,
            deleted=False,
            created_at__lt=mirror.created_at,
        )
        .select_related("created_by")
        .order_by("created_at")
    )
    for reply in replies:
        if _reply_skip_reason(reply):
            continue
        author_name, author_email = slack_author_from_user(reply.created_by)
        try:
            posted_ts = post_comment_to_slack_thread(
                client=client,
                channel=mirror.slack_channel_id,
                content=reply.content or "",
                rich_content=reply.rich_content,
                author_name=author_name,
                author_email=author_email,
                thread_ts=mirror.slack_thread_ts,
            )
        except Exception:
            logger.warning(
                "comment_slack_backfill_reply_failed",
                comment_slack_thread_id=comment_slack_thread_id,
                comment_id=str(reply.id),
            )
            continue
        _mark_reply_synced(reply, posted_ts)
