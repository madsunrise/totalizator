from telebot import TeleBot
from telebot.apihelper import ApiTelegramException


def is_chat_member(bot: TeleBot, chat_id: int, user_id: int) -> bool:
    try:
        status = bot.get_chat_member(chat_id=chat_id, user_id=user_id).status
        print(f'Get userId {user_id} role: {status}')
        return status == 'creator' or status == 'member' or status == 'administrator'
    except ApiTelegramException as e:
        print(f'Seems user {user_id} not a member, exception')
        return False
