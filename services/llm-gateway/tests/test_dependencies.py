import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import Headers

from llm_gateway.auth.models import AuthenticatedUser
from llm_gateway.config import get_settings
from llm_gateway.dependencies import (
    _check_premium_model_gate,
    _extract_end_user_id_from_body,
    enforce_throttles,
    get_provider_from_request,
    resolve_plan_and_quota,
)
from llm_gateway.rate_limiting.throttles import ThrottleContext, ThrottleResult
from llm_gateway.services.plan_resolver import PlanInfo
from llm_gateway.services.quota_resolver import QuotaResourceStatus


def _make_request(body: dict | None = None, headers: dict[str, str] | None = None) -> Request:
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    del request.state._cached_body
    request.headers = Headers(headers or {})

    if body is not None:
        raw = json.dumps(body).encode()
    else:
        raw = None

    async def fake_body():
        return raw

    request.body = fake_body
    return request


def _make_user(auth_method: str = "personal_api_key", user_id: int = 1) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        team_id=1,
        auth_method=auth_method,
        distinct_id=f"test-distinct-id-{user_id}",
        scopes=["llm_gateway:read"],
    )


class TestExtractEndUserIdFromBody:
    @pytest.mark.asyncio
    async def test_returns_openai_user_field(self) -> None:
        request = _make_request({"model": "gpt-4o", "messages": [], "user": "user-123"})
        assert await _extract_end_user_id_from_body(request) == "user-123"

    @pytest.mark.asyncio
    async def test_returns_anthropic_metadata_user_id(self) -> None:
        request = _make_request({"model": "claude-3", "messages": [], "metadata": {"user_id": "user-456"}})
        assert await _extract_end_user_id_from_body(request) == "user-456"

    @pytest.mark.asyncio
    async def test_openai_user_takes_precedence_over_metadata(self) -> None:
        request = _make_request({"model": "gpt-4o", "user": "openai-user", "metadata": {"user_id": "anthro-user"}})
        assert await _extract_end_user_id_from_body(request) == "openai-user"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_user_provided(self) -> None:
        request = _make_request({"model": "gpt-4o", "messages": []})
        assert await _extract_end_user_id_from_body(request) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_body(self) -> None:
        request = _make_request()
        assert await _extract_end_user_id_from_body(request) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_string_user(self) -> None:
        request = _make_request({"model": "gpt-4o", "user": 123})
        assert await _extract_end_user_id_from_body(request) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_string_user(self) -> None:
        request = _make_request({"model": "gpt-4o", "user": ""})
        assert await _extract_end_user_id_from_body(request) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_metadata(self) -> None:
        request = _make_request({"model": "gpt-4o", "metadata": {}})
        assert await _extract_end_user_id_from_body(request) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_dict_metadata(self) -> None:
        request = _make_request({"model": "gpt-4o", "metadata": "not-a-dict"})
        assert await _extract_end_user_id_from_body(request) is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_dict_json_body(self) -> None:
        request = MagicMock(spec=Request)
        request.state = MagicMock()
        del request.state._cached_body

        async def fake_body():
            return b'["not", "a", "dict"]'

        request.body = fake_body
        assert await _extract_end_user_id_from_body(request) is None


class TestGetProviderFromRequest:
    @pytest.mark.asyncio
    async def test_returns_provider_from_header(self) -> None:
        request = _make_request(
            {"model": "claude-sonnet-4-6", "provider": "anthropic"},
            headers={"X-PostHog-Provider": "bedrock"},
        )

        assert await get_provider_from_request(request) == "bedrock"

    @pytest.mark.asyncio
    async def test_invalid_provider_header_raises_http_400(self) -> None:
        request = _make_request(headers={"X-PostHog-Provider": "vertex"})

        with pytest.raises(HTTPException) as exc_info:
            await get_provider_from_request(request)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"]["type"] == "invalid_request_error"


class TestEnforceThrottles:
    async def _run_enforce_throttles(
        self,
        body: dict | None = None,
        auth_method: str = "personal_api_key",
        user_id: int = 1,
    ) -> ThrottleContext:
        request = _make_request(body)
        request.url = MagicMock()
        request.url.path = "/openai/v1/chat/completions"
        user = _make_user(auth_method=auth_method, user_id=user_id)

        captured_context: ThrottleContext | None = None

        async def capture_check(context: ThrottleContext) -> ThrottleResult:
            nonlocal captured_context
            captured_context = context
            return ThrottleResult.allow()

        runner = MagicMock()
        runner.check = capture_check

        with patch("llm_gateway.dependencies.ensure_costs_fresh"):
            await enforce_throttles(request=request, user=user, runner=runner)

        assert captured_context is not None
        return captured_context

    @pytest.mark.asyncio
    async def test_api_key_without_user_sets_end_user_id_none(self) -> None:
        context = await self._run_enforce_throttles(
            body={"model": "gpt-4o", "messages": []},
            auth_method="personal_api_key",
        )
        assert context.end_user_id is None

    @pytest.mark.asyncio
    async def test_api_key_with_openai_user_sets_end_user_id(self) -> None:
        context = await self._run_enforce_throttles(
            body={"model": "gpt-4o", "messages": [], "user": "user-abc"},
            auth_method="personal_api_key",
        )
        assert context.end_user_id == "user-abc"

    @pytest.mark.asyncio
    async def test_api_key_with_anthropic_metadata_user_id_sets_end_user_id(self) -> None:
        context = await self._run_enforce_throttles(
            body={"model": "claude-3", "messages": [], "metadata": {"user_id": "user-xyz"}},
            auth_method="personal_api_key",
        )
        assert context.end_user_id == "user-xyz"

    @pytest.mark.asyncio
    async def test_oauth_always_sets_end_user_id_to_user_id(self) -> None:
        context = await self._run_enforce_throttles(
            body={"model": "gpt-4o", "messages": []},
            auth_method="oauth_access_token",
            user_id=42,
        )
        assert context.end_user_id == "42"

    @pytest.mark.asyncio
    async def test_oauth_ignores_body_user_field(self) -> None:
        context = await self._run_enforce_throttles(
            body={"model": "gpt-4o", "messages": [], "user": "body-user"},
            auth_method="oauth_access_token",
            user_id=42,
        )
        assert context.end_user_id == "42"

    @pytest.mark.asyncio
    async def test_api_key_no_body_sets_end_user_id_none(self) -> None:
        context = await self._run_enforce_throttles(
            body=None,
            auth_method="personal_api_key",
        )
        assert context.end_user_id is None


class TestResolvePlanAndQuota:
    """The quota resolver roundtrip runs for bucket-billed products (against the
    product's own bucket) and is skipped entirely for unbilled ones."""

    async def _run(self, product: str) -> tuple:
        plan_info = PlanInfo(plan_key="pro", seat_created_at=None)
        plan_mock = AsyncMock(return_value=plan_info)
        quota_mock = AsyncMock(return_value=QuotaResourceStatus(limited=True))
        with (
            patch("llm_gateway.dependencies.resolve_plan_info", plan_mock),
            patch("llm_gateway.dependencies.resolve_quota_status", quota_mock),
        ):
            result = await resolve_plan_and_quota(_make_request(), user_id=1, team_id=42, product=product)
        return result, quota_mock

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("product", "expected_resource"),
        [("slack_app", "ai_credits"), ("posthog_code", "posthog_code_credits")],
    )
    async def test_bucket_billed_product_resolves_its_own_bucket(self, product: str, expected_resource: str) -> None:
        (_, quota_status), quota_mock = await self._run(product)

        quota_mock.assert_awaited_once()
        assert quota_mock.call_args.args[2] == expected_resource
        assert quota_status.limited is True

    @pytest.mark.asyncio
    async def test_unbilled_product_skips_quota_resolver(self) -> None:
        # wizard is unbilled — it shouldn't pay for the quota resolver roundtrip.
        (_, quota_status), quota_mock = await self._run("wizard")

        quota_mock.assert_not_awaited()
        assert quota_status.limited is False


class TestCheckPremiumModelGate:
    """Completion-path enforcement of the premium-model plan gate.

    See dependencies._check_premium_model_gate: gated only for products that
    opt in (posthog_code), only for models in settings.premium_models, only
    when the PostHog flag is enabled, and denies (rather than fails open) when
    the plan is missing/unknown.
    """

    @pytest.mark.asyncio
    async def test_gate_off_allows_premium_model_on_any_plan(self) -> None:
        with patch.object(get_settings(), "premium_model_gate_enabled", False):
            await _check_premium_model_gate("posthog_code", "claude-fable-5", None)
            await _check_premium_model_gate("posthog_code", "claude-fable-5", "posthog-code-free-20260301")
            await _check_premium_model_gate("posthog_code", "claude-fable-5", "posthog-code-pro-200-20260301")
        # No HTTPException raised for any plan state — reaching here is the assertion.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "plan_key",
        [None, "posthog-code-free-20260301", "posthog-code-pro-200-20260301"],
        ids=["none", "free", "pro"],
    )
    async def test_gate_on_denies_premium_model_for_non_usage_based_plan(self, plan_key: str | None) -> None:
        with patch.object(get_settings(), "premium_model_gate_enabled", True):
            with pytest.raises(HTTPException) as exc_info:
                await _check_premium_model_gate("posthog_code", "claude-fable-5", plan_key)

        assert exc_info.value.status_code == 403
        assert "claude-fable-5" in exc_info.value.detail
        assert "usage-based" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_gate_on_allows_premium_model_for_usage_based_plan(self) -> None:
        with patch.object(get_settings(), "premium_model_gate_enabled", True):
            await _check_premium_model_gate("posthog_code", "claude-fable-5", "posthog-code-usage-20260709")

    @pytest.mark.asyncio
    async def test_gate_on_allows_non_premium_model_on_any_plan(self) -> None:
        with patch.object(get_settings(), "premium_model_gate_enabled", True):
            await _check_premium_model_gate("posthog_code", "claude-sonnet-4-6", None)

    @pytest.mark.asyncio
    async def test_gate_on_ignores_products_that_do_not_opt_in(self) -> None:
        # posthog_ai never sets premium_models_gated, so a fable-ish model id is unaffected
        # regardless of plan or flag state.
        with patch.object(get_settings(), "premium_model_gate_enabled", True):
            await _check_premium_model_gate("posthog_ai", "claude-fable-5", None)

    @pytest.mark.asyncio
    async def test_no_model_in_request_is_allowed(self) -> None:
        with patch.object(get_settings(), "premium_model_gate_enabled", True):
            await _check_premium_model_gate("posthog_code", None, None)
