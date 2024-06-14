from models import Event


def create_make_bet_callback_data(event: Event) -> str:
    return f'make_bet_{event.uuid}'


def is_make_bet_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('make_bet_')


def extract_uuid_from_make_bet_callback_data(callback_data: str) -> str:
    if not is_make_bet_callback_data(callback_data):
        raise ValueError('This is not make bet callback data')
    return callback_data.removeprefix('make_bet_')
