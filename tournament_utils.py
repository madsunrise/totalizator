from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from models import Event, Group

# Очки за спецставки.
CHAMPION_BET_POINTS = 10
GROUP_CHAMPION_POINTS_PER_CORRECT = 1
ALL_GROUPS_BONUS = 10

# Минимум команд в группе, чтобы ставка на победителя группы имела смысл.
MIN_TEAMS_PER_GROUP = 2

# Маркер первой строки /setup_tournament, разрешающий перезапись уже открытой структуры.
FORCE_TOKEN = 'FORCE'


def normalize_team_name(name: str | None) -> str:
    # Каноническая форма для сравнения названий команд: trim + casefold.
    # casefold() корректнее lower() для кириллицы и не регрессирует существующее
    # сравнение через .lower() в /result.
    if name is None:
        return ''
    return name.strip().casefold()


def equals_team(a: str | None, b: str | None) -> bool:
    na = normalize_team_name(a)
    nb = normalize_team_name(b)
    if not na or not nb:
        return False
    return na == nb


def get_tournament_start(events: Iterable[Event]) -> datetime | None:
    # Старт турнира = время самого раннего матча (симметрично joker_utils.get_tournament_end).
    times = [event.get_time_in_utc() for event in events]
    if len(times) == 0:
        return None
    return min(times)


# --- Парсинг bulk-команд мейнтейнера ------------------------------------------------

def _split_body_lines(text: str, command: str) -> list[str]:
    # Убираем префикс команды (в т.ч. /command@botname) и возвращаем непустые строки тела.
    body = text.strip()
    if body.startswith(command):
        body = body[len(command):]
        # Возможный суффикс @botname сразу после команды на первой строке.
        if body.startswith('@'):
            body = body.split(None, 1)[1] if ' ' in body or '\n' in body else ''
    lines = []
    for raw_line in body.split('\n'):
        line = raw_line.strip()
        if line:
            lines.append(line)
    return lines


def has_force_token(text: str) -> bool:
    # FORCE как отдельная строка/токен тела /setup_tournament разрешает перезапись открытой структуры.
    lines = _split_body_lines(text, '/setup_tournament')
    return any(line.upper() == FORCE_TOKEN for line in lines)


def parse_structure(text: str) -> tuple[list[Group], list[str]]:
    # Разбирает тело /setup_tournament в список Group. Собирает ВСЕ ошибки, ничего не сохраняя.
    # Формат строки: "A: Канада, Мексика, США". Поддерживает первую строку-маркер FORCE
    # (она убирается вызывающим до парсинга), здесь её игнорируем, если осталась.
    lines = _split_body_lines(text, '/setup_tournament')
    lines = [line for line in lines if line.upper() != FORCE_TOKEN]

    groups: list[Group] = []
    errors: list[str] = []

    if len(lines) == 0:
        errors.append('Пустая структура: укажи хотя бы одну группу.')
        return groups, errors

    seen_group_ids: set[str] = set()
    seen_team_names: set[str] = set()

    for line in lines:
        if ':' not in line:
            errors.append(f'Строка без двоеточия: "{line}"')
            continue
        left, right = line.split(':', 1)
        group_id = left.strip()
        if not group_id:
            errors.append(f'Пустое название группы в строке: "{line}"')
            continue
        if group_id.casefold() in seen_group_ids:
            errors.append(f'Группа "{group_id}" указана дважды')
            continue
        seen_group_ids.add(group_id.casefold())

        teams = [team.strip() for team in right.split(',') if team.strip()]
        if len(teams) == 0:
            errors.append(f'В группе "{group_id}" нет команд')
            continue
        if len(teams) < MIN_TEAMS_PER_GROUP:
            errors.append(f'В группе "{group_id}" меньше {MIN_TEAMS_PER_GROUP} команд')
            continue

        for team in teams:
            normalized = team.casefold()
            if normalized in seen_team_names:
                errors.append(f'Команда "{team}" встречается несколько раз')
            else:
                seen_team_names.add(normalized)

        groups.append(Group(id=group_id, name=group_id, teams=teams))

    return groups, errors


def parse_group_winners(text, tournament) -> tuple[dict, list[str]]:
    # Разбирает тело /set_group_winners ("A: США") в {group_id -> canonical team}.
    # Валидирует: группа существует, команда из этой группы, каждая группа ровно один раз.
    lines = _split_body_lines(text, '/set_group_winners')

    winners: dict[str, str] = {}
    errors: list[str] = []

    if len(lines) == 0:
        errors.append('Не указаны победители групп.')
        return winners, errors

    seen_group_ids: set[str] = set()

    for line in lines:
        if ':' not in line:
            errors.append(f'Строка без двоеточия: "{line}"')
            continue
        left, right = line.split(':', 1)
        group_name = left.strip()
        team_name = right.strip()
        group = tournament.get_group(group_name)
        if group is None:
            errors.append(f'Нет группы "{group_name}"')
            continue
        if group.id.casefold() in seen_group_ids:
            errors.append(f'Группа "{group_name}" указана дважды')
            continue
        seen_group_ids.add(group.id.casefold())
        if not team_name:
            errors.append(f'Не указана команда для группы "{group_name}"')
            continue
        canonical = tournament.find_team_in_group(group.id, team_name)
        if canonical is None:
            errors.append(f'Команда "{team_name}" не из группы "{group_name}"')
            continue
        winners[group.id] = canonical

    # Все группы должны быть заполнены.
    missing = [group.id for group in tournament.groups if group.id not in winners]
    if missing and not errors:
        errors.append(f'Не указан победитель для групп: {", ".join(missing)}')

    return winners, errors


# --- Подсчёт очков (чистые функции) -------------------------------------------------

def calculate_champion_bet_points(pick: str | None, actual_champion: str | None) -> int:
    # +10, если прогноз совпал с фактическим чемпионом (нормализованное сравнение).
    # 0, если прогноза нет или чемпион ещё не известен.
    if not normalize_team_name(pick) or not normalize_team_name(actual_champion):
        return 0
    if equals_team(pick, actual_champion):
        return CHAMPION_BET_POINTS
    return 0


@dataclass(frozen=True)
class GroupBetResult:
    correct_count: int  # сколько групп угадано
    total_groups: int  # число групп В ТУРНИРЕ (а не сколько заполнил юзер)
    base_points: int  # = correct_count * GROUP_CHAMPION_POINTS_PER_CORRECT
    bonus_points: int  # = ALL_GROUPS_BONUS, если угаданы все, иначе 0
    total_points: int  # = base_points + bonus_points
    all_correct: bool


def calculate_group_bet_points(
        picks: dict,
        actual_winners: dict,
        total_groups: int | None = None,
) -> GroupBetResult:
    # +1 за каждую угаданную группу; бонус +10 ТОЛЬКО если correct_count == total_groups,
    # где total_groups = число групп в структуре (НЕ число заполненных юзером).
    # Группа засчитывается, только если есть и pick, и actual, и они нормализованно равны.
    if total_groups is None:
        total_groups = len(actual_winners)

    correct_count = 0
    for group_id, actual_winner in actual_winners.items():
        if not normalize_team_name(actual_winner):
            continue  # победитель группы ещё не введён — засчитать нельзя
        if equals_team(picks.get(group_id), actual_winner):
            correct_count += 1

    base_points = correct_count * GROUP_CHAMPION_POINTS_PER_CORRECT
    all_correct = total_groups > 0 and correct_count == total_groups
    bonus_points = ALL_GROUPS_BONUS if all_correct else 0
    return GroupBetResult(
        correct_count=correct_count,
        total_groups=total_groups,
        base_points=base_points,
        bonus_points=bonus_points,
        total_points=base_points + bonus_points,
        all_correct=all_correct,
    )
