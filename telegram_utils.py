from telebot import TeleBot
from telebot.apihelper import ApiTelegramException

import constants


def is_chat_member(bot: TeleBot, chat_id: int, user_id: int) -> bool:
    try:
        status = bot.get_chat_member(chat_id=chat_id, user_id=user_id).status
        return status == 'creator' or status == 'member' or status == 'administrator'
    except ApiTelegramException as e:
        return False


def safe_send_message(bot: TeleBot, chat_id: int, text: str):
    if len(text) > constants.TELEGRAM_MAX_MESSAGE_SIZE:
        messages = []
        for x in range(0, len(text), constants.TELEGRAM_MAX_MESSAGE_SIZE):
            message_text = text[x:x + constants.TELEGRAM_MAX_MESSAGE_SIZE]
            messages.append(bot.send_message(chat_id=chat_id, text=message_text))
    else:
        bot.send_message(chat_id=chat_id, text=text)
