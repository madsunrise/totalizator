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
