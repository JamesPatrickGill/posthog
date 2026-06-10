from typing import Any

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

import structlog

from posthog.models.integration import (
    DiscordBotClient,
    Integration,
    discord_bridge_configured,
    discord_deployment_region,
)
from posthog.models.user_integration import UserIntegration

from products.discord_app.backend.repos import invalidate_user_repo_list_cache

logger = structlog.get_logger(__name__)


@receiver(post_save, sender=UserIntegration)
@receiver(post_delete, sender=UserIntegration)
def invalidate_repo_list_on_user_github_change(sender: Any, instance: UserIntegration, **kwargs: Any) -> None:
    if instance.kind == UserIntegration.IntegrationKind.GITHUB:
        invalidate_user_repo_list_cache(instance.user_id)


@receiver(post_delete, sender=Integration)
def push_guild_capture_key_on_discord_disconnect(sender: Any, instance: Integration, **kwargs: Any) -> None:
    """Keep the bot's guild analytics in sync when a discord binding is removed.

    If another project is still bound to the guild, push its capture key; otherwise push an
    empty key, which disconnects guild analytics on the bot side. Best-effort — a bot outage
    must not block the deletion.
    """
    if instance.kind != "discord" or not instance.integration_id or not discord_bridge_configured():
        return
    remaining = (
        Integration.objects.filter(kind="discord", integration_id=instance.integration_id)
        .select_related("team")
        .order_by("-created_at")
        .first()
    )
    try:
        DiscordBotClient().connect_guild(
            guild_id=instance.integration_id,
            region=discord_deployment_region(),
            project_api_key=remaining.team.api_token if remaining else "",
        )
    except Exception as e:
        logger.warning("discord_disconnect_guild_push_failed", guild_id=instance.integration_id, error=str(e))
