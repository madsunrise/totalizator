from datetime import datetime


class EventResult:
    def __init__(self, team_1_scores: int, team_2_scores: int):
        self.team_1_scores = team_1_scores
        self.team_2_scores = team_2_scores


class Event:

    def __init__(self,
                 uuid: str,
                 team_1: str,
                 team_2: str,
                 time: datetime,
                 result: EventResult | None = None,
                 ):
        self.uuid = uuid
        self.team_1 = team_1
        self.team_2 = team_2
        self.time = time
        self.result = result


class Bet:

    def __init__(self,
                 user_id: int,
                 event_uuid: str,
                 team_1_scores: int,
                 team_2_scores: int,
                 created_at: datetime,
                 ):
        self.user_id = user_id
        self.event_uuid = event_uuid
        self.team_1_scores = team_1_scores
        self.team_2_scores = team_2_scores
        self.created_at = created_at


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
