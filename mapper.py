from models import Event, EventResult, Bet, UserModel


def event_to_dict(event: Event) -> dict:
    result_dict = {}
    if event.result is not None:
        result_dict = {
            'team_1': event.result.team_1_scores,
            'team_2': event.result.team_2_scores,
            'team_1_has_gone_through': event.result.team_1_has_gone_through,
        }
    return {
        'uuid': event.uuid,
        'team_1': event.team_1,
        'team_2': event.team_2,
        'time': event.time,
        'is_playoff': event.is_playoff,
        'result': result_dict
    }


def parse_event(event_dict: dict) -> Event:
    result_dict = event_dict['result']
    result_obj = None
    if result_dict:
        team_1_has_gone_through = None
        if 'team_1_has_gone_through' in result_dict:
            team_1_has_gone_through = result_dict['team_1_has_gone_through']
        result_obj = EventResult(
            team_1_scores=result_dict['team_1'],
            team_2_scores=result_dict['team_2'],
            team_1_has_gone_through=team_1_has_gone_through
        )
    is_playoff = False
    if 'is_playoff' in event_dict:
        is_playoff = event_dict['is_playoff']
    return Event(
        uuid=event_dict['uuid'],
        team_1=event_dict['team_1'],
        team_2=event_dict['team_2'],
        time=event_dict['time'],
        is_playoff=is_playoff,
        result=result_obj
    )


def bet_to_dict(bet: Bet) -> dict:
    return {
        'user_id': bet.user_id,
        'event_uuid': bet.event_uuid,
        'team_1_scores': bet.team_1_scores,
        'team_2_scores': bet.team_2_scores,
        'team_1_will_go_through': bet.team_1_will_go_through,
        'created_at': bet.created_at,
    }


def parse_bet(bet_dict: dict) -> Bet:
    team_1_will_go_through = None
    if 'team_1_will_go_through' in bet_dict:
        team_1_will_go_through = bet_dict['team_1_will_go_through']
    return Bet(
        user_id=bet_dict['user_id'],
        event_uuid=bet_dict['event_uuid'],
        team_1_scores=bet_dict['team_1_scores'],
        team_2_scores=bet_dict['team_2_scores'],
        team_1_will_go_through=team_1_will_go_through,
        created_at=bet_dict['created_at'],
    )


def user_to_dict(user: UserModel) -> dict:
    return {
        '_id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'last_interaction': user.last_interaction,
        'created_at': user.created_at,
        'scores': user.scores,
        'bets': list(map(lambda x: bet_to_dict(x), user.bets)),
    }


def parse_user(user_dict: dict) -> UserModel:
    return UserModel(
        id=user_dict['_id'],
        username=user_dict['username'],
        first_name=user_dict['first_name'],
        last_name=user_dict['last_name'],
        last_interaction=user_dict['last_interaction'],
        created_at=user_dict['created_at'],
        scores=user_dict['scores'],
        bets=list(map(lambda x: parse_bet(x), user_dict['bets'])),
    )
