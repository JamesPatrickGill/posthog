from products.discord_app.backend.conversation_context import (
    build_followup_with_context,
    build_prompt_with_context,
    render_conversation_context,
    render_reply_target,
)


def _msg(content, *, global_name=None, username="alice", bot=False):
    return {
        "id": "m1",
        "author": {"id": "u1", "username": username, "global_name": global_name, "bot": bot},
        "content": content,
        "timestamp": "2026-06-15T00:00:00Z",
        "reply_to_id": None,
    }


class TestRenderConversationContext:
    def test_renders_entries_oldest_first_with_at_label(self):
        block = render_conversation_context(
            [_msg("the button is broken"), _msg("yeah I see it too", global_name="Bob")]
        )
        assert "@alice: the button is broken" in block
        assert "@Bob: yeah I see it too" in block
        assert block.index("@alice") < block.index("@Bob")
        assert "<discord_conversation>" in block and "</discord_conversation>" in block

    def test_prefers_global_name_then_username_then_fallback(self):
        assert "@Bob:" in render_conversation_context([_msg("x", global_name="Bob")])
        assert "@alice:" in render_conversation_context([_msg("x", global_name=None, username="alice")])
        assert "@someone:" in render_conversation_context(
            [{"author": {"username": "", "global_name": None}, "content": "x"}]
        )

    def test_missing_empty_or_non_list_yields_empty_string(self):
        assert render_conversation_context(None) == ""
        assert render_conversation_context([]) == ""
        assert render_conversation_context("nope") == ""
        # entries with no usable text drop out entirely
        assert render_conversation_context([{"author": {"username": "a"}, "content": "   "}]) == ""

    def test_skips_unusable_entries_but_keeps_the_rest(self):
        block = render_conversation_context([_msg("kept"), "garbage", {"content": ""}, 42])
        assert "@alice: kept" in block


class TestRenderReplyTarget:
    def test_renders_single_target(self):
        out = render_reply_target(_msg("the original message", global_name="Carol"))
        assert "@Carol: the original message" in out
        assert "<discord_reply_target>" in out

    def test_absent_or_null_yields_empty_string(self):
        assert render_reply_target(None) == ""
        assert render_reply_target({}) == ""
        assert render_reply_target({"content": "  "}) == ""


class TestBuildPromptWithContext:
    def test_prompt_alone_when_no_context(self):
        assert build_prompt_with_context("review this", None) == "review this"

    def test_context_precedes_prompt(self):
        out = build_prompt_with_context("review this", [_msg("here is the PR link")])
        assert out.endswith("review this")
        assert out.index("here is the PR link") < out.index("review this")


class TestBuildFollowupWithContext:
    def test_text_alone_when_nothing_extra(self):
        assert build_followup_with_context("ship it", None, None) == "ship it"

    def test_history_then_reply_target_then_new_text(self):
        out = build_followup_with_context(
            "do that",
            [_msg("earlier chatter")],
            _msg("the thing being replied to", global_name="Dan"),
        )
        assert out.index("earlier chatter") < out.index("the thing being replied to") < out.index("do that")
        assert out.endswith("do that")

    def test_reply_target_without_history(self):
        out = build_followup_with_context("yes", None, _msg("replied message"))
        assert "replied message" in out
        assert "<discord_conversation>" not in out
        assert out.endswith("yes")
