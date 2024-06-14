import locale
from datetime import datetime

import pytz
import telebot
from telebot.types import User

import constants
import credentials
import datetime_utils
import telegram_utils
import utils
from database import Database
from models import Event, EventResult

locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
bot = telebot.TeleBot(credentials.TELEGRAM_TOKEN)
database = Database()


@bot.message_handler(commands=['start'])
def start(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    bot.send_message(chat_id=message.chat.id, text='Доступ к боту предоставлен')
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

    event = Event(
        uuid=utils.generate_uuid(),
        team_1=team_1,
        team_2=team_2,
        time=event_datetime_utc,
    )
    database.add_event(event)
    bot.send_message(chat_id=message.chat.id, text='Матч добавлен')


# Service method
# Формат сообщения: "Германия; Шотландия; 14.06.2024 22:00; 2:1"
@bot.message_handler(commands=['set_result'])
def set_result_for_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    split = list(map(lambda x: x.strip(), message.text.removeprefix('/set_result').strip().split(';')))
    team_1 = split[0]
    team_2 = split[1]

    date_format = '%d.%m.%Y %H:%M'
    datetime_obj = datetime.strptime(split[2], date_format)
    event_datetime_utc = datetime_utils.with_zone_same_instant(
        datetime_obj=datetime_obj,
        timezone_from=pytz.timezone('Europe/Moscow'),
        timezone_to=pytz.utc
    )

    team_1_scores = split[3].split(':')[0]
    team_2_scores = split[3].split(':')[1]

    existing_event = database.find_event(team_1=team_1, team_2=team_2, time=event_datetime_utc)
    if not existing_event:
        bot.send_message(chat_id=message.chat.id, text='Такой матч не найден :/')
        return

    if existing_event.result:
        msg = f'У матча уже есть результат ({existing_event.result.team_1_scores}:{existing_event.result.team_2_scores})'
        bot.send_message(chat_id=message.chat.id, text=msg)
        return

    existing_event.result = EventResult(
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores
    )
    database.update_event(event=existing_event)
    bot.send_message(
        chat_id=message.chat.id,
        text=f'OK, {existing_event.team_1} - {existing_event.team_2} ' +
             f'{existing_event.result.team_1_scores}:{existing_event.result.team_2_scores}'
    )


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
        text += f"{event.team_1} - {event.team_2}, {datetime_utils.to_display_string(moscow_time)}"
        event_result = event.result
        if event_result:
            text += f' ({event_result.team_1_scores} : {event_result.team_2_scores})'
        text += '\n'
    bot.send_message(chat_id=message.chat.id, text=text.strip())


@bot.message_handler(commands=['coming_events'])
def get_coming_events(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    events = database.get_all_events()
    result_events = []
    for event in events:
        event_time = event.time
        if event_time <= datetime.now(event_time.tzinfo):
            continue
        result_events.append(event)
        if len(result_events) >= 4:
            break

    if len(result_events) == 0:
        bot.send_message(chat_id=message.chat.id, text="Матчей не обнаружено")
        return

    text = ''
    for event in result_events:
        moscow_time = datetime_utils.with_zone_same_instant(
            datetime_obj=event['time'],
            timezone_from=pytz.utc,
            timezone_to=pytz.timezone('Europe/Moscow'),
        )
        text += f"{event.team_1} - {event.team_2}, {datetime_utils.to_display_string(moscow_time)}"
        text += '\n'

    bot.send_message(chat_id=message.chat.id, text=text.strip())


@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    user = call.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)


@bot.message_handler(content_types=['text'])
def get_text_messages(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)
    chat_id = message.chat.id
    text = message.text


def is_club_member(user: User) -> bool:
    return telegram_utils.is_chat_member(bot=bot, chat_id=constants.TARGET_CHAT_ID, user_id=user.id)


def is_maintainer(user: User) -> bool:
    return user.id == credentials.MAINTAINER_ID


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
        bot.send_message(chat_id=credentials.MAINTAINER_ID, text=f'New user: {user.full_name} ({user.username})')


if __name__ == '__main__':
    bot.infinity_polling()
