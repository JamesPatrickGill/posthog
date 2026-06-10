from unittest.mock import MagicMock, patch

from products.tasks.backend.temporal.discord_relay.activities import relay_discord_message
from products.tasks.backend.temporal.slack_relay.activities import RelaySlackMessageInput


def _input(text: str = "Here is my analysis", relay_id: str = "r1") -> RelaySlackMessageInput:
    return RelaySlackMessageInput(run_id="run1", relay_id=relay_id, text=text)


def _mapping() -> MagicMock:
    mapping = MagicMock()
    mapping.integration_id = 1
    mapping.channel_id = "t1"
    mapping.thread_id = "t1"
    mapping.anchor_message_id = "anchor1"
    mapping.discord_user_id = "du1"
    return mapping


class TestRelayDiscordMessage:
    def _run(self, input, mapping, state=None):
        task_run = MagicMock()
        task_run.state = state or {}
        handler = MagicMock()
        with (
            patch("products.tasks.backend.models.TaskRun") as task_run_cls,
            patch("products.discord_app.backend.models.DiscordThreadTaskMapping") as mapping_cls,
            patch("products.discord_app.backend.discord_thread.DiscordThreadHandler", return_value=handler),
        ):
            task_run_cls.objects.get.return_value = task_run
            mapping_cls.objects.unscoped.return_value.filter.return_value.first.return_value = mapping
            relay_discord_message(input)
            return handler, task_run_cls

    def test_posts_with_mention_prefix(self):
        handler, task_run_cls = self._run(_input(), _mapping())
        handler.post_thread_message.assert_called_once_with("<@du1> Here is my analysis")
        task_run_cls.mutate_state_atomic.assert_called_once()

    def test_no_mapping_is_noop(self):
        handler, task_run_cls = self._run(_input(), mapping=None)
        handler.post_thread_message.assert_not_called()
        task_run_cls.mutate_state_atomic.assert_not_called()

    def test_duplicate_relay_skipped(self):
        handler, task_run_cls = self._run(_input(relay_id="r1"), _mapping(), state={"discord_sent_relay_ids": ["r1"]})
        handler.post_thread_message.assert_not_called()
        task_run_cls.mutate_state_atomic.assert_not_called()

    def test_long_message_chunked_mention_on_first_only(self):
        text = "para\n\n" + "x" * 2500
        handler, _ = self._run(_input(text=text), _mapping())
        calls = [c.args[0] for c in handler.post_thread_message.call_args_list]
        assert len(calls) >= 2
        assert calls[0].startswith("<@du1> ")
        assert all(not c.startswith("<@du1>") for c in calls[1:])
        assert all(len(c) <= 2000 for c in calls)
