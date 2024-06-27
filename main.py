import locale
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta

import pytz
import schedule
import telebot
from telebot.types import User, InlineKeyboardMarkup, InlineKeyboardButton

import callback_data_utils
import constants
import datetime_utils
import strings
import telegram_utils
import utils
from database import Database
from models import Event, EventResult, Bet, Guessers

locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
bot = telebot.TeleBot(os.environ[constants.ENV_BOT_TOKEN])
database = Database()
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
# Формат сообщения для группы: "Германия; Шотландия; 14.06.2024 22:00"
# Формат сообщения для плей-офф: "Германия; Шотландия; 14.06.2024 22:00; playoff"
@bot.message_handler(commands=['add_event'])
def add_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    split = list(map(lambda x: x.strip(), message.text.removeprefix('/add_event').strip().split(';')))
    team_1 = split[0]
    team_2 = split[1]

    date_format = '%d.%m.%Y %H:%M'
    datetime_obj = datetime.strptime(split[2], date_format)

    is_playoff = len(split) >= 4 and split[3] == 'playoff'

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
        is_playoff=is_playoff,
    )
    database.add_event(event)
    bot.send_message(chat_id=message.chat.id, text=strings.EVENT_HAS_BEEN_ADDED)


# Service method
# Формат сообщения: "916dbd19-7d2c-46b6-a96a-0f726a22ec9c 2:1"
@bot.message_handler(commands=['set_result'])
def set_result_for_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    split = list(map(lambda x: x.strip(), message.text.removeprefix('/set_result').strip().split(' ')))
    if len(split) != 2:
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
        msg = f'У матча уже записан результат ({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores})'
        bot.send_message(chat_id=message.chat.id, text=msg)
        return

    existing_event.result = EventResult(
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores
    )
    database.update_event(event=existing_event)
    calculate_scores_after_finished_event(event=existing_event)
    bot.send_message(
        chat_id=message.chat.id,
        text=f'OK, {existing_event.team_1} – {existing_event.team_2} ' +
             f'{existing_event.result.team_1_scores}:{existing_event.result.team_2_scores}'
    )
    msg_text = (f'Основное время матча {existing_event.team_1} – {existing_event.team_2} завершилось ' +
                f'({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores})')
    msg_text += '\n\n'

    guessers = get_users_guessed_event_result(eventUuid=existing_event.uuid, result=existing_event.result)
    if len(guessers.guessed_total_score) == 0 and len(guessers.guessed_only_winner) == 0:
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

        if len(guessers.guessed_only_winner) == 1:
            msg_text += f'{guessers.guessed_only_winner[0].get_full_name()} угадал победителя.'
            msg_text += '\n\n'
        elif len(guessers.guessed_only_winner) > 1:
            msg_text += 'Угадали победителя:'
            msg_text += '\n'
            for user_model in guessers.guessed_only_winner:
                msg_text += user_model.get_full_name()
                msg_text += '\n'
            msg_text += '\n'

    msg_text += '---\n'
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
        if idx > 0 and event_result is None and events[idx - 1].result is not None:
            text += '-----'
            text += '\n\n'
        text += f"{event.team_1} – {event.team_2}, {datetime_utils.to_display_string(event.get_time_in_moscow_zone())}"
        if event_result is not None:
            text += f' ({event_result.team_1_scores}:{event_result.team_2_scores})'
        text += '\n'
        text += event.uuid
        text += '\n\n'
    telegram_utils.safe_send_message(bot=bot, user_id=message.chat.id, text=text.strip())


@bot.message_handler(commands=['coming_events'])
def get_coming_events(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    send_coming_events(user=user, chat_id=message.chat.id)


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
def get_coming_events(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    all_bets = database.get_all_user_bets(user_id=user.id)
    bets_with_events = []
    for bet in all_bets:
        event = database.get_event_by_uuid(uuid=bet.event_uuid)
        if event is None:
            continue
        bets_with_events.append((bet, event))

    if len(bets_with_events) == 0:
        msg = 'Пока ничего нет. Начни с команды /coming_events.'
        bot.send_message(chat_id=message.chat.id, text=msg)
        return
    bets_with_events.sort(key=lambda x: x[1].time, reverse=False)

    bets_played = list(filter(lambda x: x[1].result is not None, bets_with_events))
    bets_awaiting = list(filter(lambda x: x[1].result is None, bets_with_events))

    text = ''
    if len(bets_played) > 0:
        text += 'Разыгранные:\n\n'
        for (bet, event) in bets_played:
            text += (f'{event.team_1} – {event.team_2} ({event.get_time_in_moscow_zone().strftime('%d %b')}): '
                     f'{event.result.team_1_scores}:{event.result.team_2_scores} '
                     f'(прогноз {bet.team_1_scores}:{bet.team_2_scores})')
            text += '\n\n'

    if len(bets_awaiting) > 0:
        text += '\nОжидающие:\n\n'
        for (bet, event) in bets_awaiting:
            text += (f'{event.team_1} – {event.team_2} ({event.get_time_in_moscow_zone().strftime('%d %b')}): '
                     f'{bet.team_1_scores}:{bet.team_2_scores}')
            text += '\n\n'

    telegram_utils.safe_send_message(bot=bot, user_id=message.chat.id, text=text.strip())


@bot.message_handler(commands=['leaderboard'])
def get_leaderboard(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    bot.send_message(chat_id=message.chat.id, text=get_leaderboard_text())


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
                bot.send_message(chat_id=chat_id, text='Матч не найден :/')
                return
            database.save_current_event_to_user(user_id=user.id, event_uuid=event.uuid)
            msg = (f'Укажи счёт, с которым завершится основное время матча '
                   f'{event.team_1} – {event.team_2}. '
                   f'Формат сообщения: \"X:X\" (например, \"1:0\").')
            bot.send_message(chat_id=chat_id, text=msg)

        elif callback_data_utils.is_team_1_will_go_through_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_team_1_will_go_through_callback_data(call.data)
            event = database.get_event_by_uuid(uuid=event_uuid)
            if event is None:
                bot.send_message(chat_id=chat_id, text='Матч не найден :/')
                return
            bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
            if bet is None:
                bot.send_message(chat_id=chat_id, text='Что-то пошло не так :/')
                return
            bet.team_1_will_go_through = True
            database.update_bet(user_id=user.id, bet=bet)
            msg = f'OK, {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}, проход: {event.team_1}.'
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=msg
            )
        elif callback_data_utils.is_team_2_will_go_through_callback_data(call.data):
            event_uuid = callback_data_utils.extract_uuid_from_team_2_will_go_through_callback_data(call.data)
            event = database.get_event_by_uuid(uuid=event_uuid)
            if event is None:
                bot.send_message(chat_id=chat_id, text='Матч не найден :/')
                return
            bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
            if bet is None:
                bot.send_message(chat_id=chat_id, text='Что-то пошло не так :/')
                return
            bet.team_1_will_go_through = False
            database.update_bet(user_id=user.id, bet=bet)
            msg = f'OK, {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}, проход: {event.team_2}.'
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=msg
            )


    except Exception as e:
        handle_exception(e=e, user=user, chat_id=chat_id)


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
    if len(message.text) != 3:
        bot.send_message(chat_id=message.chat.id, text=wrong_format_msg)
        return

    split_result = message.text.split(':')
    if len(split_result) != 2:
        bot.send_message(chat_id=message.chat.id, text=wrong_format_msg)
        return

    if event.is_started():
        bot.send_message(chat_id=message.chat.id, text=strings.EVENT_HAS_ALREADY_STARTER_YOU_ARE_LATE)
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
        bet = Bet(
            user_id=user.id,
            event_uuid=event.uuid,
            team_1_scores=team_1_scores,
            team_2_scores=team_2_scores,
            team_1_will_go_through=None,
            created_at=datetime.now(timezone.utc)
        )
        database.add_bet(user_id=user.id, bet=bet)
        database.clear_current_event_for_user(user_id=user.id)
        # В случае ничьи нужно также поставить на проход одной из команд (но только для плей-офф).
        if event.is_playoff and team_1_scores == team_2_scores:
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
        else:
            bot.send_message(chat_id=message.chat.id,
                             text=f'Принято: {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}')
            send_coming_events(user=user, chat_id=message.chat.id)
        msg_for_everybody = f'{user.full_name} сделал прогноз на матч {event.team_1} – {event.team_2}'
        bot.send_message(chat_id=get_target_chat_id(), text=msg_for_everybody)
    except:
        bot.send_message(chat_id=message.chat.id, text=wrong_format_msg)
        return


def is_club_member(user: User) -> bool:
    return telegram_utils.is_chat_member(bot=bot, chat_id=get_target_chat_id(), user_id=user.id)


def is_maintainer(user: User) -> bool:
    return user.id == get_maintainer_id()


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
        bot.send_message(chat_id=get_maintainer_id(), text=f'New user: {user.full_name} ({user.username})')


def handle_exception(e: Exception, user: User, chat_id: int):
    logging.exception(e)
    bot.send_message(chat_id=chat_id, text='Что-то пошло не так, произошла ошибка :(')
    from_user = f'{user.full_name} (f{user.username}). '
    error_message = 'Произошла ошибка. ' + ''.join(traceback.TracebackException.from_exception(e).format())
    bot.send_message(chat_id=get_maintainer_id(), text=from_user + error_message)


def send_coming_events(user: User, chat_id: int):
    max_events_in_message = 5
    events = database.get_all_events()
    coming_events = []
    for event in events:
        event_time = event.time.replace(tzinfo=timezone.utc)
        if event_time <= datetime.now(timezone.utc):
            continue
        coming_events.append(event)
        if len(coming_events) >= max_events_in_message:
            break

    if len(coming_events) == 0:
        bot.send_message(chat_id=chat_id, text="Матчей не обнаружено")
        return

    text = ''
    index = 0
    events_available_for_bet_with_index = []
    for event in coming_events:
        index += 1
        text += f'{index}. {event.team_1} – {event.team_2}, {datetime_utils.to_display_string(event.get_time_in_moscow_zone())}'
        existing_bet = database.find_bet(user_id=user.id, event_uuid=event.uuid)
        if existing_bet is not None:
            text += f' (прогноз {existing_bet.team_1_scores}:{existing_bet.team_2_scores})'
        else:
            events_available_for_bet_with_index.append((index, event))
        text += '\n\n'

    if len(events_available_for_bet_with_index) == 0:
        bot.send_message(chat_id=chat_id, text=text.strip())
        return

    text += '\nВыбери матч, на который хотел бы сделать прогноз.'
    buttons_list = []
    for (i, event) in events_available_for_bet_with_index:
        callback_data = callback_data_utils.create_make_bet_callback_data(event)
        button = InlineKeyboardButton(i, callback_data=callback_data)
        buttons_list.append(button)

    markup = InlineKeyboardMarkup()
    markup.row(*buttons_list)
    bot.send_message(chat_id=chat_id, text=text.strip(), reply_markup=markup)


def calculate_scores_after_finished_event(event: Event):
    result = event.result
    if result is None:
        raise ValueError('Event does not have result')
    users = database.get_all_users()
    for user_model in users:
        user_id = user_model.id
        bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
        if bet is None:
            logging.warning(f'User {user_model.username} has no bets on event, using default bet 0:0')
            bet = Bet(
                user_id=user_id,
                event_uuid=event.uuid,
                team_1_scores=0,
                team_2_scores=0,
                team_1_will_go_through=True,
                created_at=datetime.now(timezone.utc)
            )

        scores_earned = 0
        if is_exact_score(result=result, bet=bet):
            scores_earned = 3
        elif is_same_goal_difference(result=result, bet=bet):
            scores_earned = 2
        elif is_same_winner(result=result, bet=bet):
            scores_earned = 1

        if scores_earned > 0:
            database.add_scores_to_user(user_id=user_id, amount=scores_earned)


def is_exact_score(result: EventResult, bet: Bet) -> bool:
    return result.team_1_scores == bet.team_1_scores and result.team_2_scores == bet.team_2_scores


def is_same_goal_difference(result: EventResult, bet: Bet) -> bool:
    return result.team_1_scores - result.team_2_scores == bet.team_1_scores - bet.team_2_scores


def is_same_winner(result: EventResult, bet: Bet) -> bool:
    if result.team_1_scores > result.team_2_scores:
        return bet.team_1_scores > bet.team_2_scores
    if result.team_1_scores < result.team_2_scores:
        return bet.team_1_scores < bet.team_2_scores
    return bet.team_1_scores == bet.team_2_scores


def get_leaderboard_text() -> str:
    users = database.get_all_users()
    users.sort(key=lambda x: x.scores, reverse=True)
    text = ''
    for user in users:
        text += f'{user.get_full_name()}: {user.scores}'
        text += '\n'
    return text.strip()


def get_target_chat_id() -> int:
    return int(os.environ[constants.ENV_TARGET_CHAT_ID])


def get_maintainer_id() -> int:
    return int(os.environ[constants.ENV_MAINTAINER_ID])


def run_scheduler():
    schedule.every().hour.do(do_every_hour)
    while True:
        schedule.run_pending()
        time.sleep(1)


def do_every_hour():
    send_morning_message_if_required()
    check_coming_soon_events()
    check_for_unfinished_events()


scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.start()


def send_morning_message_if_required():
    if datetime_utils.get_moscow_time().hour != 9:
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
    bot.send_message(chat_id=get_target_chat_id(), text=text.strip())


def check_coming_soon_events():
    now_utc = datetime_utils.get_utc_time()
    events_in_1_2_hours = database.find_events_in_time_range(
        from_inclusive=now_utc + timedelta(hours=1),
        to_exclusive=now_utc + timedelta(hours=2)
    )
    if len(events_in_1_2_hours) == 0:
        return
    for event in events_in_1_2_hours:
        send_event_will_start_soon_warning(event)


def send_event_will_start_soon_warning(event: Event):
    all_users = database.get_all_users()
    without_bets = list(filter(lambda x: database.find_bet(user_id=x.id, event_uuid=event.uuid) is None, all_users))
    if len(without_bets) == 0:
        return
    match_time = event.get_time_in_moscow_zone().strftime('%H:%M')
    text = f'❗️Матч {event.team_1} – {event.team_2} начнётся в {match_time}, но не все сделали прогноз:'
    text += '\n'
    for user in without_bets:
        if user.username:
            text += f'@{user.username}'
        else:
            text += user.first_name
        text += '\n'
    bot.send_message(chat_id=get_target_chat_id(), text=text.strip())


def get_users_guessed_event_result(eventUuid: str, result: EventResult) -> Guessers:
    users = database.get_all_users()
    guessed_total_score = []
    guessed_only_winner = []
    for user_model in users:
        user_id = user_model.id
        bet = database.find_bet(user_id=user_id, event_uuid=eventUuid)
        if bet is None:
            continue
        if result.team_1_scores == bet.team_1_scores and result.team_2_scores == bet.team_2_scores:
            guessed_total_score.append(user_model)
        elif is_same_winner(result=result, bet=bet):
            guessed_only_winner.append(user_model)
    return Guessers(guessed_total_score=guessed_total_score, guessed_only_winner=guessed_only_winner)


def check_for_unfinished_events():
    all_events = database.get_all_events()
    unfinished = list(filter(lambda x: is_unfinished_event(x), all_events))
    if len(unfinished) == 1:
        event = unfinished[0]
        msg = f'❗️Матч {event.team_1} – {event.team_2} требует завершения.'
        bot.send_message(get_maintainer_id(), text=msg)
    elif len(unfinished) > 1:
        msg = f'❗{len(unfinished)} матча требуют завершения.'
        bot.send_message(get_maintainer_id(), text=msg)


def is_unfinished_event(event: Event) -> bool:
    in_progress = event.is_started() and not event.is_finished()
    if not in_progress:
        return False
    return event.get_time_in_utc() + timedelta(hours=2) < datetime_utils.get_utc_time()


if __name__ == '__main__':
    bot.infinity_polling()
