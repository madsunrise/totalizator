from models import Event, EventResult, Bet


def event_to_dict(event: Event) -> dict:
    result_dict = {}
    if event.result is not None:
        result_dict = {
            'team_1': event.result.team_1_scores,
            'team_2': event.result.team_2_scores
        }
    return {
        'uuid': event.uuid,
        'team_1': event.team_1,
        'team_2': event.team_2,
        'time': event.time,
        'result': result_dict
    }


def parse_event(event_dict: dict) -> Event:
    result_dict = event_dict['result']
    result_obj = None
    if result_dict:
        result_obj = EventResult(team_1_scores=result_dict['team_1'], team_2_scores=result_dict['team_2'])
    return Event(
        uuid=event_dict['uuid'],
        team_1=event_dict['team_1'],
        team_2=event_dict['team_2'],
        time=event_dict['time'],
        result=result_obj
    )


def bet_to_dict(bet: Bet) -> dict:
    return {
        'user_id': bet.user_id,
        'event_uuid': bet.event_uuid,
        'team_1_scores': bet.team_1_scores,
        'team_2_scores': bet.team_2_scores,
        'created_at': bet.created_at,
    }


def parse_bet(bet_dict: dict) -> Bet:
    return Bet(
        user_id=bet_dict['user_id'],
        event_uuid=bet_dict['event_uuid'],
        team_1_scores=bet_dict['team_1_scores'],
        team_2_scores=bet_dict['team_2_scores'],
        created_at=bet_dict['created_at'],
    )
