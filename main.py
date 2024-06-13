import telebot
from telebot.types import User

import constants
import credentials
import telegram_utils
from database import Database

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


@bot.message_handler(commands=['test'])
def testCommand(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    save_user_or_update_interaction(user=user)


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
