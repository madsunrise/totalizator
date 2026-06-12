import pytz
from datetime import datetime, timezone
from enum import Enum

import datetime_utils


class EventResult:
    def __init__(self, team_1_scores: int, team_2_scores: int, team_1_has_gone_through: bool | None):
        self.team_1_scores = team_1_scores
        self.team_2_scores = team_2_scores
        self.team_1_has_gone_through = team_1_has_gone_through
        # Для GROUP_STAGE и PLAY_OFF_FIRST_MATCH указываем None (исход по итогу самого матча не определяется).
        # В матчах PLAY_OFF_SINGLE_MATCH и PLAY_OFF_SECOND_MATCH обязательно будет или True, или False.

    def is_draw(self) -> bool:
        return self.team_1_scores == self.team_2_scores


class EventType(Enum):
    GROUP_STAGE = 1  # Матчи группового этапа.
    PLAY_OFF_SINGLE_MATCH = 2  # Единственный матч в плей-офф (когда нет ответного матча – например, финал).
    PLAY_OFF_SECOND_MATCH = 3  # Второй матч в плей-офф, когда их два.
    PLAY_OFF_FIRST_MATCH = 4  # Первый матч в плей-офф, когда их два. Исход (кто проходит) определяется по сумме двух матчей.


class Event:

    def __init__(self,
                 uuid: str,
                 team_1: str,
                 team_2: str,
                 time: datetime,
                 event_type: EventType,
                 result: EventResult | None = None,
                 ):
        self.uuid = uuid
        self.team_1 = team_1
        self.team_2 = team_2
        self.time = time
        self.event_type = event_type
        self.result = result

    def get_time_in_utc(self) -> datetime:
        return self.time.replace(tzinfo=timezone.utc)

    def get_time_in_moscow_zone(self) -> datetime:
        return datetime_utils.with_zone_same_instant(
            datetime_obj=self.time,
            timezone_from=pytz.utc,
            timezone_to=pytz.timezone('Europe/Moscow'),
        )

    def is_started(self) -> bool:
        return self.time.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)

    def is_finished(self) -> bool:
        return self.result is not None

    def is_in_progress(self) -> bool:
        return self.is_started() and not self.is_finished()

    def decides_who_goes_through(self) -> bool:
        return self.event_type in (EventType.PLAY_OFF_SINGLE_MATCH, EventType.PLAY_OFF_SECOND_MATCH)


class Bet:

    def __init__(self,
                 user_id: int,
                 event_uuid: str,
                 team_1_scores: int,
                 team_2_scores: int,
                 team_1_will_go_through: bool | None,
                 created_at: datetime,
                 is_joker: bool = False,
                 ):
        self.user_id = user_id
        self.event_uuid = event_uuid
        self.team_1_scores = team_1_scores
        self.team_2_scores = team_2_scores
        self.team_1_will_go_through = team_1_will_go_through
        # Для GROUP_STAGE и PLAY_OFF_FIRST_MATCH всегда None — проход определяется не текущим матчем.
        # В матчах PLAY_OFF_SINGLE_MATCH и PLAY_OFF_SECOND_MATCH обязательно будет или True, или False.
        # Только если юзер поставил на ничью и проигнорировал кнопку с тем, кто пройдёт дальше, будет None.
        self.created_at = created_at
        self.is_joker = is_joker

    def is_bet_on_draw(self) -> bool:
        return self.team_1_scores == self.team_2_scores


class UserModel:
    def __init__(self,
                 id: int,
                 username: str,
                 first_name: str,
                 last_name: str,
                 last_interaction: datetime,
                 created_at: datetime,
                 scores: int,
                 bets: list,
                 ):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.last_interaction = last_interaction
        self.created_at = created_at
        self.scores = scores
        self.bets = bets

    def get_full_name(self) -> str:
        if self.last_name:
            return f'{self.first_name} {self.last_name}'
        return self.first_name


class Guessers:
    def __init__(self, guessed_total_score: list,
                 guessed_goal_difference: list,
                 guessed_draw: list,
                 guessed_only_winner: list,
                 guessed_who_has_gone_through: list,
                 triggered_jokers: list | None = None):
        self.guessed_total_score = guessed_total_score
        self.guessed_goal_difference = guessed_goal_difference
        self.guessed_draw = guessed_draw
        self.guessed_only_winner = guessed_only_winner
        self.guessed_who_has_gone_through = guessed_who_has_gone_through
        self.triggered_jokers = triggered_jokers or []

    def is_everything_empty(self) -> bool:
        return (len(self.guessed_total_score) == 0 and
                len(self.guessed_goal_difference) == 0 and
                len(self.guessed_draw) == 0 and
                len(self.guessed_only_winner) == 0 and
                len(self.guessed_who_has_gone_through) == 0)


class GuessedEvent(Enum):
    WINNER = 1
    GOAL_DIFFERENCE = 2
    DRAW = 3
    EXACT_SCORE = 4


class DetailedStatistic:
    def __init__(self, user_model: UserModel,
                 guessed_total_score_count: int,
                 guessed_goal_difference_count: int,
                 guessed_draw_count: int,
                 guessed_only_winner_count: int,
                 guessed_who_has_gone_through_count: int,
                 one_goal_from_total_score_count_with_winner_consider: int,
                 one_goal_from_total_score_count_exclude_winner: int,
                 triggered_jokers_count: int,
                 joker_bets_count: int,
                 joker_bonus_scores: int,
                 ):
        self.user_model = user_model
        self.guessed_total_score_count = guessed_total_score_count
        self.guessed_goal_difference_count = guessed_goal_difference_count
        self.guessed_draw_count = guessed_draw_count
        self.guessed_only_winner_count = guessed_only_winner_count
        self.guessed_who_has_gone_through_count = guessed_who_has_gone_through_count
        self.one_goal_from_total_score_count_with_winner_consider = one_goal_from_total_score_count_with_winner_consider
        self.one_goal_from_total_score_count_exclude_winner = one_goal_from_total_score_count_exclude_winner
        self.triggered_jokers_count = triggered_jokers_count
        self.joker_bets_count = joker_bets_count
        self.joker_bonus_scores = joker_bonus_scores


class Group:
    # Группа турнира: id (буква, которую ввёл мейнтейнер) + упорядоченный список команд.
    def __init__(self, id: str, name: str, teams: list):
        self.id = id
        self.name = name
        self.teams = teams


class Tournament:
    # Изолированная структура турнира для спецставок (чемпион + чемпионы групп).
    # Не связана с Event/Bet/джокерами; хранится в отдельной коллекции одним документом.
    def __init__(self,
                 name: str,
                 created_at: datetime,
                 groups: list,
                 champion_bet_open: bool = False,
                 group_bet_open: bool = False,
                 champion_winner: str | None = None,
                 group_winners: dict | None = None,
                 champion_settled: bool = False,
                 group_settled: bool = False,
                 ):
        self.name = name
        self.created_at = created_at
        self.groups = groups
        self.champion_bet_open = champion_bet_open
        self.group_bet_open = group_bet_open
        self.champion_winner = champion_winner
        self.group_winners = group_winners
        self.champion_settled = champion_settled
        self.group_settled = group_settled

    def all_teams(self) -> list:
        # Кандидаты в чемпионы = все команды по всем группам, в порядке групп/команд.
        return [team for group in self.groups for team in group.teams]

    def group_count(self) -> int:
        return len(self.groups)

    def get_group(self, group_id: str) -> Group | None:
        needle = group_id.strip().casefold()
        return next((g for g in self.groups if g.id.casefold() == needle), None)

    def find_team(self, team_name: str) -> str | None:
        # Case-insensitive, trimmed поиск по всем командам; возвращает каноническое
        # написание из структуры (чтобы хранить и сравнивать единообразно).
        needle = team_name.strip().casefold()
        for team in self.all_teams():
            if team.casefold() == needle:
                return team
        return None

    def find_team_in_group(self, group_id: str, team_name: str) -> str | None:
        group = self.get_group(group_id)
        if group is None:
            return None
        needle = team_name.strip().casefold()
        for team in group.teams:
            if team.casefold() == needle:
                return team
        return None
