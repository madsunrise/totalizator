import pytz
from dataclasses import dataclass
from datetime import datetime

import datetime_utils
from models import EventType

# Формат даты/времени матча (в московском часовом поясе), как в /add_event.
DATE_FORMAT = '%d.%m.%Y %H:%M'
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Токены типа события в теле /add_event -> EventType.
EVENT_TYPE_BY_TOKEN = {
    'group': EventType.GROUP_STAGE,
    'playoff_single': EventType.PLAY_OFF_SINGLE_MATCH,
    'playoff_first_match': EventType.PLAY_OFF_FIRST_MATCH,
    'playoff_second_match': EventType.PLAY_OFF_SECOND_MATCH,
}


@dataclass(frozen=True)
class ParsedEvent:
    team_1: str
    team_2: str
    time_utc: datetime  # aware UTC; та же конвенция, что у одиночного /add_event
    event_type: EventType


def _split_body_lines(text: str, command: str) -> list[str]:
    # Срезаем префикс команды (в т.ч. /command@botname) и возвращаем непустые строки тела.
    # Зеркало tournament_utils._split_body_lines, чтобы многострочные команды вели себя одинаково.
    body = text.strip()
    if body.startswith(command):
        body = body[len(command):]
        if body.startswith('@'):
            body = body.split(None, 1)[1] if (' ' in body or '\n' in body or '\t' in body) else ''
    return [line.strip() for line in body.split('\n') if line.strip()]


def parse_event_line(line: str) -> tuple[ParsedEvent | None, str | None]:
    # Разбирает одну строку "Команда1; Команда2; ДД.ММ.ГГГГ ЧЧ:ММ; group".
    # Возвращает (ParsedEvent, None) либо (None, текст ошибки).
    parts = [part.strip() for part in line.split(';')]
    if len(parts) != 4:
        return None, f'нужно 4 поля через ";": "{line}"'
    team_1, team_2, time_str, type_token = parts
    if not team_1 or not team_2:
        return None, f'пустое название команды: "{line}"'
    if team_1.casefold() == team_2.casefold():
        return None, f'команда играет сама с собой: "{line}"'
    try:
        naive_moscow = datetime.strptime(time_str, DATE_FORMAT)
    except ValueError:
        return None, f'не разобрать дату/время (нужен формат ДД.ММ.ГГГГ ЧЧ:ММ): "{line}"'
    event_type = EVENT_TYPE_BY_TOKEN.get(type_token)
    if event_type is None:
        valid = ', '.join(EVENT_TYPE_BY_TOKEN)
        return None, f'неизвестный тип "{type_token}" (допустимо: {valid}): "{line}"'
    time_utc = datetime_utils.with_zone_same_instant(
        datetime_obj=naive_moscow, timezone_from=MOSCOW_TZ, timezone_to=pytz.utc)
    return ParsedEvent(team_1=team_1, team_2=team_2, time_utc=time_utc, event_type=event_type), None


def parse_events_block(text: str, command: str = '/add_event') -> tuple[list[ParsedEvent], list[str]]:
    # Разбирает тело /add_event (по матчу на строку) в список ParsedEvent.
    # Контракт all-or-nothing: при ЛЮБОЙ ошибке возвращаем ([], errors) (но собираем ВСЕ ошибки,
    # чтобы показать их разом). Дубль в пределах одного сообщения тоже считается ошибкой.
    # Ключ дубля — точное совпадение (team_1, team_2, time), как в database.find_event.
    lines = _split_body_lines(text, command)
    if not lines:
        return [], ['Пустой ввод: укажи хотя бы один матч.']
    events: list[ParsedEvent] = []
    errors: list[str] = []
    seen: set = set()
    for line in lines:
        parsed, error = parse_event_line(line)
        if error is not None:
            errors.append(error)
            continue
        key = (parsed.team_1, parsed.team_2, parsed.time_utc)
        if key in seen:
            errors.append(f'дубль в сообщении: "{line}"')
            continue
        seen.add(key)
        events.append(parsed)
    if errors:
        return [], errors
    return events, errors
