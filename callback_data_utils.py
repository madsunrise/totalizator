from models import Event


def create_make_bet_callback_data(event: Event) -> str:
    return f'make_bet_{event.uuid}'


def is_make_bet_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('make_bet_')


def extract_uuid_from_make_bet_callback_data(callback_data: str) -> str:
    if not is_make_bet_callback_data(callback_data):
        raise ValueError('This is not make bet callback data')
    return callback_data.removeprefix('make_bet_')


def create_team_1_will_go_through_callback_data(event: Event) -> str:
    return f'team_1_will_go_through_{event.uuid}'


def is_team_1_will_go_through_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('team_1_will_go_through_')


def extract_uuid_from_team_1_will_go_through_callback_data(callback_data: str) -> str:
    if not is_team_1_will_go_through_callback_data(callback_data):
        raise ValueError('This is not team_1_will_go_through callback data')
    return callback_data.removeprefix('team_1_will_go_through_')


def create_team_2_will_go_through_callback_data(event: Event) -> str:
    return f'team_2_will_go_through_{event.uuid}'


def is_team_2_will_go_through_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('team_2_will_go_through_')


def is_show_my_already_played_bets(callback_data: str) -> bool:
    return callback_data == 'show_my_already_played_bets'


def create_show_my_already_played_bets() -> str:
    return 'show_my_already_played_bets'


def extract_uuid_from_team_2_will_go_through_callback_data(callback_data: str) -> str:
    if not is_team_2_will_go_through_callback_data(callback_data):
        raise ValueError('This is not team_2_will_go_through callback data')
    return callback_data.removeprefix('team_2_will_go_through_')
