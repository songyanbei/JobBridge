"""Single conversion entry point for recommendation time semantics (§9.12).

Rules this module encodes so no caller has to remember them:

* every persisted timestamp column means UTC;
* MySQL ``DATETIME`` carries no offset, so values are stored as **naive UTC** and
  re-tagged as UTC the moment they are read back;
* the *business day* (``stat_date``, ``rotation_date``, daily token budgets) uses
  ``settings.scheduler_timezone``, which v1 requires to be ``Asia/Shanghai``;
* "last 7 days of exposure" is a rolling 168-hour UTC window and deliberately
  does **not** go through the business-day helpers.

Recommendation code must not call ``date.today()`` or a naive ``datetime.now()``
directly — both silently pick up the host timezone.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

try:  # pragma: no cover - exercised implicitly by the business-day helpers
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]

# China has observed a fixed UTC+8 with no DST since 1991, so this stays correct
# for every timestamp the product can produce even when the IANA database is not
# installed (a bare Windows/Alpine runtime without `tzdata`).
_FALLBACK_BUSINESS_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

EXPOSURE_WINDOW_HOURS = 168


def business_timezone() -> timezone:
    """Resolve ``settings.scheduler_timezone`` with a safe UTC+8 fallback."""
    name = "Asia/Shanghai"
    try:
        from app.config import settings

        name = getattr(settings, "scheduler_timezone", None) or name
    except Exception:  # pragma: no cover - config import must never break ranking
        pass
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)  # type: ignore[return-value]
        except Exception:
            pass
    return _FALLBACK_BUSINESS_TZ


def utc_now() -> datetime:
    """Timezone-aware current UTC instant."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Tag a value read from the database as UTC.

    Naive values are assumed to already be UTC because that is the only thing
    this codebase writes; aware values are converted.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Convert to the naive-UTC form MySQL ``DATETIME`` columns expect."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def business_date(value: datetime | None = None) -> date:
    """Business-day date of an instant, e.g. the ``stat_date`` of an impression."""
    moment = ensure_utc(value) or utc_now()
    return moment.astimezone(business_timezone()).date()


def rotation_date(value: datetime | None = None) -> str:
    """``rotation_date`` for the stable tie-break and exploration bucket (§6.9.5).

    Using UTC here would rotate everyone's ordering at 08:00 Beijing time instead
    of at local midnight.
    """
    return business_date(value).isoformat()


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` UTC instants covering one business day."""
    tz = business_timezone()
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def exposure_window_start(now: datetime | None = None, hours: int = EXPOSURE_WINDOW_HOURS) -> datetime:
    """Start of the rolling exposure window — UTC hours, never a calendar day."""
    return (ensure_utc(now) or utc_now()) - timedelta(hours=hours)
