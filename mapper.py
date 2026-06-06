import constants
from models import Event, EventResult, Bet, UserModel, EventType, Group, Tournament


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
        'type': event_type_to_int(event.event_type),
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
    event_type = EventType.GROUP_STAGE
    if 'is_playoff' in event_dict:  # deprecated, can be removed in future
        event_type = EventType.PLAY_OFF_SECOND_MATCH
    elif 'type' in event_dict:
        event_type = parse_event_type(event_dict['type'])

    return Event(
        uuid=event_dict['uuid'],
        team_1=event_dict['team_1'],
        team_2=event_dict['team_2'],
        time=event_dict['time'],
        event_type=event_type,
        result=result_obj
    )


def event_type_to_int(event_type: EventType) -> int:
    match event_type:
        case EventType.GROUP_STAGE:
            return 1
        case EventType.PLAY_OFF_SINGLE_MATCH:
            return 2
        case EventType.PLAY_OFF_SECOND_MATCH:
            return 3
        case EventType.PLAY_OFF_FIRST_MATCH:
            return 4
        case _:
            raise ValueError(f'Unknown enum value: {event_type}')


def parse_event_type(value: int) -> EventType:
    match value:
        case 1:
            return EventType.GROUP_STAGE
        case 2:
            return EventType.PLAY_OFF_SINGLE_MATCH
        case 3:
            return EventType.PLAY_OFF_SECOND_MATCH
        case 4:
            return EventType.PLAY_OFF_FIRST_MATCH
        case _:
            raise ValueError(f'Unknown int value: {value}')


def bet_to_dict(bet: Bet) -> dict:
    return {
        'user_id': bet.user_id,
        'event_uuid': bet.event_uuid,
        'team_1_scores': bet.team_1_scores,
        'team_2_scores': bet.team_2_scores,
        'team_1_will_go_through': bet.team_1_will_go_through,
        'created_at': bet.created_at,
        'is_joker': bet.is_joker,
    }


def parse_bet(bet_dict: dict) -> Bet:
    team_1_will_go_through = None
    if 'team_1_will_go_through' in bet_dict:
        team_1_will_go_through = bet_dict['team_1_will_go_through']
    is_joker = False
    if 'is_joker' in bet_dict:
        is_joker = bet_dict['is_joker']
    return Bet(
        user_id=bet_dict['user_id'],
        event_uuid=bet_dict['event_uuid'],
        team_1_scores=bet_dict['team_1_scores'],
        team_2_scores=bet_dict['team_2_scores'],
        team_1_will_go_through=team_1_will_go_through,
        created_at=bet_dict['created_at'],
        is_joker=is_joker,
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


def group_to_dict(group: Group) -> dict:
    return {'id': group.id, 'name': group.name, 'teams': list(group.teams)}


def parse_group(group_dict: dict) -> Group:
    return Group(
        id=group_dict['id'],
        name=group_dict['name'] if 'name' in group_dict else group_dict['id'],
        teams=group_dict['teams'],
    )


def tournament_to_dict(tournament: Tournament) -> dict:
    return {
        '_id': constants.ACTIVE_TOURNAMENT_ID,
        'name': tournament.name,
        'created_at': tournament.created_at,
        'groups': list(map(group_to_dict, tournament.groups)),
        'champion_bet_open': tournament.champion_bet_open,
        'group_bet_open': tournament.group_bet_open,
        'champion_winner': tournament.champion_winner,
        'group_winners': tournament.group_winners,
        'champion_settled': tournament.champion_settled,
        'group_settled': tournament.group_settled,
    }


def parse_tournament(tournament_dict: dict) -> Tournament:
    # Back-compat: отсутствующий флаг -> False, отсутствующий winner -> None.
    def flag(key: str) -> bool:
        return tournament_dict[key] if key in tournament_dict else False

    def optional(key: str):
        return tournament_dict[key] if key in tournament_dict else None

    return Tournament(
        name=tournament_dict['name'],
        created_at=tournament_dict['created_at'],
        groups=list(map(parse_group, tournament_dict['groups'])),
        champion_bet_open=flag('champion_bet_open'),
        group_bet_open=flag('group_bet_open'),
        champion_winner=optional('champion_winner'),
        group_winners=optional('group_winners'),
        champion_settled=flag('champion_settled'),
        group_settled=flag('group_settled'),
    )
