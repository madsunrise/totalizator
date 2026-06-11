# Клиент football-data.org v4 для авто-завершения матчей ЧМ-2026.
# Сетевая здесь только fetch_matches; парсинг, сопоставление с нашими Event
# и построение EventResult — чистые функции, тестируемые без сети и БД.
import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone

import requests

import team_names
from models import Event, EventResult, EventType

MATCHES_URL = 'https://api.football-data.org/v4/competitions/WC/matches'
REQUEST_TIMEOUT_SECONDS = 15
# Статусы, при которых результат окончательный. AWARDED — техническое решение без игры.
FINAL_STATUSES = ('FINISHED', 'AWARDED')
# Насколько kickoff из API может отличаться от времени нашего события.
# Дизамбигуирует возможный повтор пары команд (группа vs плей-офф) — те разнесены на дни.
KICKOFF_TOLERANCE = timedelta(minutes=15)
# Серии пенальти и доп. время: по правилам тотализатора записываем счёт основного времени.
EXTENDED_DURATIONS = ('EXTRA_TIME', 'PENALTY_SHOOTOUT')


@dataclass(frozen=True)
class ApiMatch:
    utc_date: datetime  # aware UTC
    status: str
    home_team: str  # отображаемое имя для логов
    away_team: str
    home_team_keys: frozenset  # нормализованные {name, shortName, tla}
    away_team_keys: frozenset
    winner: str | None  # HOME_TEAM | AWAY_TEAM | DRAW | None
    duration: str | None  # REGULAR | EXTRA_TIME | PENALTY_SHOOTOUT | None
    full_time: tuple | None  # (home, away)
    regular_time: tuple | None  # счёт за 90 минут; в API присутствует только при доп. времени


def _parse_score_pair(score_dict) -> tuple | None:
    # В актуальном v4 ключи 'home'/'away', но в части документации/старых ответов
    # встречаются 'homeTeam'/'awayTeam' — принимаем оба стиля.
    if not isinstance(score_dict, dict):
        return None
    home = score_dict.get('home', score_dict.get('homeTeam'))
    away = score_dict.get('away', score_dict.get('awayTeam'))
    if not isinstance(home, int) or not isinstance(away, int):
        return None
    if home < 0 or away < 0:
        return None
    return home, away


def _parse_team_keys(team_dict) -> frozenset:
    keys = set()
    for field in ('name', 'shortName', 'tla'):
        value = team_dict.get(field)
        if isinstance(value, str) and value.strip():
            keys.add(team_names.normalize(value))
    return frozenset(keys)


def _parse_match(match_dict: dict) -> ApiMatch | None:
    utc_date_raw = match_dict.get('utcDate')
    status = match_dict.get('status')
    home_team = match_dict.get('homeTeam') or {}
    away_team = match_dict.get('awayTeam') or {}
    if not isinstance(utc_date_raw, str) or not isinstance(status, str):
        return None
    try:
        utc_date = datetime.fromisoformat(utc_date_raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if utc_date.tzinfo is None:
        utc_date = utc_date.replace(tzinfo=timezone.utc)
    home_keys = _parse_team_keys(home_team)
    away_keys = _parse_team_keys(away_team)
    if not home_keys or not away_keys:
        return None
    score = match_dict.get('score') or {}
    return ApiMatch(
        utc_date=utc_date,
        status=status,
        home_team=home_team.get('name') or '?',
        away_team=away_team.get('name') or '?',
        home_team_keys=home_keys,
        away_team_keys=away_keys,
        winner=score.get('winner'),
        duration=score.get('duration'),
        full_time=_parse_score_pair(score.get('fullTime')),
        regular_time=_parse_score_pair(score.get('regularTime')),
    )


def parse_matches_response(payload: dict) -> list:
    matches = []
    for match_dict in payload.get('matches') or []:
        parsed = _parse_match(match_dict)
        if parsed is not None:
            matches.append(parsed)
    return matches


def fetch_matches(token: str, date_from: date, date_to: date) -> list | None:
    # None — «не удалось получить данные» (сеть/лимит/не-200): вызывающий просто ждёт следующего тика.
    try:
        response = requests.get(
            MATCHES_URL,
            headers={'X-Auth-Token': token},
            params={'dateFrom': date_from.isoformat(), 'dateTo': date_to.isoformat()},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logging.warning(f'football-data.org request failed: {e}')
        return None
    # API просит следить за этими заголовками. При нашем темпе (1 запрос в 10 минут
    # против лимита 10/мин) упереться в лимит нельзя, но мониторим как просят.
    requests_available = response.headers.get('X-RequestsAvailable')
    if requests_available is not None and requests_available.isdigit() and int(requests_available) < 3:
        logging.warning(f'football-data.org: only {requests_available} requests available '
                        f'(кто-то ещё использует этот токен?)')
    if response.status_code == 429:
        reset_seconds = response.headers.get('X-RequestCounter-Reset')
        logging.warning(f'football-data.org rate limit hit, resets in {reset_seconds} seconds')
        return None
    if response.status_code != 200:
        logging.warning(f'football-data.org returned HTTP {response.status_code}: {response.text[:200]}')
        return None
    try:
        payload = response.json()
    except ValueError as e:
        logging.warning(f'football-data.org returned non-JSON response: {e}')
        return None
    return parse_matches_response(payload)


def find_api_match_for_event(event: Event, api_matches: list,
                             tolerance: timedelta = KICKOFF_TOLERANCE) -> tuple:
    # Возвращает (ApiMatch | None, reason). Сопоставление: пара команд без учёта порядка
    # + kickoff в пределах допуска. Пара дизамбигуирует одновременные матчи 3-го тура,
    # время — гипотетический повтор пары в плей-офф.
    team_1_keys = team_names.get_api_keys(event.team_1)
    team_2_keys = team_names.get_api_keys(event.team_2)
    if team_1_keys is None:
        return None, f'unmapped_team:{event.team_1}'
    if team_2_keys is None:
        return None, f'unmapped_team:{event.team_2}'
    event_time = event.get_time_in_utc()
    candidates = []
    for api_match in api_matches:
        straight = (team_1_keys & api_match.home_team_keys) and (team_2_keys & api_match.away_team_keys)
        swapped = (team_1_keys & api_match.away_team_keys) and (team_2_keys & api_match.home_team_keys)
        if not straight and not swapped:
            continue
        if abs(api_match.utc_date - event_time) > tolerance:
            continue
        candidates.append(api_match)
    if len(candidates) == 0:
        return None, 'not_found'
    if len(candidates) > 1:
        return None, 'ambiguous'
    return candidates[0], ''


def build_event_result(event: Event, api_match: ApiMatch) -> tuple:
    # Возвращает (EventResult | None, reason). Никогда не строит результат
    # по неполным/противоречивым данным — лучше ручной /result, чем неверное начисление очков.
    if api_match.status not in FINAL_STATUSES:
        return None, 'not_finished'
    if event.event_type == EventType.PLAY_OFF_SECOND_MATCH:
        # Победитель пары определяется по сумме двух матчей — из одного матча API его не вывести.
        return None, 'second_leg_manual_only'

    team_1_keys = team_names.get_api_keys(event.team_1)
    team_2_keys = team_names.get_api_keys(event.team_2)
    if team_1_keys is None or team_2_keys is None:
        return None, 'unmapped_team'
    team_1_is_home = bool(team_1_keys & api_match.home_team_keys)
    team_2_is_away = bool(team_2_keys & api_match.away_team_keys)
    if team_1_is_home != team_2_is_away:
        return None, 'orientation_mismatch'
    if not team_1_is_home and not (team_1_keys & api_match.away_team_keys):
        return None, 'orientation_mismatch'

    if api_match.duration in EXTENDED_DURATIONS:
        score_pair = api_match.regular_time
        if score_pair is None:
            return None, 'no_regular_time'
        if score_pair[0] != score_pair[1]:
            # Доп. время бывает только после ничьей в основное время — данные противоречивы.
            return None, 'inconsistent_regular_time'
    else:
        score_pair = api_match.full_time
        if score_pair is None:
            return None, 'no_full_time'

    home_scores, away_scores = score_pair
    if team_1_is_home:
        team_1_scores, team_2_scores = home_scores, away_scores
    else:
        team_1_scores, team_2_scores = away_scores, home_scores

    if event.event_type in (EventType.GROUP_STAGE, EventType.PLAY_OFF_FIRST_MATCH):
        team_1_has_gone_through = None
    else:  # PLAY_OFF_SINGLE_MATCH
        if api_match.winner == 'HOME_TEAM':
            team_1_has_gone_through = team_1_is_home
        elif api_match.winner == 'AWAY_TEAM':
            team_1_has_gone_through = not team_1_is_home
        elif team_1_scores != team_2_scores:
            # winner не пришёл, но счёт основного времени не ничейный — победитель очевиден.
            team_1_has_gone_through = team_1_scores > team_2_scores
        else:
            # Плей-офф без победителя не завершаем.
            return None, 'no_winner'

    result = EventResult(
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores,
        team_1_has_gone_through=team_1_has_gone_through,
    )
    return result, ''
