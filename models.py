from datetime import datetime, timezone
from enum import Enum

import pytz

import datetime_utils


class EventResult:
    def __init__(self, team_1_scores: int, team_2_scores: int, team_1_has_gone_through: bool | None):
        self.team_1_scores = team_1_scores
        self.team_2_scores = team_2_scores
        self.team_1_has_gone_through = team_1_has_gone_through  # указываем None для матчах типа SIMPLE.
        # В матчах PLAY_OFF_SINGLE_MATCH и PLAY_OFF_SECOND_MATCH обязательно будет или True, или False.

    def is_draw(self) -> bool:
        return self.team_1_scores == self.team_2_scores


class EventType(Enum):
    SIMPLE = 1  # Групповые этапы, а также первый матч в плей-офф, когда их два.
    PLAY_OFF_SINGLE_MATCH = 2  # Единственный матч в плей-офф (когда нет ответного матча – например, финал).
    PLAY_OFF_SECOND_MATCH = 3  # Второй матч в плей-офф, когда их два.

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



class Bet:

    def __init__(self,
                 user_id: int,
                 event_uuid: str,
                 team_1_scores: int,
                 team_2_scores: int,
                 team_1_will_go_through: bool | None,
                 created_at: datetime,
                 ):
        self.user_id = user_id
        self.event_uuid = event_uuid
        self.team_1_scores = team_1_scores
        self.team_2_scores = team_2_scores
        self.team_1_will_go_through = team_1_will_go_through
        # указываем None для матчах типа SIMPLE. В матчах PLAY_OFF_SINGLE_MATCH и PLAY_OFF_SECOND_MATCH
        # обязательно будет или True, или False. Только если юзер не поставит на ничью и проигнорирует кнопку с тем,
        # кто пройдёт дальше (тогда будет None).
        self.created_at = created_at

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
                 guessed_only_winner: list,
                 guessed_who_has_gone_through: list):
        self.guessed_total_score = guessed_total_score
        self.guessed_goal_difference = guessed_goal_difference
        self.guessed_only_winner = guessed_only_winner
        self.guessed_who_has_gone_through = guessed_who_has_gone_through

    def is_everything_empty(self) -> bool:
        return (len(self.guessed_total_score) == 0 and
                len(self.guessed_goal_difference) == 0 and
                len(self.guessed_only_winner) == 0 and
                len(self.guessed_who_has_gone_through) == 0)


class GuessedEvent(Enum):
    WINNER = 1
    GOAL_DIFFERENCE = 2
    EXACT_SCORE = 3
