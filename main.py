import csv
import locale
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta

import pytz
import requests
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
# Формат сообщения для группы: "Германия; Шотландия; 14.06.2024 22:00; group"
# Формат сообщения для плей-офф: "Германия; Шотландия; 14.06.2024 22:00; playoff"
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

    if split[3] == 'playoff':
        is_playoff = True
    elif split[3] == 'group':
        is_playoff = False
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
        is_playoff=is_playoff,
    )
    database.add_event(event)
    msg = strings.EVENT_HAS_BEEN_ADDED
    msg += '\n'
    msg += f"{event.team_1} – {event.team_2}, {datetime_utils.to_display_string(datetime_obj)}"
    bot.send_message(chat_id=message.chat.id, text=msg)


# Service method
# Формат сообщения: "916dbd19-7d2c-46b6-a96a-0f726a22ec9c 2:1".
# Если это плей-офф, и в основное время была ничья, то указываем сразу после счёта кто прошёл дальше:
# "916dbd19-7d2c-46b6-a96a-0f726a22ec9c 1:1 Испания".
@bot.message_handler(commands=['set_result'])
def set_result_for_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    split = list(map(lambda x: x.strip(), message.text.removeprefix('/set_result').strip().split(' ')))

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
        msg = f'У матча уже записан результат ({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores})'
        bot.send_message(chat_id=message.chat.id, text=msg)
        return

    if existing_event.is_playoff and team_1_scores == team_2_scores and len(split) != 3:
        bot.send_message(chat_id=message.chat.id, text='Не указано, кто прошёл дальше!')
        return

    team_1_has_gone_through = None  # Указываем True/False только в матчах плей-офф (для всех матчей!).
    if existing_event.is_playoff:
        if team_1_scores > team_2_scores:
            team_1_has_gone_through = True
        elif team_1_scores < team_2_scores:
            team_1_has_gone_through = False
        else:
            # В случае ничьи указать прошедшую команду нужно явно.
            team_winner = split[2]
            if team_winner.lower() == existing_event.team_1.lower():
                team_1_has_gone_through = True
            elif team_winner.lower() == existing_event.team_2.lower():
                team_1_has_gone_through = False
            else:
                bot.send_message(chat_id=message.chat.id, text=f'Неизвестная команда: {team_winner}')
                return

    existing_event.result = EventResult(
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores,
        team_1_has_gone_through=team_1_has_gone_through
    )
    database.update_event(event=existing_event)
    guessers = calculate_scores_after_finished_event(event=existing_event)
    msg_text = (f'Матч {existing_event.team_1} – {existing_event.team_2} завершился ' +
                f'({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores}).')
    if existing_event.is_playoff and team_1_scores == team_2_scores:
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
        if idx > 0 and event_result is None and events[idx - 1].result is not None:
            text += '-----'
            text += '\n\n'
        text += f"{event.team_1} – {event.team_2}, {datetime_utils.to_display_string(event.get_time_in_moscow_zone())}"
        if event_result is not None:
            text += f' ({event_result.team_1_scores}:{event_result.team_2_scores})'
        text += '\n'
        text += event.uuid
        text += '\n\n'
    telegram_utils.safe_send_message(bot=bot, chat_id=message.chat.id, text=text.strip())


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
    if len(bets_awaiting) > 0:
        for (bet, event) in bets_awaiting:
            text += (f'{event.team_1} – {event.team_2} ({event.get_time_in_moscow_zone().strftime('%d %b')}): '
                     f'{bet.team_1_scores}:{bet.team_2_scores}')
            if bet.team_1_will_go_through is not None and bet.is_bet_on_draw():
                if bet.team_1_will_go_through:
                    text += f' (проход {event.team_1})'
                else:
                    text += f' (проход {event.team_2})'
            text += '\n\n'
    else:
        text += 'Пока ничего нет. Начни с команды /coming_events.'

    reply_markup = None
    if len(bets_played) > 0:
        callback_data = callback_data_utils.create_show_my_already_played_bets()
        button = InlineKeyboardButton(text='Показать разыгранные', callback_data=callback_data)
        reply_markup = InlineKeyboardMarkup()
        reply_markup.add(button)

    bot.send_message(chat_id=message.chat.id, text=text.strip(), reply_markup=reply_markup)


@bot.message_handler(commands=['leaderboard'])
def get_leaderboard(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    bot.send_message(chat_id=message.chat.id, text=get_leaderboard_text())


# Удалить сделанный прогноз. Формат сообщения: "/delete_bet 65619a74-44b2-4f81-9557-713dec9bfe96".
@bot.message_handler(commands=['delete_bet'])
def delete_bet(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    event_uuid = message.text.removeprefix('/delete_bet').strip()
    if not event_uuid:
        bot.send_message(chat_id=message.chat.id, text=strings.WRONG_MESSAGE_FORMAT_ERROR)
        return

    existing_event = database.get_event_by_uuid(uuid=event_uuid)
    if existing_event is None:
        bot.send_message(chat_id=message.chat.id, text=strings.EVENT_NOT_FOUND_ERROR)
        return

    existing_bet = database.find_bet(user_id=user.id, event_uuid=existing_event.uuid)
    if existing_bet is None:
        bot.send_message(chat_id=message.chat.id, text='Прогноз на этот матч не найден.')
        return
    database.delete_bet(user_id=user.id, event_uuid=existing_event.uuid)
    bot.send_message(chat_id=message.chat.id, text=strings.OK)


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

            text = 'Разыгранные:\n\n'
            for (bet, event) in bets_played:
                text += (f'{event.team_1} – {event.team_2} ({event.get_time_in_moscow_zone().strftime('%d %b')}): '
                         f'{event.result.team_1_scores}:{event.result.team_2_scores} '
                         f'(прогноз {bet.team_1_scores}:{bet.team_2_scores}')
                if bet.team_1_will_go_through is not None and bet.is_bet_on_draw():
                    if bet.team_1_will_go_through:
                        text += f', проход {event.team_1}'
                    else:
                        text += f', проход {event.team_2}'
                text += ')'
                text += '\n\n'
            telegram_utils.safe_send_message(bot=bot, chat_id=call.message.chat.id, text=text.strip())

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
    send_coming_events(user_id=user_id, chat_id=chat_id)


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

        team_1_will_go_through = None
        # Для группового этапа будет всегда None.
        # Для плей-офф – True или False.
        if event.is_playoff:
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
            send_coming_events(user_id=user.id, chat_id=message.chat.id)
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


def send_coming_events(user_id: int, chat_id: int):
    max_events_in_message = 10
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
        existing_bet = database.find_bet(user_id=user_id, event_uuid=event.uuid)
        if existing_bet is not None:
            text += f' (прогноз {existing_bet.team_1_scores}:{existing_bet.team_2_scores}'
            if existing_bet.team_1_will_go_through is not None and existing_bet.is_bet_on_draw():
                if existing_bet.team_1_will_go_through:
                    text += f', проход {event.team_1}'
                else:
                    text += f', проход {event.team_2}'
            text += ')'
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


def calculate_scores_after_finished_event(event: Event) -> Guessers:
    guessed_total_score = []
    guessed_goal_difference = []
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
        if event.is_playoff:
            if is_exact_score(result=result, bet=bet):
                scores_earned = 3
                guessed_total_score.append(user_model)
            elif is_same_goal_difference(result=result, bet=bet):
                scores_earned = 2
                guessed_goal_difference.append(user_model)
            elif is_same_winner(result=result, bet=bet):
                scores_earned = 1
                guessed_only_winner.append(user_model)

            # Также можно получить +1 очко за проход одной из команд.
            # Независимо от первой ставки.
            if is_guessed_who_has_gone_through(result=result, bet=bet):
                scores_earned += 1
                guessed_who_has_gone_through.append(user_model)
        else:
            # Алгоритм подсчёта для группового этапа
            if is_exact_score(result=result, bet=bet):
                scores_earned = 3
                guessed_total_score.append(user_model)
            elif is_same_goal_difference(result=result, bet=bet):
                scores_earned = 2
                guessed_goal_difference.append(user_model)
            elif is_same_winner(result=result, bet=bet):
                scores_earned = 1
                guessed_only_winner.append(user_model)

        if scores_earned > 0:
            database.add_scores_to_user(user_id=user_id, amount=scores_earned)

    return Guessers(
        guessed_total_score=guessed_total_score,
        guessed_goal_difference=guessed_goal_difference,
        guessed_only_winner=guessed_only_winner,
        guessed_who_has_gone_through=guessed_who_has_gone_through,
    )


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


def is_guessed_who_has_gone_through(result: EventResult, bet: Bet) -> bool:
    if result.team_1_has_gone_through is None or bet.team_1_will_go_through is None:
        return False
    return result.team_1_has_gone_through == bet.team_1_will_go_through


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
    schedule.every(10).minutes.do(do_every_ten_minutes)
    while True:
        schedule.run_pending()
        time.sleep(1)


def do_every_ten_minutes():
    send_morning_message_if_required()
    check_coming_soon_events()
    check_for_unfinished_events()


scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.start()


def send_morning_message_if_required():
    moscow_time = datetime_utils.get_moscow_time()
    if moscow_time.hour != 9 or moscow_time.minute not in range(10, 20):  # Отправляем в интервале 9:10 – 9:20
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


def check_for_unfinished_events():
    all_events = database.get_all_events()
    events_on_progress = list(filter(lambda x: x.is_in_progress(), all_events))
    result_events = list(filter(lambda x: is_event_requires_finish(x), events_on_progress))
    if len(result_events) == 1:
        event = result_events[0]
        msg = f'❗️Матч {event.team_1} – {event.team_2} требует завершения.'
        bot.send_message(get_maintainer_id(), text=msg)
    elif len(result_events) > 1:
        msg = f'❗{len(result_events)} матча требуют завершения.'
        bot.send_message(get_maintainer_id(), text=msg)


def is_event_requires_finish(unfinished_event: Event) -> bool:
    if unfinished_event.is_playoff:
        delta_hours = 3
    else:
        delta_hours = 2
    return unfinished_event.get_time_in_utc() + timedelta(hours=delta_hours) < datetime_utils.get_utc_time()


if __name__ == '__main__':
    bot.infinity_polling()
