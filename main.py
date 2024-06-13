import telebot
from telebot.types import User

import constants
import credentials
import telegram_utils

bot = telebot.TeleBot(credentials.TELEGRAM_TOKEN)


@bot.message_handler(commands=['start'])
def start(message):
    if not is_club_member(user=message.from_user):
        return
    bot.send_message(chat_id=message.chat.id, text='Доступ к боту предоставлен')
    pass


@bot.message_handler(commands=['test'])
def testCommand(message):
    if not is_club_member(user=message.from_user):
        return
    pass


@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    user = call.from_user
    if not is_club_member(user=user):
        return


@bot.message_handler(content_types=['text'])
def get_text_messages(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    chat_id = message.chat.id
    text = message.text


def is_club_member(user: User) -> bool:
    return telegram_utils.is_chat_member(bot=bot, chat_id=constants.TARGET_CHAT_ID, user_id=user.id)


if __name__ == '__main__':
    bot.infinity_polling()
