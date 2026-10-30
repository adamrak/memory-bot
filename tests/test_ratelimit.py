from datetime import UTC, datetime

import pytest
from google.genai import errors, types

from memorybot import gemini
from memorybot.ratelimit import RateLimit, daily_reset, format_rate_limit, parse_rate_limit

NOW = datetime(2026, 10, 30, 21, 0, 0, tzinfo=UTC)  # 14:00 in Los Angeles (PDT)


def error_429(quota_id: str | None, delay: str | None) -> dict:
    details = []
    if quota_id:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaMetric": "generativelanguage.googleapis.com/x", "quotaId": quota_id}]})
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return {"error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED",
                      "details": details}}


def test_per_minute_limit_uses_retry_delay():
    limit = parse_rate_limit(error_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "34s"), NOW)
    assert limit == RateLimit(datetime(2026, 10, 30, 21, 0, 34, tzinfo=UTC), False,
                              "GenerateRequestsPerMinutePerProjectPerModel-FreeTier")


def test_fractional_delay():
    limit = parse_rate_limit(error_429(None, "12.5s"), NOW)
    assert limit.retry_at == datetime(2026, 10, 30, 21, 0, 12, 500000, tzinfo=UTC)


def test_daily_limit_resets_at_pacific_midnight():
    limit = parse_rate_limit(error_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "40s"), NOW)
    assert limit.daily
    assert limit.retry_at == datetime(2026, 10, 31, 7, 0, tzinfo=UTC)  # 00:00 PDT = 07:00 UTC


def test_daily_reset_handles_winter_time():
    assert daily_reset(datetime(2026, 12, 1, 12, 0, tzinfo=UTC)) == datetime(2026, 12, 2, 8, 0, tzinfo=UTC)  # PST


@pytest.mark.parametrize("details", [None, {}, [], {"error": {"code": 429}}, error_429(None, "soon")])
def test_unknown_payloads_fall_back_gracefully(details):
    limit = parse_rate_limit(details, NOW)
    assert limit.retry_at is None and not limit.daily
    assert format_rate_limit(limit) == "⏳ Gemini rate limit, skús o chvíľu."


def test_format_uses_discord_timestamps():
    ts = int(datetime(2026, 10, 30, 21, 0, 34, tzinfo=UTC).timestamp())
    assert format_rate_limit(RateLimit(datetime.fromtimestamp(ts, UTC), False, None)) == \
        f"⏳ Gemini rate limit, skús znova <t:{ts}:R> (<t:{ts}:T>)."
    assert "Denný limit" in format_rate_limit(RateLimit(datetime.fromtimestamp(ts, UTC), True, "x"))


def test_parses_real_client_error():
    e = errors.ClientError(429, error_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "7s"))
    assert parse_rate_limit(e.details, NOW).retry_at == datetime(2026, 10, 30, 21, 0, 7, tzinfo=UTC)


# --- model fallback on 429 ---
class FakeModels:
    def __init__(self, outcomes: dict):
        self.outcomes, self.calls = outcomes, []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.outcomes[model]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_gemini(monkeypatch):
    def install(models: list[str], outcomes: dict) -> FakeModels:
        fake = FakeModels(outcomes)
        client = type("C", (), {"aio": type("A", (), {"models": fake})()})()
        monkeypatch.setattr(gemini, "GEMINI_MODELS", models)
        monkeypatch.setattr(gemini, "client", lambda: client)
        return fake
    return install


async def test_rate_limited_model_falls_back_to_next(fake_gemini):
    ok = types.GenerateContentResponse()
    fake = fake_gemini(["a", "b"], {"a": errors.ClientError(429, error_429(None, "5s")), "b": ok})
    assert await gemini.generate([], gemini.make_config()) is ok
    assert fake.calls == ["a", "b"]


async def test_all_models_rate_limited_raises_last_429(fake_gemini):
    fake_gemini(["a", "b"], {"a": errors.ClientError(429, error_429(None, "5s")),
                             "b": errors.ClientError(429, error_429(None, "9s"))})
    with pytest.raises(errors.ClientError) as exc:
        await gemini.generate([], gemini.make_config())
    assert parse_rate_limit(exc.value.details, NOW).retry_at == datetime(2026, 10, 30, 21, 0, 9, tzinfo=UTC)


async def test_bad_request_does_not_try_other_models(fake_gemini):
    fake = fake_gemini(["a", "b"], {"a": errors.ClientError(400, {"error": {"code": 400, "message": "bad"}}),
                                    "b": types.GenerateContentResponse()})
    with pytest.raises(errors.ClientError):
        await gemini.generate([], gemini.make_config())
    assert fake.calls == ["a"]
