from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from models import Bet, Event, EventType

TOTAL_JOKERS = 8
PLAYOFF_JOKERS = 4
REMINDER_HOURS = (48, 24, 6)


@dataclass(frozen=True)
class JokerStatus:
    used_total: int
    used_playoff: int
    used_before_playoff: int
    remaining_total: int
    remaining_playoff: int
    will_burn_at_playoff_start: int
    remaining_usable_now: int
    playoff_started: bool
    playoff_start: datetime | None
    tournament_end: datetime | None
    total_limit: int = TOTAL_JOKERS
    playoff_limit: int = PLAYOFF_JOKERS


def is_playoff_event(event: Event) -> bool:
    return event.event_type in (
        EventType.PLAY_OFF_SINGLE_MATCH,
        EventType.PLAY_OFF_SECOND_MATCH,
        EventType.PLAY_OFF_FIRST_MATCH,
    )


def get_playoff_start(events: Iterable[Event]) -> datetime | None:
    playoff_event_times = [event.get_time_in_utc() for event in events if is_playoff_event(event)]
    if len(playoff_event_times) == 0:
        return None
    return min(playoff_event_times)


def get_tournament_end(events: Iterable[Event]) -> datetime | None:
    all_event_times = [event.get_time_in_utc() for event in events]
    if len(all_event_times) == 0:
        return None
    return max(all_event_times)


def calculate_used_total(bets_with_events: Iterable[tuple[Bet, Event]]) -> int:
    return sum(1 for (bet, _) in bets_with_events if bet.is_joker)


def calculate_used_playoff(bets_with_events: Iterable[tuple[Bet, Event]]) -> int:
    return sum(1 for (bet, event) in bets_with_events if bet.is_joker and is_playoff_event(event))


def calculate_used_before_playoff(bets_with_events: Iterable[tuple[Bet, Event]]) -> int:
    return sum(1 for (bet, event) in bets_with_events if bet.is_joker and not is_playoff_event(event))


def calculate_remaining_total(used_total: int) -> int:
    return max(TOTAL_JOKERS - used_total, 0)


def calculate_remaining_playoff(used_playoff: int) -> int:
    return max(PLAYOFF_JOKERS - used_playoff, 0)


def calculate_will_burn_at_playoff_start(remaining_total: int, remaining_playoff: int) -> int:
    return max(remaining_total - remaining_playoff, 0)


def calculate_remaining_usable_now(playoff_started: bool, remaining_total: int, remaining_playoff: int) -> int:
    if playoff_started:
        return min(remaining_total, remaining_playoff)
    return remaining_total


def calculate_joker_status(
        bets_with_events: Iterable[tuple[Bet, Event]],
        events: Iterable[Event],
        now_utc: datetime | None = None,
) -> JokerStatus:
    now = now_utc or datetime.now(timezone.utc)
    normalized_bets_with_events = list(bets_with_events)
    normalized_events = list(events)
    playoff_start = get_playoff_start(normalized_events)
    tournament_end = get_tournament_end(normalized_events)
    playoff_started = playoff_start is not None and playoff_start <= now
    used_total = calculate_used_total(normalized_bets_with_events)
    used_playoff = calculate_used_playoff(normalized_bets_with_events)
    used_before_playoff = calculate_used_before_playoff(normalized_bets_with_events)
    remaining_total = calculate_remaining_total(used_total)
    remaining_playoff = calculate_remaining_playoff(used_playoff)
    will_burn_at_playoff_start = calculate_will_burn_at_playoff_start(
        remaining_total=remaining_total,
        remaining_playoff=remaining_playoff,
    )
    remaining_usable_now = calculate_remaining_usable_now(
        playoff_started=playoff_started,
        remaining_total=remaining_total,
        remaining_playoff=remaining_playoff,
    )
    return JokerStatus(
        used_total=used_total,
        used_playoff=used_playoff,
        used_before_playoff=used_before_playoff,
        remaining_total=remaining_total,
        remaining_playoff=remaining_playoff,
        will_burn_at_playoff_start=will_burn_at_playoff_start,
        remaining_usable_now=remaining_usable_now,
        playoff_started=playoff_started,
        playoff_start=playoff_start,
        tournament_end=tournament_end,
    )


def is_event_started(event: Event, now_utc: datetime | None = None) -> bool:
    now = now_utc or datetime.now(timezone.utc)
    return event.get_time_in_utc() <= now


def can_assign_joker_to_bet(
        bet: Bet,
        event: Event,
        bets_with_events: Iterable[tuple[Bet, Event]],
        events: Iterable[Event],
        now_utc: datetime | None = None,
) -> bool:
    if bet.is_joker or event.result is not None or is_event_started(event=event, now_utc=now_utc):
        return False
    status = calculate_joker_status(bets_with_events=bets_with_events, events=events, now_utc=now_utc)
    if status.remaining_usable_now <= 0:  # после старта плей-офф = min(remaining_total, remaining_playoff); защита от обхода сгорания
        return False
    if status.remaining_total <= 0:
        return False
    if is_playoff_event(event) and status.remaining_playoff <= 0:
        return False
    return True


def can_remove_joker_from_bet(bet: Bet, event: Event, now_utc: datetime | None = None) -> bool:
    return bet.is_joker and event.result is None and not is_event_started(event=event, now_utc=now_utc)


def calculate_scores_with_joker(base_scores: int, is_joker: bool) -> int:
    if is_joker:
        return base_scores * 2
    return base_scores


def get_joker_status_text(status: JokerStatus) -> str:
    lines = ['Джокеры:']
    lines.append(f'Использовано всего: {status.used_total}/{status.total_limit}')
    lines.append(f'На матчи плей-офф поставлено: {status.used_playoff}/{status.playoff_limit}')

    if status.playoff_started:
        lines.append(f'Доступно сейчас: {status.remaining_usable_now}')
        if status.will_burn_at_playoff_start > 0:
            lines.append(f'К старту плей-офф сгорело: {status.will_burn_at_playoff_start}')
    else:
        lines.append(f'Осталось всего: {status.remaining_total}')
        effective_remaining_playoff = min(status.remaining_total, status.remaining_playoff)
        lines.append(f'На матчи плей-офф ещё можно поставить: {effective_remaining_playoff}')
        if status.will_burn_at_playoff_start > 0:
            lines.append(f'К старту плей-офф сгорит: {status.will_burn_at_playoff_start}')
            lines.append(
                f'Потрать ещё {status.will_burn_at_playoff_start} джокер(а/ов) на матчи группового этапа, чтобы ничего не сгорело.')
        else:
            lines.append('К старту плей-офф ничего не сгорит.')
        if status.playoff_start is None:
            lines.append('Матчи плей-офф ещё не добавлены.')

    return '\n'.join(lines)
