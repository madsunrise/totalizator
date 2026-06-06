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


def is_delete_bet_button(callback_data: str) -> bool:
    return callback_data == 'delete_bet_button'


def create_delete_bet_button() -> str:
    return 'delete_bet_button'


def create_delete_specific_bet_callback_data(event_uuid: str) -> str:
    return f'delete_specific_bet_{event_uuid}'


def is_delete_specific_bet_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('delete_specific_bet_')


def extract_uuid_from_delete_specific_bet_callback_data(callback_data: str) -> str:
    if not is_delete_specific_bet_callback_data(callback_data):
        raise ValueError('This is not make bet callback data')
    return callback_data.removeprefix('delete_specific_bet_')


def create_set_joker_callback_data(event_uuid: str) -> str:
    return f'set_joker_{event_uuid}'


def is_set_joker_callback_data(callback_data: str) -> bool:
    # 'set_joker_button' тоже начинается с 'set_joker_', но это кнопка-меню, а не set_joker_<uuid>.
    return callback_data.startswith('set_joker_') and not is_set_joker_button(callback_data)


def extract_uuid_from_set_joker_callback_data(callback_data: str) -> str:
    if not is_set_joker_callback_data(callback_data):
        raise ValueError('This is not set joker callback data')
    return callback_data.removeprefix('set_joker_')


def create_set_joker_button() -> str:
    return 'set_joker_button'


def is_set_joker_button(callback_data: str) -> bool:
    return callback_data == 'set_joker_button'


def create_remove_joker_button() -> str:
    return 'remove_joker_button'


def is_remove_joker_button(callback_data: str) -> bool:
    return callback_data == 'remove_joker_button'


def create_set_specific_joker_callback_data(event_uuid: str) -> str:
    return f'set_specific_joker_{event_uuid}'


def is_set_specific_joker_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('set_specific_joker_')


def extract_uuid_from_set_specific_joker_callback_data(callback_data: str) -> str:
    if not is_set_specific_joker_callback_data(callback_data):
        raise ValueError('This is not set specific joker callback data')
    return callback_data.removeprefix('set_specific_joker_')


def create_remove_specific_joker_callback_data(event_uuid: str) -> str:
    return f'remove_specific_joker_{event_uuid}'


def is_remove_specific_joker_callback_data(callback_data: str) -> bool:
    return callback_data.startswith('remove_specific_joker_')


def extract_uuid_from_remove_specific_joker_callback_data(callback_data: str) -> str:
    if not is_remove_specific_joker_callback_data(callback_data):
        raise ValueError('This is not remove specific joker callback data')
    return callback_data.removeprefix('remove_specific_joker_')


def extract_uuid_from_team_2_will_go_through_callback_data(callback_data: str) -> str:
    if not is_team_2_will_go_through_callback_data(callback_data):
        raise ValueError('This is not team_2_will_go_through callback data')
    return callback_data.removeprefix('team_2_will_go_through_')


# --- Спецставки: ставка на чемпиона турнира ---------------------------------------
# В callback_data едут ТОЛЬКО индексы (имена команд — лишь в тексте кнопок),
# поэтому всё надёжно укладывается в лимит Telegram 64 байта.

def create_champion_open() -> str:
    return 'champ_open'


def is_champion_open(callback_data: str) -> bool:
    return callback_data == 'champ_open'


def create_champion_group(group_index: int) -> str:
    return f'champgrp_{group_index}'


def is_champion_group(callback_data: str) -> bool:
    return callback_data.startswith('champgrp_')


def extract_group_index_from_champion_group(callback_data: str) -> int:
    if not is_champion_group(callback_data):
        raise ValueError('This is not champion group callback data')
    return int(callback_data.removeprefix('champgrp_'))


def create_champion_team(group_index: int, team_index: int) -> str:
    return f'champteam_{group_index}_{team_index}'


def is_champion_team(callback_data: str) -> bool:
    return callback_data.startswith('champteam_')


def extract_indexes_from_champion_team(callback_data: str) -> tuple[int, int]:
    if not is_champion_team(callback_data):
        raise ValueError('This is not champion team callback data')
    group_index, team_index = callback_data.removeprefix('champteam_').split('_')
    return int(group_index), int(team_index)


# --- Спецставки: ставка на победителей групп --------------------------------------

def create_group_overview() -> str:
    return 'grp_over'


def is_group_overview(callback_data: str) -> bool:
    return callback_data == 'grp_over'


def create_group_done() -> str:
    return 'grp_done'


def is_group_done(callback_data: str) -> bool:
    return callback_data == 'grp_done'


def create_group_pick(group_index: int) -> str:
    return f'grppick_{group_index}'


def is_group_pick(callback_data: str) -> bool:
    return callback_data.startswith('grppick_')


def extract_group_index_from_group_pick(callback_data: str) -> int:
    if not is_group_pick(callback_data):
        raise ValueError('This is not group pick callback data')
    return int(callback_data.removeprefix('grppick_'))


def create_group_team(group_index: int, team_index: int) -> str:
    return f'grpteam_{group_index}_{team_index}'


def is_group_team(callback_data: str) -> bool:
    return callback_data.startswith('grpteam_')


def extract_indexes_from_group_team(callback_data: str) -> tuple[int, int]:
    if not is_group_team(callback_data):
        raise ValueError('This is not group team callback data')
    group_index, team_index = callback_data.removeprefix('grpteam_').split('_')
    return int(group_index), int(team_index)
