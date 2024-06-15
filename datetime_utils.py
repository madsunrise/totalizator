import zoneinfo
from datetime import datetime, timezone


def with_zone_same_instant(datetime_obj: datetime, timezone_from, timezone_to) -> datetime:
    localized_datetime = timezone_from.localize(datetime_obj)
    return localized_datetime.astimezone(timezone_to)


def to_display_string(datetime_obj: datetime) -> str:
    return datetime_obj.strftime('%d %b %H:%M')


def get_utc_time() -> datetime:
    return datetime.now(timezone.utc)


def get_moscow_time() -> datetime:
    return datetime.now(zoneinfo.ZoneInfo("Europe/Moscow"))
