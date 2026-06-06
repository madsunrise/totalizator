import csv
import locale
import logging
import os
import pytz
import requests
import schedule
import telebot
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from telebot.types import User, InlineKeyboardMarkup, InlineKeyboardButton

import callback_data_utils
import constants
import datetime_utils
import joker_utils
import strings
import telegram_utils
import utils
from database import Database
from models import Event, EventResult, Bet, Guessers, GuessedEvent, EventType, DetailedStatistic, UserModel

locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
bot = telebot.TeleBot(os.environ[constants.ENV_BOT_TOKEN])
database = Database()
joker_write_lock = threading.Lock()
logging.basicConfig(filename='totalizator.log', encoding='utf-8', level=logging.INFO)


@bot.message_handler(commands=['start'])
def start(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    bot.send_message(chat_id=message.chat.id, text=strings.START_MESSAGE)
    pass


# Service method
# Формат сообщения для матча группового этапа: "Германия; Шотландия; 14.06.2024 22:00; group"
# Формат сообщения для первого матча в плей-офф (двухматчевая пара): "Германия; Шотландия; 14.06.2024 22:00; playoff_first_match"
# Формат сообщения для ответного (второго) матча в плей-офф: "Германия; Шотландия; 14.06.2024 22:00; playoff_second_match"
# Формат сообщения для матча на вылет (например, финал): "Германия; Шотландия; 14.06.2024 22:00; playoff_single"
@bot.message_handler(commands=['add_event'])
def add_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    split = list(map(lambda x: x.strip(), message.text.removeprefix('/add_event').strip().split(';')))

    if len(split) != 4:
        bot.send_message(chat_id=message.chat.id, text=strings.WRONG_MESSAGE_FORMAT_ERROR)
        return

    team_1 = split[0]
    team_2 = split[1]

    date_format = '%d.%m.%Y %H:%M'
    datetime_obj = datetime.strptime(split[2], date_format)

    if split[3] == 'playoff_single':
        event_type = EventType.PLAY_OFF_SINGLE_MATCH
    elif split[3] == 'playoff_second_match':
        event_type = EventType.PLAY_OFF_SECOND_MATCH
    elif split[3] == 'playoff_first_match':
        event_type = EventType.PLAY_OFF_FIRST_MATCH
    elif split[3] == 'group':
        event_type = EventType.GROUP_STAGE
    else:
        bot.send_message(chat_id=message.chat.id, text=strings.WRONG_MESSAGE_FORMAT_ERROR)
        return

    event_datetime_utc = datetime_utils.with_zone_same_instant(
        datetime_obj=datetime_obj,
        timezone_from=pytz.timezone('Europe/Moscow'),
        timezone_to=pytz.utc
    )

    existing_event = database.find_event(team_1=team_1, team_2=team_2, time=event_datetime_utc)
    if existing_event is not None:
        bot.send_message(chat_id=message.chat.id, text=strings.EVENT_ALREADY_EXIST_ERROR)
        return

    event = Event(
        uuid=utils.generate_uuid(),
        team_1=team_1,
        team_2=team_2,
        time=event_datetime_utc,
        event_type=event_type,
    )
    database.add_event(event)
    msg = f"{strings.OK}: "
    msg += f"{event.team_1} – {event.team_2}, {datetime_utils.to_display_string(datetime_obj)}"
    bot.send_message(chat_id=message.chat.id, text=msg)


# Service method
# Формат сообщения: "916dbd19-7d2c-46b6-a96a-0f726a22ec9c 2:1".
# Если это плей-офф, и в основное время была ничья, то указываем сразу после счёта кто прошёл дальше:
# "916dbd19-7d2c-46b6-a96a-0f726a22ec9c 1:1 Испания".
@bot.message_handler(commands=['result'])
def set_result_for_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    split = list(map(lambda x: x.strip(), message.text.removeprefix('/result').strip().split(' ')))

    if len(split) < 2:
        bot.send_message(chat_id=message.chat.id, text=strings.WRONG_MESSAGE_FORMAT_ERROR)
        return

    event_uuid = split[0]
    result_scores = split[1]
    team_1_scores = int(result_scores.split(':')[0])
    team_2_scores = int(result_scores.split(':')[1])

    existing_event = database.get_event_by_uuid(uuid=event_uuid)
    if not existing_event:
        bot.send_message(chat_id=message.chat.id, text=f'Матч с UUID = {event_uuid} не найден :/')
        return

    if existing_event.result:
        scores_team_1 = existing_event.result.team_1_scores
        scores_team_2 = existing_event.result.team_2_scores
        msg = f'У матча уже записан результат ({scores_team_1}:{scores_team_2})'
        bot.send_message(chat_id=message.chat.id, text=msg)
        return

    if existing_event.event_type == EventType.PLAY_OFF_SINGLE_MATCH and team_1_scores == team_2_scores and len(
            split) < 3:
        bot.send_message(chat_id=message.chat.id, text='Не указано, кто прошёл дальше!')
        return
    elif existing_event.event_type == EventType.PLAY_OFF_SECOND_MATCH and len(split) < 3:
        # Здесь указать надо в любом случае, т.к. по счёту нельзя определить, кто прошёл.
        bot.send_message(chat_id=message.chat.id, text='Не указано, кто прошёл дальше!')
        return

    event_type = existing_event.event_type
    match event_type:
        case EventType.GROUP_STAGE | EventType.PLAY_OFF_FIRST_MATCH:
            team_1_has_gone_through = None
        case EventType.PLAY_OFF_SINGLE_MATCH:
            if team_1_scores > team_2_scores:
                team_1_has_gone_through = True
            elif team_1_scores < team_2_scores:
                team_1_has_gone_through = False
            else:
                # В случае ничьи указать прошедшую команду нужно явно.
                team_winner = ' '.join(split[2:])
                if team_winner.lower() == existing_event.team_1.lower():
                    team_1_has_gone_through = True
                elif team_winner.lower() == existing_event.team_2.lower():
                    team_1_has_gone_through = False
                else:
                    bot.send_message(chat_id=message.chat.id, text=f'Неизвестная команда: {team_winner}')
                    return
        case EventType.PLAY_OFF_SECOND_MATCH:
            team_winner = ' '.join(split[2:])
            if team_winner.lower() == existing_event.team_1.lower():
                team_1_has_gone_through = True
            elif team_winner.lower() == existing_event.team_2.lower():
                team_1_has_gone_through = False
            else:
                bot.send_message(chat_id=message.chat.id, text=f'Неизвестная команда: {team_winner}')
                return
        case _:
            raise ValueError(f'Unknown enum value: {event_type}')

    existing_event.result = EventResult(
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores,
        team_1_has_gone_through=team_1_has_gone_through
    )
    database.update_event(event=existing_event)
    guessers = calculate_scores_after_finished_event(event=existing_event)
    msg_text = (f'Матч {existing_event.team_1} – {existing_event.team_2} завершился ' +
                f'({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores}).')

    if event_type == EventType.PLAY_OFF_SINGLE_MATCH or event_type == EventType.PLAY_OFF_SECOND_MATCH:
        if existing_event.result.team_1_has_gone_through is None:
            raise ValueError('team_1_has_gone_through cannot be None here')
        msg_text += ' '
        if existing_event.result.team_1_has_gone_through:
            msg_text += f'Проходит {existing_event.team_1}.'
        else:
            msg_text += f'Проходит {existing_event.team_2}.'

    bot.send_message(chat_id=message.chat.id, text=msg_text)

    msg_text += '\n\n'
    if guessers.is_everything_empty():
        msg_text += 'Никто не угадал результат.'
        msg_text += '\n\n'
    else:
        if len(guessers.guessed_total_score) == 1:
            msg_text += f'{guessers.guessed_total_score[0].get_full_name()} угадал точный счёт!'
            msg_text += '\n\n'
        elif len(guessers.guessed_total_score) > 1:
            msg_text += 'Угадали точный счёт:'
            msg_text += '\n'
            for user_model in guessers.guessed_total_score:
                msg_text += user_model.get_full_name()
                msg_text += '\n'
            msg_text += '\n'

        if len(guessers.guessed_goal_difference) == 1:
            msg_text += f'{guessers.guessed_goal_difference[0].get_full_name()} угадал разницу мячей.'
            msg_text += '\n\n'
        elif len(guessers.guessed_goal_difference) > 1:
            msg_text += 'Угадали разницу мячей:'
            msg_text += '\n'
            for user_model in guessers.guessed_goal_difference:
                msg_text += user_model.get_full_name()
                msg_text += '\n'
            msg_text += '\n'

        if len(guessers.guessed_draw) == 1:
            msg_text += f'{guessers.guessed_draw[0].get_full_name()} угадал ничью.'
            msg_text += '\n\n'
        elif len(guessers.guessed_draw) > 1:
            msg_text += 'Угадали ничью:'
            msg_text += '\n'
            for user_model in guessers.guessed_draw:
                msg_text += user_model.get_full_name()
                msg_text += '\n'
            msg_text += '\n'

        if len(guessers.guessed_only_winner) == 1:
            msg_text += f'{guessers.guessed_only_winner[0].get_full_name()} угадал исход.'
            msg_text += '\n\n'
        elif len(guessers.guessed_only_winner) > 1:
            msg_text += 'Угадали исход:'
            msg_text += '\n'
            for user_model in guessers.guessed_only_winner:
                msg_text += user_model.get_full_name()
                msg_text += '\n'
            msg_text += '\n'

        if len(guessers.guessed_who_has_gone_through) > 0:
            msg_text += '--'
            msg_text += '\n'
            msg_text += '+1 очко за проход:'
            msg_text += '\n'
            for user_model in guessers.guessed_who_has_gone_through:
                msg_text += user_model.get_full_name()
                msg_text += '\n'
            msg_text += '\n'

    msg_text += '-----\n'
    msg_text += get_leaderboard_text()
    bot.send_message(chat_id=get_target_chat_id(), text=msg_text)


@bot.message_handler(commands=['events'])
def get_all_events(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    events = database.get_all_events()
    if len(events) == 0:
        bot.send_message(chat_id=message.chat.id, text='Матчей не обнаружено')
        return
    text = ''
    for idx, event in enumerate(events):
        event_result = event.result
        if event_result is not None:
            continue
        text += f"{event.team_1} – {event.team_2}, {datetime_utils.to_display_string(event.get_time_in_moscow_zone())}"
        text += '\n'
        text += event.uuid
        text += ' \n\n'
    text = text.strip()
    if len(text) == 0:
        text = 'Предстоящих событий не обнаружено'
    telegram_utils.safe_send_message(bot=bot, chat_id=message.chat.id, text=text)


@bot.message_handler(commands=['export_statistic'])
def export_statistic(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    with open('stat.csv', 'w', newline='') as file:
        writer = csv.writer(file)
        field = [
            "Команда 1",
            "Команда 2",
            "Голы команды 1",
            "Голы команды 2",
            "Проход",
            "Юзер",
            "Ставка на команду 1",
            "Ставка на команду 2",
            "Ставка на проход"
        ]

        writer.writerow(field)
        events = database.get_all_events()
        users = database.get_all_users()
        for event in events:
            event_result = event.result
            if event_result is None:
                continue
            for user in users:
                bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
                if bet is not None:
                    go_through = None
                    if event_result.team_1_has_gone_through is not None:
                        if event_result.team_1_has_gone_through:
                            go_through = event.team_1
                        else:
                            go_through = event.team_2
                    bet_go_through = None
                    if bet.team_1_will_go_through is not None:
                        if bet.team_1_will_go_through:
                            bet_go_through = event.team_1
                        else:
                            bet_go_through = event.team_2
                    writer.writerow([
                        event.team_1,
                        event.team_2,
                        event_result.team_1_scores,
                        event_result.team_2_scores,
                        go_through,
                        user.username,
                        bet.team_1_scores,
                        bet.team_2_scores,
                        bet_go_through
                    ])

    with open('stat.csv', 'rb') as file:
        url = f'https://api.telegram.org/bot{os.environ[constants.ENV_BOT_TOKEN]}/sendDocument'
        files = {
            'document': file
        }
        payload = {
            'chat_id': message.chat.id,
        }
        response = requests.post(url, data=payload, files=files)

        if response.ok:
            return response.json()['result']['document']['file_id']
        logging.error(f"Failed to upload file to Telegram API: {response.status_code}")
        raise Exception()


@bot.message_handler(commands=['coming_events'])
def get_coming_events(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    send_coming_events(user_id=user.id, chat_id=message.chat.id)


@bot.message_handler(commands=['clear_context'])
def clear_current_event(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    database.clear_current_event_for_user(user_id=user.id)
    bot.send_message(chat_id=message.chat.id, text='OK')


@bot.message_handler(commands=['my_bets'])
def show_my_bets(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    send_my_bets_message(chat_id=message.chat.id, user_id=user.id)


@bot.message_handler(commands=['leaderboard'])
def get_leaderboard(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    bot.send_message(chat_id=message.chat.id, text=get_leaderboard_text())


@bot.message_handler(commands=['last_18_hours'])
def send_message_with_results_for_last_18_hours(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    hours = 18
    to_time = datetime_utils.get_utc_time()
    from_time = to_time - timedelta(hours=hours)
    events_for_this_period = database.find_events_in_time_range(from_inclusive=from_time, to_exclusive=to_time)
    if len(events_for_this_period) == 0:
        text = f'За последние {hours} часов матчей не было.'
        bot.send_message(chat_id=message.chat.id, text=text.strip())
        return
    text = f'Результаты за последние {hours} часов:\n\n'
    for user_model in database.get_all_users():
        user_id = user_model.id
        scores_earned_total_by_user = 0
        for event in events_for_this_period:
            bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
            event_result = event.result
            if event_result is None:
                continue
            if bet is None:
                continue
            guessed_event = calculate_if_user_guessed_result(event_result=event_result, bet=bet)
            if guessed_event is not None:
                base_scores = convert_guessed_event_to_scores(guessed_event=guessed_event)
                scores_earned_total_by_user += joker_utils.calculate_scores_with_joker(
                    base_scores=base_scores,
                    is_joker=bet.is_joker,
                )
            if event.decides_who_goes_through():
                # Также можно получить +1 очко за проход одной из команд.Независимо от первой ставки.
                if is_guessed_who_has_gone_through(result=event_result, bet=bet):
                    scores_earned_total_by_user += 1
        text += f'{user_model.get_full_name()}: +{scores_earned_total_by_user}'
        text += '\n'
    bot.send_message(chat_id=message.chat.id, text=text.strip())


@bot.message_handler(commands=['detailed_analytics'])
def get_detailed_analytics(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    leaderboard_text = f'Общий рейтинг:\n\n{get_leaderboard_text()}'
    detailed_statistic_text = get_users_detailed_statistic_text()
    matches_statistic = get_matches_result_statistic_text()
    bot.send_message(chat_id=message.chat.id, text=leaderboard_text)
    bot.send_message(chat_id=message.chat.id, text=detailed_statistic_text)
    bot.send_message(chat_id=message.chat.id, text=matches_statistic)


def get_user_bets_with_events(user_id: int) -> list[tuple[Bet, Event]]:
    result = []
    for bet in database.get_all_user_bets(user_id=user_id):
        event = database.get_event_by_uuid(uuid=bet.event_uuid)
        if event is None:
            continue
        result.append((bet, event))
    result.sort(key=lambda x: x[1].time, reverse=False)
    return result


def get_awaiting_bets_with_index(user_id: int) -> list[tuple[int, Bet, Event]]:
    bets_with_events = get_user_bets_with_events(user_id=user_id)
    bets_awaiting = list(filter(lambda x: x[1].result is None, bets_with_events))
    result = []
    index = 1
    for bet, event in bets_awaiting:
        result.append((index, bet, event))
        index += 1
    return result


def get_joker_status_for_user(user_id: int) -> joker_utils.JokerStatus:
    return joker_utils.calculate_joker_status(
        bets_with_events=get_user_bets_with_events(user_id=user_id),
        events=database.get_all_events(),
        now_utc=datetime_utils.get_utc_time(),
    )


def create_joker_offer_markup(user_id: int, event: Event) -> InlineKeyboardMarkup | None:
    bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
    if bet is None:
        return None
    bets_with_events = get_user_bets_with_events(user_id=user_id)
    all_events = database.get_all_events()
    if not joker_utils.can_assign_joker_to_bet(
            bet=bet,
            event=event,
            bets_with_events=bets_with_events,
            events=all_events,
            now_utc=datetime_utils.get_utc_time(),
    ):
        return None
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            text='Поставить джокер',
            callback_data=callback_data_utils.create_set_joker_callback_data(event_uuid=event.uuid),
        )
    )
    return markup


def send_joker_status_message(chat_id: int, user_id: int, event: Event | None = None):
    status = get_joker_status_for_user(user_id=user_id)
    text = joker_utils.get_joker_status_text(status)
    markup = None
    if event is not None:
        markup = create_joker_offer_markup(user_id=user_id, event=event)
        if markup is not None:
            text += '\n\nМожно усилить этот прогноз джокером.'
    bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


def send_public_joker_set_message(user: User, event: Event):
    bot.send_message(
        chat_id=get_target_chat_id(),
        text=f'{user.full_name} поставил джокер на матч {event.team_1} – {event.team_2}.'
    )


def send_public_joker_removed_message(user: User, event: Event):
    bot.send_message(
        chat_id=get_target_chat_id(),
        text=f'{user.full_name} снял джокер с матча {event.team_1} – {event.team_2}.'
    )


def get_user_mention(user_model: UserModel) -> str:
    if user_model.username:
        return f'@{user_model.username}'
    return user_model.first_name


def send_set_joker_selection_message(chat_id: int, user_id: int):
    awaiting_bets = get_awaiting_bets_with_index(user_id=user_id)
    bets_with_events = get_user_bets_with_events(user_id=user_id)
    all_events = database.get_all_events()
    available_bets = []
    for index, bet, event in awaiting_bets:
        if joker_utils.can_assign_joker_to_bet(
                bet=bet,
                event=event,
                bets_with_events=bets_with_events,
                events=all_events,
                now_utc=datetime_utils.get_utc_time(),
        ):
            available_bets.append((index, event))

    if len(available_bets) == 0:
        bot.send_message(chat_id=chat_id, text='Подходящих ставок для джокера не найдено.')
        return

    buttons_list = []
    for index, event in available_bets:
        button = InlineKeyboardButton(
            str(index),
            callback_data=callback_data_utils.create_set_specific_joker_callback_data(event_uuid=event.uuid)
        )
        buttons_list.append(button)
    markup = InlineKeyboardMarkup()
    markup.row(*buttons_list)
    bot.send_message(chat_id=chat_id, text='Выбери номер ставки для джокера', reply_markup=markup)


def send_remove_joker_selection_message(chat_id: int, user_id: int):
    awaiting_bets = get_awaiting_bets_with_index(user_id=user_id)
    removable_bets = []
    for index, bet, event in awaiting_bets:
        if joker_utils.can_remove_joker_from_bet(bet=bet, event=event, now_utc=datetime_utils.get_utc_time()):
            removable_bets.append((index, event))

    if len(removable_bets) == 0:
        bot.send_message(chat_id=chat_id, text='Ставок с джокером для снятия не найдено.')
        return

    buttons_list = []
    for index, event in removable_bets:
        button = InlineKeyboardButton(
            str(index),
            callback_data=callback_data_utils.create_remove_specific_joker_callback_data(event_uuid=event.uuid)
        )
        buttons_list.append(button)
    markup = InlineKeyboardMarkup()
    markup.row(*buttons_list)
    bot.send_message(chat_id=chat_id, text='Выбери номер ставки, с которой нужно снять джокер', reply_markup=markup)


def set_joker_for_event(user: User, chat_id: int, message_id: int, event_uuid: str) -> bool:
    event = database.get_event_by_uuid(uuid=event_uuid)
    if event is None:
        bot.send_message(chat_id=chat_id, text=strings.EVENT_NOT_FOUND_ERROR)
        return False
    with joker_write_lock:
        bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
        if bet is None:
            bot.send_message(chat_id=chat_id, text='Ставка на этот матч не обнаружена.')
            return False
        if not joker_utils.can_assign_joker_to_bet(
                bet=bet,
                event=event,
                bets_with_events=get_user_bets_with_events(user_id=user.id),
                events=database.get_all_events(),
                now_utc=datetime_utils.get_utc_time(),
        ):
            bot.send_message(chat_id=chat_id, text='На этот матч сейчас нельзя поставить джокер.')
            return False
        bet.is_joker = True
        database.update_bet(user_id=user.id, bet=bet)

    send_public_joker_set_message(user=user, event=event)
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f'Джокер поставлен: {event.team_1} – {event.team_2}.'
        )
    except Exception as e:
        logging.exception(e)
    return True


def remove_joker_from_event(user: User, chat_id: int, message_id: int, event_uuid: str) -> bool:
    event = database.get_event_by_uuid(uuid=event_uuid)
    if event is None:
        bot.send_message(chat_id=chat_id, text=strings.EVENT_NOT_FOUND_ERROR)
        return False
    with joker_write_lock:
        bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
        if bet is None:
            bot.send_message(chat_id=chat_id, text='Ставка на этот матч не обнаружена.')
            return False
        if not joker_utils.can_remove_joker_from_bet(bet=bet, event=event, now_utc=datetime_utils.get_utc_time()):
            bot.send_message(chat_id=chat_id, text='С этого матча сейчас нельзя снять джокер.')
            return False
        bet.is_joker = False
        database.update_bet(user_id=user.id, bet=bet)

    send_public_joker_removed_message(user=user, event=event)
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f'Джокер снят: {event.team_1} – {event.team_2}.'
        )
    except Exception as e:
        logging.exception(e)
    return True


@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    user = call.from_user
    chat_id = call.message.chat.id
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    try:
        if callback_data_utils.is_make_bet_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_make_bet_callback_data(call.data)
            event = database.get_event_by_uuid(uuid=event_uuid)
            if event is None:
                bot.send_message(chat_id=chat_id, text=strings.EVENT_NOT_FOUND_ERROR)
                return
            database.save_current_event_to_user(user_id=user.id, event_uuid=event.uuid)
            msg = (f'Укажи счёт, с которым завершится основное время матча '
                   f'{event.team_1} – {event.team_2}. '
                   f'Формат сообщения: \"X:X\" (например, \"1:0\").')
            bot.send_message(chat_id=chat_id, text=msg)

        elif callback_data_utils.is_team_1_will_go_through_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_team_1_will_go_through_callback_data(call.data)
            process_who_will_go_through_bet(
                user_id=user.id,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                event_uuid=event_uuid,
                team_1_will_go_through=True
            )
        elif callback_data_utils.is_team_2_will_go_through_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_team_2_will_go_through_callback_data(call.data)
            process_who_will_go_through_bet(
                user_id=user.id,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                event_uuid=event_uuid,
                team_1_will_go_through=False
            )
        elif callback_data_utils.is_set_joker_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_set_joker_callback_data(call.data)
            success = set_joker_for_event(
                user=user,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                event_uuid=event_uuid,
            )
            if success:
                send_joker_status_message(chat_id=chat_id, user_id=user.id)
        elif callback_data_utils.is_set_joker_button(call.data):
            send_set_joker_selection_message(chat_id=chat_id, user_id=user.id)
        elif callback_data_utils.is_remove_joker_button(call.data):
            send_remove_joker_selection_message(chat_id=chat_id, user_id=user.id)
        elif callback_data_utils.is_set_specific_joker_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_set_specific_joker_callback_data(call.data)
            success = set_joker_for_event(
                user=user,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                event_uuid=event_uuid,
            )
            if success:
                send_my_bets_message(chat_id=chat_id, user_id=user.id)
        elif callback_data_utils.is_remove_specific_joker_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_remove_specific_joker_callback_data(call.data)
            success = remove_joker_from_event(
                user=user,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                event_uuid=event_uuid,
            )
            if success:
                send_my_bets_message(chat_id=chat_id, user_id=user.id)
        elif callback_data_utils.is_show_my_already_played_bets(call.data):
            all_bets = database.get_all_user_bets(user_id=user.id)
            bets_with_events = []
            for bet in all_bets:
                event = database.get_event_by_uuid(uuid=bet.event_uuid)
                if event is None:
                    continue
                bets_with_events.append((bet, event))

            bets_with_events.sort(key=lambda x: x[1].time, reverse=False)
            bets_played = list(filter(lambda x: x[1].result is not None, bets_with_events))
            if len(bets_played) == 0:
                msg = 'Внезапно, но здесь пусто.'
                bot.send_message(chat_id=call.message.chat.id, text=msg)
                return

            text = ''
            for (bet, event) in bets_played:
                text += (f'{event.team_1} – {event.team_2} ({event.get_time_in_moscow_zone().strftime('%d %b')}): '
                         f'{event.result.team_1_scores}:{event.result.team_2_scores} '
                         f'(прогноз {bet.team_1_scores}:{bet.team_2_scores}')
                need_to_show_who_will_go_through = (bet.team_1_will_go_through is not None and
                                                    (event.event_type == EventType.PLAY_OFF_SECOND_MATCH or
                                                     bet.is_bet_on_draw())
                                                    )
                if need_to_show_who_will_go_through:
                    if bet.team_1_will_go_through:
                        text += f', проход {event.team_1}'
                    else:
                        text += f', проход {event.team_2}'
                if bet.is_joker:
                    text += ', джокер'
                text += ')'
                text += '\n\n'
            telegram_utils.safe_send_message(bot=bot, chat_id=call.message.chat.id, text=text.strip())
        elif callback_data_utils.is_delete_bet_button(call.data):
            all_bets = database.get_all_user_bets(user_id=user.id)
            bets_with_events = []
            for bet in all_bets:
                event = database.get_event_by_uuid(uuid=bet.event_uuid)
                if event is None:
                    continue
                bets_with_events.append((bet, event))

            bets_with_events.sort(key=lambda x: x[1].time, reverse=False)
            bets_awaiting = list(filter(lambda x: x[1].result is None, bets_with_events))
            if len(bets_awaiting) == 0:
                msg = 'Ставок не обнаружено :('
                bot.send_message(chat_id=call.message.chat.id, text=msg)
                return

            text = 'Выбери номер ставки для отмены'
            buttons_list = []
            index = 1
            for (bet, _) in bets_awaiting:
                callback_data = callback_data_utils.create_delete_specific_bet_callback_data(event_uuid=bet.event_uuid)
                button = InlineKeyboardButton(str(index), callback_data=callback_data)
                buttons_list.append(button)
                index += 1
            markup = InlineKeyboardMarkup()
            markup.row(*buttons_list)
            bot.send_message(chat_id=chat_id, text=text.strip(), reply_markup=markup)
        elif callback_data_utils.is_delete_specific_bet_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_delete_specific_bet_callback_data(call.data)
            if not event_uuid:
                bot.send_message(chat_id=chat_id, text='Что-то пошло не так :(')
                return

            existing_event = database.get_event_by_uuid(uuid=event_uuid)
            if existing_event is None:
                bot.send_message(chat_id=chat_id, text=strings.EVENT_NOT_FOUND_ERROR)
                return

            existing_bet = database.find_bet(user_id=user.id, event_uuid=existing_event.uuid)
            if existing_bet is None:
                bot.send_message(chat_id=chat_id, text='Ставка на этот матч не обнаружена.')
                return

            if existing_event.is_started():
                bot.send_message(chat_id=chat_id, text=strings.EVENT_HAS_ALREADY_STARTED)
                return

            database.delete_bet(user_id=user.id, event_uuid=existing_event.uuid)
            if existing_bet.is_joker:
                send_public_joker_removed_message(user=user, event=existing_event)
            text = f'Ставка отменена: {existing_event.team_1} – {existing_event.team_2}.'
            bot.send_message(chat_id=chat_id, text=text)
            send_my_bets_message(chat_id=chat_id, user_id=user.id)


    except Exception as e:
        handle_exception(e=e, user=user, chat_id=chat_id)


def process_who_will_go_through_bet(user_id: int, chat_id: int, message_id: int, event_uuid: str,
                                    team_1_will_go_through: bool):
    event = database.get_event_by_uuid(uuid=event_uuid)
    if event is None:
        bot.send_message(chat_id=chat_id, text=strings.EVENT_NOT_FOUND_ERROR)
        return
    bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
    if bet is None:
        bot.send_message(chat_id=chat_id, text='Что-то пошло не так :/')
        return
    bet.team_1_will_go_through = team_1_will_go_through
    database.update_bet(user_id=user_id, bet=bet)
    msg = f'OK, {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}, проход: '
    if team_1_will_go_through:
        msg += f'{event.team_1}.'
    else:
        msg += f'{event.team_2}.'
    bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=msg
    )
    send_coming_events(user_id=user_id, chat_id=chat_id, send_error_if_all_bets_already_make=False)


@bot.message_handler(content_types=['text'])
def get_text_messages(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        return
    save_user_or_update_interaction(user=user)
    current_event = database.get_current_event_for_user(user_id=user.id)
    if not current_event:
        bot.send_message(chat_id=message.chat.id, text='Чтобы сделать прогноз, используй команду /coming_events.')
        return
    event = database.get_event_by_uuid(uuid=current_event)
    if not event:
        bot.send_message(chat_id=message.chat.id, text='Произошла ошибка. Попробуй повторить с начала.')
        database.clear_current_event_for_user(user_id=user.id)
        return

    wrong_format_msg = 'Укажи счёт в формате \"X:X\" (например, \"1:0\"). Отменить: /clear_context.'
    split_result = message.text.split(':')
    if len(split_result) != 2:
        bot.send_message(chat_id=message.chat.id, text=wrong_format_msg)
        return

    if event.is_started():
        bot.send_message(chat_id=message.chat.id, text=strings.EVENT_HAS_ALREADY_STARTED)
        database.clear_current_event_for_user(user_id=user.id)
        return

    existing_bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
    if existing_bet:
        msg = f'Ты уже сделал прогноз на этот матч ({existing_bet.team_1_scores}:{existing_bet.team_2_scores})'
        bot.send_message(chat_id=message.chat.id, text=msg)
        database.clear_current_event_for_user(user_id=user.id)
        return

    try:
        team_1_scores = int(split_result[0])
        team_2_scores = int(split_result[1])

        match event.event_type:
            case EventType.GROUP_STAGE | EventType.PLAY_OFF_FIRST_MATCH:
                bet = Bet(
                    user_id=user.id,
                    event_uuid=event.uuid,
                    team_1_scores=team_1_scores,
                    team_2_scores=team_2_scores,
                    team_1_will_go_through=None,
                    created_at=datetime.now(timezone.utc),
                    is_joker=False,
                )
                database.add_bet(user_id=user.id, bet=bet)
                database.clear_current_event_for_user(user_id=user.id)
                bot.send_message(chat_id=message.chat.id,
                                 text=f'Принято: {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}')
                send_joker_status_message(chat_id=message.chat.id, user_id=user.id, event=event)
                send_coming_events(user_id=user.id, chat_id=message.chat.id, send_error_if_all_bets_already_make=False)
            case EventType.PLAY_OFF_SINGLE_MATCH:
                if team_1_scores > team_2_scores:
                    team_1_will_go_through = True
                elif team_1_scores < team_2_scores:
                    team_1_will_go_through = False
                else:
                    # оставляем None до момента уточнения юзером, кто пройдёт дальше.
                    team_1_will_go_through = None
                bet = Bet(
                    user_id=user.id,
                    event_uuid=event.uuid,
                    team_1_scores=team_1_scores,
                    team_2_scores=team_2_scores,
                    team_1_will_go_through=team_1_will_go_through,
                    created_at=datetime.now(timezone.utc),
                    is_joker=False,
                )
                database.add_bet(user_id=user.id, bet=bet)
                database.clear_current_event_for_user(user_id=user.id)
                if team_1_will_go_through is None:
                    buttons_list = []
                    callback_data_1 = callback_data_utils.create_team_1_will_go_through_callback_data(event)
                    button_1 = InlineKeyboardButton(event.team_1, callback_data=callback_data_1)
                    buttons_list.append(button_1)
                    callback_data_2 = callback_data_utils.create_team_2_will_go_through_callback_data(event)
                    button_2 = InlineKeyboardButton(event.team_2, callback_data=callback_data_2)
                    buttons_list.append(button_2)
                    markup = InlineKeyboardMarkup()
                    markup.row(*buttons_list)
                    bot.send_message(
                        chat_id=message.chat.id,
                        text=f'Принято: {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}. '
                             f'Кто пройдёт дальше?',
                        reply_markup=markup
                    )
                    send_joker_status_message(chat_id=message.chat.id, user_id=user.id, event=event)
                else:
                    bot.send_message(chat_id=message.chat.id,
                                     text=f'Принято: {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}')
                    send_joker_status_message(chat_id=message.chat.id, user_id=user.id, event=event)
                    send_coming_events(user_id=user.id, chat_id=message.chat.id,
                                       send_error_if_all_bets_already_make=False)

            case EventType.PLAY_OFF_SECOND_MATCH:
                bet = Bet(
                    user_id=user.id,
                    event_uuid=event.uuid,
                    team_1_scores=team_1_scores,
                    team_2_scores=team_2_scores,
                    team_1_will_go_through=None,
                    created_at=datetime.now(timezone.utc),
                    is_joker=False,
                )
                database.add_bet(user_id=user.id, bet=bet)
                database.clear_current_event_for_user(user_id=user.id)
                buttons_list = []
                callback_data_1 = callback_data_utils.create_team_1_will_go_through_callback_data(event)
                button_1 = InlineKeyboardButton(event.team_1, callback_data=callback_data_1)
                buttons_list.append(button_1)
                callback_data_2 = callback_data_utils.create_team_2_will_go_through_callback_data(event)
                button_2 = InlineKeyboardButton(event.team_2, callback_data=callback_data_2)
                buttons_list.append(button_2)
                markup = InlineKeyboardMarkup()
                markup.row(*buttons_list)
                bot.send_message(
                    chat_id=message.chat.id,
                    text=f'Принято: {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}. '
                         f'Кто пройдёт дальше?',
                    reply_markup=markup
                )
                send_joker_status_message(chat_id=message.chat.id, user_id=user.id, event=event)

        msg_for_everybody = f'{user.full_name} сделал прогноз на матч {event.team_1} – {event.team_2}'
        bot.send_message(chat_id=get_target_chat_id(), text=msg_for_everybody)
    except:
        bot.send_message(chat_id=message.chat.id, text=wrong_format_msg)
        return


def is_club_member(user: User) -> bool:
    return telegram_utils.is_chat_member(bot=bot, chat_id=get_target_chat_id(), user_id=user.id)


def save_user_or_update_interaction(user: User):
    inserted_new = database.register_user_if_required(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    if not inserted_new:
        database.update_last_interaction(user_id=user.id)
    else:
        for user_id in get_maintainer_ids():
            bot.send_message(chat_id=user_id, text=f'New user: {user.full_name} ({user.username})')


def handle_exception(e: Exception, user: User, chat_id: int):
    logging.exception(e)
    bot.send_message(chat_id=chat_id, text='Что-то пошло не так, произошла ошибка :(')
    from_user = f'{user.full_name} (f{user.username}). '
    error_message = 'Произошла ошибка. ' + ''.join(traceback.TracebackException.from_exception(e).format())
    for user_id in get_maintainer_ids():
        bot.send_message(chat_id=user_id, text=from_user + error_message)


def send_coming_events(user_id: int, chat_id: int, send_error_if_all_bets_already_make: bool = True):
    events = database.get_all_events()
    coming_events = []
    for event in events:
        event_time = event.time.replace(tzinfo=timezone.utc)
        if event_time <= datetime.now(timezone.utc):
            continue
        coming_events.append(event)

    status_text = joker_utils.get_joker_status_text(get_joker_status_for_user(user_id=user_id))
    if len(coming_events) == 0:
        bot.send_message(chat_id=chat_id, text=f'{status_text}\n\nМатчей не обнаружено')
        return

    text = f'{status_text}\n\nДжокер можно поставить после прогноза или через /my_bets.\n\n'
    index = 0
    max_events_in_message = 10
    events_available_for_bet_with_index = []
    for event in coming_events:
        existing_bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
        if existing_bet is not None:
            continue
        index += 1
        event_time = datetime_utils.to_display_string(event.get_time_in_moscow_zone())
        text += f'{index}. {event.team_1} – {event.team_2}, {event_time}'
        events_available_for_bet_with_index.append((index, event))
        text += '\n\n'
        if len(events_available_for_bet_with_index) == max_events_in_message:
            break

    if len(events_available_for_bet_with_index) == 0:
        if send_error_if_all_bets_already_make:
            bot.send_message(chat_id=chat_id, text=f'{status_text}\n\nНа все предстоящие матчи прогноз уже сделан.')
        return

    text += '\nВыбери матч для прогноза'
    buttons_list = []
    for (i, event) in events_available_for_bet_with_index:
        callback_data = callback_data_utils.create_make_bet_callback_data(event)
        button = InlineKeyboardButton(i, callback_data=callback_data)
        buttons_list.append(button)

    markup = InlineKeyboardMarkup()
    markup.row(*buttons_list)
    bot.send_message(chat_id=chat_id, text=text.strip(), reply_markup=markup)


def send_my_bets_message(chat_id: int, user_id: int):
    bets_with_events = get_user_bets_with_events(user_id=user_id)
    status_text = joker_utils.get_joker_status_text(get_joker_status_for_user(user_id=user_id))
    if len(bets_with_events) == 0:
        bot.send_message(chat_id=chat_id, text=f'{status_text}\n\nПока ничего нет. Начни с команды /coming_events.')
        return

    bets_played = list(filter(lambda x: x[1].result is not None, bets_with_events))
    bets_awaiting = list(filter(lambda x: x[1].result is None, bets_with_events))

    text = f'{status_text}\n\n'
    if len(bets_awaiting) > 0:
        index = 1
        for (bet, event) in bets_awaiting:
            text += f'{index}. '
            index += 1
            text += (f'{event.team_1} – {event.team_2} ({event.get_time_in_moscow_zone().strftime('%d %b')}): '
                     f'{bet.team_1_scores}:{bet.team_2_scores}')
            if bet.is_joker:
                text += ' (джокер)'
            need_to_show_who_will_go_through = (bet.team_1_will_go_through is not None and
                                                (event.event_type == EventType.PLAY_OFF_SECOND_MATCH or
                                                 bet.is_bet_on_draw())
                                                )
            if need_to_show_who_will_go_through:
                if bet.team_1_will_go_through:
                    text += f' (проход {event.team_1})'
                else:
                    text += f' (проход {event.team_2})'
            text += '\n\n'
    else:
        text += 'Пока ничего нет. Начни с команды /coming_events.'

    reply_markup = InlineKeyboardMarkup()
    if len(bets_played) > 0:
        callback_data_already_played = callback_data_utils.create_show_my_already_played_bets()
        already_played_button = InlineKeyboardButton(
            text='Показать разыгранные',
            callback_data=callback_data_already_played
        )
        reply_markup.add(already_played_button)

    if len(bets_awaiting) > 0:
        callback_data_delete_bet = callback_data_utils.create_delete_bet_button()
        delete_bet_button = InlineKeyboardButton(text='Отменить ставку', callback_data=callback_data_delete_bet)
        reply_markup.add(delete_bet_button)
        awaiting_bets = get_awaiting_bets_with_index(user_id=user_id)
        can_set_joker = False
        can_remove_joker = False
        all_events = database.get_all_events()
        for _, bet, event in awaiting_bets:
            if not can_set_joker and joker_utils.can_assign_joker_to_bet(
                    bet=bet,
                    event=event,
                    bets_with_events=bets_with_events,
                    events=all_events,
                    now_utc=datetime_utils.get_utc_time(),
            ):
                can_set_joker = True
            if not can_remove_joker and joker_utils.can_remove_joker_from_bet(
                    bet=bet,
                    event=event,
                    now_utc=datetime_utils.get_utc_time(),
            ):
                can_remove_joker = True
        if can_set_joker:
            reply_markup.add(
                InlineKeyboardButton(
                    text='Поставить джокер',
                    callback_data=callback_data_utils.create_set_joker_button()
                )
            )
        if can_remove_joker:
            reply_markup.add(
                InlineKeyboardButton(
                    text='Снять джокер',
                    callback_data=callback_data_utils.create_remove_joker_button()
                )
            )
    bot.send_message(chat_id=chat_id, text=text.strip(), reply_markup=reply_markup)


def calculate_scores_after_finished_event(event: Event) -> Guessers:
    guessed_total_score = []
    guessed_goal_difference = []
    guessed_draw = []
    guessed_only_winner = []
    guessed_who_has_gone_through = []

    result = event.result
    if result is None:
        raise ValueError('Event does not have result')
    users = database.get_all_users()
    for user_model in users:
        user_id = user_model.id
        bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
        if bet is None:
            continue
        guessed_result = calculate_if_user_guessed_result(event_result=result, bet=bet)
        scores_earned = 0
        if guessed_result is not None:
            base_scores = convert_guessed_event_to_scores(guessed_result)
            scores_earned = joker_utils.calculate_scores_with_joker(
                base_scores=base_scores,
                is_joker=bet.is_joker,
            )
            match guessed_result:
                case GuessedEvent.WINNER:
                    guessed_only_winner.append(user_model)
                case GuessedEvent.DRAW:
                    guessed_draw.append(user_model)
                case GuessedEvent.GOAL_DIFFERENCE:
                    guessed_goal_difference.append(user_model)
                case GuessedEvent.EXACT_SCORE:
                    guessed_total_score.append(user_model)

        if event.decides_who_goes_through():
            # Также можно получить +1 очко за проход одной из команд. Независимо от первой ставки.
            if is_guessed_who_has_gone_through(result=result, bet=bet):
                scores_earned += 1
                guessed_who_has_gone_through.append(user_model)

        if scores_earned > 0:
            database.add_scores_to_user(user_id=user_id, amount=scores_earned)

    return Guessers(
        guessed_total_score=guessed_total_score,
        guessed_draw=guessed_draw,
        guessed_goal_difference=guessed_goal_difference,
        guessed_only_winner=guessed_only_winner,
        guessed_who_has_gone_through=guessed_who_has_gone_through,
    )


def convert_guessed_event_to_scores(guessed_event: GuessedEvent) -> int:
    match guessed_event:
        case GuessedEvent.WINNER:
            return 1
        case GuessedEvent.DRAW:
            return 2
        case GuessedEvent.GOAL_DIFFERENCE:
            return 3
        case GuessedEvent.EXACT_SCORE:
            return 4
        case _:
            raise ValueError(f'Unknown enum value: {guessed_event}')


def calculate_if_user_guessed_result(event_result: EventResult, bet: Bet) -> GuessedEvent | None:
    if is_exact_score(result=event_result, bet=bet):
        return GuessedEvent.EXACT_SCORE
    elif is_guessed_draw(result=event_result, bet=bet):
        return GuessedEvent.DRAW
    elif is_same_goal_difference(result=event_result, bet=bet):
        return GuessedEvent.GOAL_DIFFERENCE
    elif is_same_winner(result=event_result, bet=bet):
        return GuessedEvent.WINNER
    else:
        return None


def is_exact_score(result: EventResult, bet: Bet) -> bool:
    return result.team_1_scores == bet.team_1_scores and result.team_2_scores == bet.team_2_scores


def is_guessed_draw(result: EventResult, bet: Bet) -> bool:
    return result.team_1_scores == result.team_2_scores and bet.team_1_scores == bet.team_2_scores


def is_same_goal_difference(result: EventResult, bet: Bet) -> bool:
    return result.team_1_scores - result.team_2_scores == bet.team_1_scores - bet.team_2_scores


def is_same_winner(result: EventResult, bet: Bet) -> bool:
    if result.team_1_scores > result.team_2_scores:
        return bet.team_1_scores > bet.team_2_scores
    if result.team_1_scores < result.team_2_scores:
        return bet.team_1_scores < bet.team_2_scores
    return bet.team_1_scores == bet.team_2_scores


def is_guessed_who_has_gone_through(result: EventResult, bet: Bet) -> bool:
    if result.team_1_has_gone_through is None or bet.team_1_will_go_through is None:
        return False
    return result.team_1_has_gone_through == bet.team_1_will_go_through


def is_one_goal_from_total_score_winner_consider(event_result: EventResult, bet: Bet) -> bool:
    if not is_same_winner(event_result, bet):
        return False
    elif event_result.team_1_scores == bet.team_1_scores:
        return bet.team_2_scores in [event_result.team_2_scores - 1, event_result.team_2_scores + 1]
    elif event_result.team_2_scores == bet.team_2_scores:
        return bet.team_1_scores in [event_result.team_1_scores - 1, event_result.team_1_scores + 1]
    else:
        return False


def is_one_goal_from_total_score_with_two_or_more_scores(event_result: EventResult, bet: Bet) -> bool:
    if event_result.team_1_scores + event_result.team_2_scores < 2:
        return False
    if event_result.team_1_scores == bet.team_1_scores:
        return bet.team_2_scores in [event_result.team_2_scores - 1, event_result.team_2_scores + 1]
    elif event_result.team_2_scores == bet.team_2_scores:
        return bet.team_1_scores in [event_result.team_1_scores - 1, event_result.team_1_scores + 1]
    else:
        return False


def get_leaderboard_text() -> str:
    users = database.get_all_users()
    users.sort(key=lambda x: x.scores, reverse=True)
    users_dict = {}
    for user in users:
        if user.scores not in users_dict:
            users_dict[user.scores] = []
        users_dict[user.scores].append(user)
    scores_sorted = list(users_dict.keys())
    scores_sorted.sort(key=lambda x: x, reverse=True)
    text = ''
    for score in scores_sorted:
        current_line = ', '.join(list(map(lambda x: x.get_full_name(), users_dict[score])))
        current_line += f': {score}'
        text += current_line.strip()
        text += '\n'
    return text.strip()


def get_users_detailed_statistic_text() -> str:
    users = database.get_all_users()
    users.sort(key=lambda x: x.scores, reverse=True)
    users_dict = {}
    for user in users:
        if user.scores not in users_dict:
            users_dict[user.scores] = []
        users_dict[user.scores].append(user)
    scores_sorted = list(users_dict.keys())
    scores_sorted.sort(key=lambda x: x, reverse=True)
    text = 'Подробная аналитика по набранным очкам (указывается число матчей):\n\n'
    for score in scores_sorted:
        for user in users_dict[score]:
            user_statistic = get_user_detailed_statistic(user_model=user)
            user_text = get_user_statistic_formatted_text(statistic=user_statistic)
            text += user_text
            text += '\n\n'
    return text.strip()


def get_matches_result_statistic_text() -> str:
    events = database.get_all_events()
    total_matches = 0
    draws_count = 0
    goals_scored = 0
    for event in events:
        result = event.result
        if result is None:
            continue
        total_matches += 1
        goals_scored += result.team_1_scores + result.team_2_scores
        if result.is_draw():
            draws_count += 1

    average_goals_per_match = goals_scored / total_matches
    text = 'Аналитика по матчам:\n\n'
    text += f'Всего сыграно: {total_matches}\n'
    text += f'Из них ничьих: {draws_count}\n'
    text += f'Голов забито в основное время: {goals_scored}\n'
    text += f'В среднем голов в основное время: {average_goals_per_match:.1f}\n'
    return text.strip()


def get_user_detailed_statistic(user_model: UserModel) -> DetailedStatistic:
    guessed_total_score_count = 0
    guessed_goal_difference_count = 0
    guessed_draw_count = 0
    guessed_only_winner_count = 0
    guessed_who_has_gone_through_count = 0
    one_goal_from_total_score_count_with_winner_consider = 0
    one_goal_from_total_score_count_exclude_winner = 0
    events = database.get_all_events()
    for event in events:
        result = event.result
        if result is None:
            continue
        user_bet = database.find_bet(user_id=user_model.id, event_uuid=event.uuid)
        if user_bet is None:
            continue
        is_guessed_event = calculate_if_user_guessed_result(event_result=result, bet=user_bet)
        match is_guessed_event:
            case GuessedEvent.WINNER:
                guessed_only_winner_count += 1
            case GuessedEvent.GOAL_DIFFERENCE:
                guessed_goal_difference_count += 1
            case GuessedEvent.DRAW:
                guessed_draw_count += 1
            case GuessedEvent.EXACT_SCORE:
                guessed_total_score_count += 1
        if event.decides_who_goes_through() and is_guessed_who_has_gone_through(result=result, bet=user_bet):
            guessed_who_has_gone_through_count += 1
        if is_one_goal_from_total_score_winner_consider(event_result=result, bet=user_bet):
            one_goal_from_total_score_count_with_winner_consider += 1
        elif is_one_goal_from_total_score_with_two_or_more_scores(event_result=result, bet=user_bet):
            one_goal_from_total_score_count_exclude_winner += 1
    return DetailedStatistic(
        user_model=user_model,
        guessed_total_score_count=guessed_total_score_count,
        guessed_goal_difference_count=guessed_goal_difference_count,
        guessed_draw_count=guessed_draw_count,
        guessed_only_winner_count=guessed_only_winner_count,
        guessed_who_has_gone_through_count=guessed_who_has_gone_through_count,
        one_goal_from_total_score_count_with_winner_consider=one_goal_from_total_score_count_with_winner_consider,
        one_goal_from_total_score_count_exclude_winner=one_goal_from_total_score_count_exclude_winner,
    )


def get_user_statistic_formatted_text(statistic: DetailedStatistic) -> str:
    text = f'{statistic.user_model.get_full_name()}:\n'
    text += f'Точный счёт: {statistic.guessed_total_score_count}\n'
    text += f'Разница мячей: {statistic.guessed_goal_difference_count}\n'
    text += f'Ничьи: {statistic.guessed_draw_count}\n'
    text += f'Победитель: {statistic.guessed_only_winner_count}\n'
    text += f'Проходы: {statistic.guessed_who_has_gone_through_count}\n'
    text += f'В одном мяче от ТС с учётом исхода матча: {statistic.one_goal_from_total_score_count_with_winner_consider}\n'
    text += f'В одном мяче от ТС в иных случаях (только матчи с двумя и более голами): {statistic.one_goal_from_total_score_count_exclude_winner}\n'
    return text.strip()


def get_target_chat_id() -> int:
    return int(os.environ[constants.ENV_TARGET_CHAT_ID])


def is_maintainer(user: User) -> bool:
    return user.id in get_maintainer_ids()


def get_maintainer_ids() -> list:
    value = os.environ[constants.ENV_MAINTAINER_IDS]
    if not value:
        return []
    return list(map(lambda x: int(x), value.split(',')))


def run_scheduler():
    schedule.every(10).minutes.do(do_every_ten_minutes)
    while True:
        # Любое исключение из плановой задачи (например, отправка заблокировавшему бота пользователю)
        # не должно убивать поток-планировщик и останавливать все остальные рассылки.
        try:
            schedule.run_pending()
        except Exception as e:
            logging.exception(e)
        time.sleep(1)


def do_every_ten_minutes():
    send_morning_message_with_games_today()
    check_coming_soon_events()
    check_for_night_events()
    check_for_unfinished_events()
    check_playoff_joker_reminders()
    check_tournament_end_joker_reminders()
    check_for_burned_jokers_after_playoff_start()


scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.start()


def send_morning_message_with_games_today():
    if not is_now_good_time_for_morning_message():
        return
    from_time = datetime_utils.get_utc_time()
    to_time = from_time + timedelta(hours=24)
    events_today = database.find_events_in_time_range(from_inclusive=from_time, to_exclusive=to_time)
    if len(events_today) == 0:
        return
    text = 'Доброе утро! Сегодня у нас:\n\n'
    for event in events_today:
        match_time = event.get_time_in_moscow_zone().strftime('%H:%M')
        text += f'{event.team_1} – {event.team_2} в {match_time}'
        text += '\n'

    from_time = to_time
    to_time = from_time + timedelta(hours=24)
    events_tomorrow = database.find_events_in_time_range(from_inclusive=from_time, to_exclusive=to_time)
    if len(events_tomorrow) > 0:
        text += '\n'
        text += 'Завтра:\n\n'
        for event in events_tomorrow:
            match_time = event.get_time_in_moscow_zone().strftime('%H:%M')
            text += f'{event.team_1} – {event.team_2} в {match_time}'
            text += '\n'
    text += '\n-----\n'
    text += get_leaderboard_text()
    bot.send_message(chat_id=get_target_chat_id(), text=text.strip())


def is_now_good_time_for_morning_message() -> bool:
    moscow_time = datetime_utils.get_moscow_time()
    return moscow_time.hour == 9 and moscow_time.minute in range(10, 20)  # Отправляем в интервале 9:10 – 9:20


def check_coming_soon_events():
    now_utc = datetime_utils.get_utc_time()
    coming_soon_events = database.find_events_in_time_range(
        from_inclusive=now_utc + timedelta(hours=1, minutes=40),
        to_exclusive=now_utc + timedelta(hours=1, minutes=50)  # интервал должен быть 10 минут!
    )
    for event in coming_soon_events:
        match_time = event.get_time_in_moscow_zone().strftime('%H:%M')
        header = f'❗️Матч {event.team_1} – {event.team_2} начнётся в {match_time}, но не все сделали прогноз:'
        send_event_will_start_soon_warning(event_uuid=event.uuid, header_text=header)

    coming_very_soon_events = database.find_events_in_time_range(
        from_inclusive=now_utc + timedelta(minutes=5),
        to_exclusive=now_utc + timedelta(minutes=15)  # интервал должен быть 10 минут!
    )
    for event in coming_very_soon_events:
        header = f'‼️ LAST CALL ‼️'
        send_event_will_start_soon_warning(event_uuid=event.uuid, header_text=header)


def check_for_night_events():
    # Проверяем ночные матчи в интервале 20:40 – 20:50
    moscow_time = datetime_utils.get_moscow_time()
    if moscow_time.hour != 20 or moscow_time.minute not in range(40, 50):
        return

    # Проверяем все матчи, которые начнутся с 00:40(00:50) до 08:40(08:50).
    event_datetime_utc_start = datetime_utils.get_utc_time() + timedelta(hours=4)
    event_datetime_utc_end = event_datetime_utc_start + timedelta(hours=8)

    coming_soon_night_events = database.find_events_in_time_range(
        from_inclusive=event_datetime_utc_start,
        to_exclusive=event_datetime_utc_end,
    )

    if len(coming_soon_night_events) == 0:
        return

    all_users = database.get_all_users()
    already_mentioned_users = set()
    text = 'Не забудьте про ночные матчи!\n\n'

    for event in coming_soon_night_events:
        users_without_bets = list(
            filter(lambda x: database.find_bet(user_id=x.id, event_uuid=event.uuid) is None, all_users)
        )
        for user in users_without_bets:
            if user in already_mentioned_users:
                continue
            already_mentioned_users.add(user)
            if user.username:
                text += f'@{user.username}'
            else:
                text += user.first_name
            text += ' '

    if len(already_mentioned_users) > 0:
        bot.send_message(chat_id=get_target_chat_id(), text=text.strip())


def send_event_will_start_soon_warning(event_uuid: str, header_text: str):
    all_users = database.get_all_users()
    without_bets = list(filter(lambda x: database.find_bet(user_id=x.id, event_uuid=event_uuid) is None, all_users))
    if len(without_bets) == 0:
        return
    text = header_text
    text += '\n'
    for user in without_bets:
        if user.username:
            text += f'@{user.username}'
        else:
            text += user.first_name
        text += '\n'
    bot.send_message(chat_id=get_target_chat_id(), text=text.strip())


def get_all_users_with_joker_status() -> list[tuple[UserModel, joker_utils.JokerStatus]]:
    all_events = database.get_all_events()
    now_utc = datetime_utils.get_utc_time()
    result = []
    for user_model in database.get_all_users():
        status = joker_utils.calculate_joker_status(
            bets_with_events=get_user_bets_with_events(user_id=user_model.id),
            events=all_events,
            now_utc=now_utc,
        )
        result.append((user_model, status))
    return result


def send_joker_threshold_reminder_if_due(target_time, now_utc, key_prefix, send_reminder):
    # Надёжно к дрейфу планировщика: напоминание срабатывает на ПЕРВОМ тике после того, как порог
    # (за 48/24/6 ч до target_time) пройден, а не в узком 10-минутном окне, которое тик может проскочить.
    # Разовость гарантирует database.claim_reminder (ключ привязан к target_time, поэтому при сдвиге
    # расписания напоминание корректно переоценивается заново).
    crossed_hours = [
        hours_before for hours_before in joker_utils.REMINDER_HOURS
        if now_utc >= target_time - timedelta(hours=hours_before)
    ]
    if len(crossed_hours) == 0:
        return
    most_urgent_hours = min(crossed_hours)
    if database.claim_reminder(f'{key_prefix}:{most_urgent_hours}:{target_time.isoformat()}'):
        send_reminder(hours_before=most_urgent_hours)
    # Гасим менее срочные пройденные пороги (например, при старте бота уже после окна 48 ч),
    # чтобы они не сработали отдельным запоздалым сообщением.
    for hours_before in crossed_hours:
        if hours_before != most_urgent_hours:
            database.claim_reminder(f'{key_prefix}:{hours_before}:{target_time.isoformat()}')


def check_playoff_joker_reminders():
    all_events = database.get_all_events()
    playoff_start = joker_utils.get_playoff_start(all_events)
    now_utc = datetime_utils.get_utc_time()
    if playoff_start is None or now_utc >= playoff_start:
        return
    send_joker_threshold_reminder_if_due(
        target_time=playoff_start,
        now_utc=now_utc,
        key_prefix='playoff_joker',
        send_reminder=send_playoff_joker_reminders,
    )


def send_joker_reminder_message(chat_id: int, text: str):
    # Best-effort: пользователь мог заблокировать бота или ни разу не открыть приватный чат
    # (bot.send_message бросит ApiTelegramException). Один такой получатель не должен прерывать
    # рассылку остальным и публичное сообщение, тем более что маркер claim_reminder уже записан.
    try:
        bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logging.exception(e)


def send_playoff_joker_reminders(hours_before: int):
    affected_users = []
    for user_model, status in get_all_users_with_joker_status():
        if status.will_burn_at_playoff_start <= 0:
            continue
        affected_users.append(user_model)
        text = (f'Напоминание о джокерах: до старта плей-офф меньше {hours_before} часов.\n\n'
                f'{joker_utils.get_joker_status_text(status)}')
        send_joker_reminder_message(chat_id=user_model.id, text=text)

    if len(affected_users) == 0:
        return

    mentions = ' '.join(map(get_user_mention, affected_users))
    text = (f'Напоминание о джокерах перед плей-офф:\n{mentions}\n'
            f'Если не потратить джокеры на матчи группового этапа, часть сгорит к старту плей-офф.')
    send_joker_reminder_message(chat_id=get_target_chat_id(), text=text)


def check_tournament_end_joker_reminders():
    all_events = database.get_all_events()
    tournament_end = joker_utils.get_tournament_end(all_events)
    now_utc = datetime_utils.get_utc_time()
    if tournament_end is None or now_utc >= tournament_end:
        return
    send_joker_threshold_reminder_if_due(
        target_time=tournament_end,
        now_utc=now_utc,
        key_prefix='tournament_end_joker',
        send_reminder=send_tournament_end_joker_reminders,
    )


def send_tournament_end_joker_reminders(hours_before: int):
    affected_users = []
    for user_model, status in get_all_users_with_joker_status():
        if status.remaining_usable_now <= 0:
            continue
        affected_users.append(user_model)
        text = (f'Напоминание о джокерах: до конца турнира меньше {hours_before} часов.\n\n'
                f'{joker_utils.get_joker_status_text(status)}')
        send_joker_reminder_message(chat_id=user_model.id, text=text)

    if len(affected_users) == 0:
        return

    mentions = ' '.join(map(get_user_mention, affected_users))
    text = (f'Напоминание о джокерах перед концом турнира:\n{mentions}\n'
            f'У вас остались неиспользованные джокеры.')
    send_joker_reminder_message(chat_id=get_target_chat_id(), text=text)


def check_for_burned_jokers_after_playoff_start():
    all_events = database.get_all_events()
    playoff_start = joker_utils.get_playoff_start(all_events)
    now_utc = datetime_utils.get_utc_time()
    if playoff_start is None or now_utc < playoff_start:
        return
    # Срабатывает на первом тике после старта плей-офф и ровно один раз (claim_reminder),
    # без узкого окна, которое дрейф планировщика мог проскочить и потерять сообщение навсегда.
    if not database.claim_reminder(f'burned_at_playoff_start:{playoff_start.isoformat()}'):
        return
    for user_model, status in get_all_users_with_joker_status():
        if status.will_burn_at_playoff_start <= 0:
            continue
        text = (f'Стартовал плей-офф. Сгорело джокеров: {status.will_burn_at_playoff_start}.\n\n'
                f'{joker_utils.get_joker_status_text(status)}')
        send_joker_reminder_message(chat_id=user_model.id, text=text)


def check_for_unfinished_events():
    utc_time = datetime_utils.get_utc_time()
    if utc_time.minute not in range(10, 20):
        return
    all_events = database.get_all_events()
    events_on_progress = list(filter(lambda x: x.is_in_progress(), all_events))
    result_events = list(filter(lambda x: is_event_requires_finish(x), events_on_progress))
    if len(result_events) == 1:
        event = result_events[0]
        msg = f'❗️Матч {event.team_1} – {event.team_2} требует завершения.'
        for user_id in get_maintainer_ids():
            bot.send_message(user_id, text=msg)
    elif len(result_events) > 1:
        msg = f'❗{len(result_events)} матча требуют завершения.'
        for user_id in get_maintainer_ids():
            bot.send_message(user_id, text=msg)


def is_event_requires_finish(unfinished_event: Event) -> bool:
    # Дополнительное время/пенальти возможны только в матчах, где проход определяется самим матчем.
    if unfinished_event.decides_who_goes_through():
        delta_hours = 3
    else:
        delta_hours = 2
    return unfinished_event.get_time_in_utc() + timedelta(hours=delta_hours) < datetime_utils.get_utc_time()


if __name__ == '__main__':
    bot.infinity_polling()
