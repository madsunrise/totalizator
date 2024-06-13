from datetime import datetime


def with_zone_same_instant(datetime_obj: datetime, timezone_from, timezone_to) -> datetime:
    localized_datetime = timezone_from.localize(datetime_obj)
    return localized_datetime.astimezone(timezone_to)
