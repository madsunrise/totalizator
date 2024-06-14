import locale
import logging
import os
import traceback
from datetime import datetime, timezone

import pytz
import telebot
from telebot.types import User, InlineKeyboardMarkup, InlineKeyboardButton

import callback_data_utils
import constants
import datetime_utils
import telegram_utils
import utils
from database import Database
from models import Event, EventResult, Bet

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
    bot.send_message(chat_id=message.chat.id, text='Доступ к боту предоставлен. Начни с команды /coming_events.')
    pass


# Service method
# Формат сообщения: "Германия; Шотландия; 14.06.2024 22:00"
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

    event_datetime_utc = datetime_utils.with_zone_same_instant(
        datetime_obj=datetime_obj,
        timezone_from=pytz.timezone('Europe/Moscow'),
        timezone_to=pytz.utc
    )

    existing_event = database.find_event(team_1=team_1, team_2=team_2, time=event_datetime_utc)
    if existing_event is not None:
        bot.send_message(chat_id=message.chat.id, text='Такой матч уже существует!')
        return

    event = Event(
        uuid=utils.generate_uuid(),
        team_1=team_1,
        team_2=team_2,
        time=event_datetime_utc,
    )
    database.add_event(event)
    bot.send_message(chat_id=message.chat.id, text='Матч добавлен')


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
        bot.send_message(chat_id=message.chat.id, text='Неверный формат сообщения')
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
    msg_text = (f'Матч {existing_event.team_1} – {existing_event.team_2} завершился ' +
                f'({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores})')
    msg_text += '\n\n'
    msg_text += get_leaderboard_text()
    bot.send_message(chat_id=constants.TARGET_CHAT_ID, text=msg_text)


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
    for event in events:
        moscow_time = datetime_utils.with_zone_same_instant(
            datetime_obj=event.time,
            timezone_from=pytz.utc,
            timezone_to=pytz.timezone('Europe/Moscow'),
        )
        text += f"{event.team_1} – {event.team_2}, {datetime_utils.to_display_string(moscow_time)}"
        event_result = event.result
        if event_result:
            text += f' ({event_result.team_1_scores}:{event_result.team_2_scores})'
        text += f'\nUUID: {event.uuid}'
        text += '\n\n'
    bot.send_message(chat_id=message.chat.id, text=text.strip())


@bot.message_handler(commands=['coming_events'])
def get_coming_events(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text='В личку, в личку пишем!')
        return
    save_user_or_update_interaction(user=user)
    send_coming_events(user=user, chat_id=message.chat.id)


@bot.message_handler(commands=['clear_context'])
def clear_current_event(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text='В личку, в личку пишем!')
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
        bot.send_message(chat_id=message.chat.id, text='В личку, в личку пишем!')
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
    text = ''
    for (bet, event) in bets_with_events:
        moscow_time = datetime_utils.with_zone_same_instant(
            datetime_obj=event.time,
            timezone_from=pytz.utc,
            timezone_to=pytz.timezone('Europe/Moscow'),
        )
        text += (f'{event.team_1} – {event.team_2} ({moscow_time.strftime('%d %b')}): '
                 f'{bet.team_1_scores}:{bet.team_2_scores}')
        text += '\n\n'
    bot.send_message(chat_id=message.chat.id, text=text)


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

    except Exception as e:
        handle_exception(e=e, user=user, chat_id=chat_id)


@bot.message_handler(content_types=['text'])
def get_text_messages(message):
    user = message.from_user
    if not is_club_member(user=user):
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

    if is_event_already_started(event):
        msg = f'Матч уже начался, досвидули!'
        bot.send_message(chat_id=message.chat.id, text=msg)
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
            created_at=datetime.now(timezone.utc)
        )
        database.add_bet(user_id=user.id, bet=bet)
        database.clear_current_event_for_user(user_id=user.id)
        bot.send_message(chat_id=message.chat.id,
                         text=f'Принято: {event.team_1} – {event.team_2} {bet.team_1_scores}:{bet.team_2_scores}')
        send_coming_events(user=user, chat_id=message.chat.id)
        msg_for_everybody = f'{user.full_name} сделал прогноз на матч {event.team_1} – {event.team_2}'
        bot.send_message(chat_id=constants.TARGET_CHAT_ID, text=msg_for_everybody)
    except:
        bot.send_message(chat_id=message.chat.id, text=wrong_format_msg)
        return


def is_club_member(user: User) -> bool:
    return telegram_utils.is_chat_member(bot=bot, chat_id=constants.TARGET_CHAT_ID, user_id=user.id)


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
        moscow_time = datetime_utils.with_zone_same_instant(
            datetime_obj=event.time,
            timezone_from=pytz.utc,
            timezone_to=pytz.timezone('Europe/Moscow'),
        )
        text += f'{index}. {event.team_1} – {event.team_2}, {datetime_utils.to_display_string(moscow_time)}'
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
                created_at=datetime.now(timezone.utc)
            )
        scores_earned = 0
        if result.team_1_scores == bet.team_1_scores and result.team_2_scores == bet.team_2_scores:
            scores_earned = 2
        elif is_same_winner(result=result, bet=bet):
            scores_earned = 1
        if scores_earned > 0:
            database.add_scores_to_user(user_id=user_id, amount=scores_earned)


def is_same_winner(result: EventResult, bet: Bet) -> bool:
    if result.team_1_scores > result.team_2_scores:
        return bet.team_1_scores > bet.team_2_scores
    if result.team_1_scores < result.team_2_scores:
        return bet.team_1_scores < bet.team_2_scores
    return bet.team_1_scores == bet.team_2_scores


def is_event_already_started(event: Event) -> bool:
    event_time = event.time.replace(tzinfo=timezone.utc)
    return event_time <= datetime.now(timezone.utc)


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

if __name__ == '__main__':
    bot.infinity_polling()
