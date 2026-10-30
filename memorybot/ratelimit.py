"""Understanding Gemini 429 errors: when can we try again?

A 429 body looks like {"error": {"code": 429, "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", ...}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}}
Per-day quotas reset at midnight Pacific time.
"""
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass
class RateLimit:
    retry_at: datetime | None  # UTC; None when Gemini didn't say
    daily: bool  # the per-day quota is exhausted
    quota_id: str | None


def daily_reset(now: datetime) -> datetime:
    """Next midnight Pacific time, in UTC."""
    local = now.astimezone(PACIFIC)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), PACIFIC)
    return midnight.astimezone(UTC)


def _seconds(value) -> float | None:
    m = re.fullmatch(r"\s*([\d.]+)s\s*", str(value or ""))
    return float(m.group(1)) if m else None


def parse_rate_limit(details, now: datetime | None = None) -> RateLimit:
    now = now or datetime.now(UTC)
    error = details.get("error", details) if isinstance(details, dict) else {}
    delay, quota_id = None, None
    for d in error.get("details") or []:
        kind = d.get("@type", "")
        if kind.endswith("RetryInfo"):
            delay = _seconds(d.get("retryDelay"))
        elif kind.endswith("QuotaFailure"):
            quota_id = next((v["quotaId"] for v in d.get("violations", []) if v.get("quotaId")), quota_id)
    daily = bool(quota_id and "PerDay" in quota_id)
    if daily:
        retry_at = daily_reset(now)
    elif delay is not None:
        retry_at = now + timedelta(seconds=delay)
    else:
        retry_at = None
    return RateLimit(retry_at, daily, quota_id)


def format_rate_limit(limit: RateLimit) -> str:
    """Discord message; <t:…> timestamps render in the reader's timezone and as a live countdown."""
    if limit.retry_at is None:
        return "⏳ Gemini rate limit, skús o chvíľu."
    ts = int(limit.retry_at.timestamp())
    if limit.daily:
        return f"⏳ Denný limit Gemini je vyčerpaný. Obnoví sa <t:{ts}:R> (<t:{ts}:f>)."
    return f"⏳ Gemini rate limit, skús znova <t:{ts}:R> (<t:{ts}:T>)."
