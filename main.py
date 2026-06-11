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
import event_utils
import football_api
import joker_utils
import strings
import telegram_utils
import tournament_utils
import utils
from database import Database
from models import Event, EventResult, Bet, Guessers, GuessedEvent, EventType, DetailedStatistic, UserModel, Tournament

locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
bot = telebot.TeleBot(os.environ[constants.ENV_BOT_TOKEN])
database = Database()
joker_write_lock = threading.Lock()
# Завершение матча может прийти из двух потоков: ручной /result (поток telebot)
# и авто-завершение по API (поток планировщика). Лок делает проверку
# «результат ещё не записан» + запись + начисление очков атомарными.
finish_event_lock = threading.Lock()
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
# Один матч на строку: "Германия; Шотландия; 14.06.2024 22:00; group". Можно добавить
# несколько матчей одним сообщением — по одному на строку (тело может идти и на первой строке
# с командой, и со следующих строк). Типы: group | playoff_first_match | playoff_second_match | playoff_single.
# Время — по Москве. При любой синтаксической ошибке НЕ добавляется ничего (все ошибки сразу);
# уже существующие матчи пропускаются (повторная отправка списка безопасна).
@bot.message_handler(commands=['add_event'])
def add_event(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    parsed_events, errors = event_utils.parse_events_block(message.text)
    if errors:
        telegram_utils.safe_send_message(
            bot=bot, chat_id=message.chat.id, text='Ничего не добавлено. Ошибки:\n' + '\n'.join(errors))
        return

    moscow_tz = pytz.timezone('Europe/Moscow')
    now_utc = datetime_utils.get_utc_time()
    added = []  # list[ParsedEvent]
    skipped = []  # list[ParsedEvent] — уже есть в базе
    in_past = 0  # сколько добавленных матчей уже в прошлом (подсказка про опечатку в дате)
    for parsed in parsed_events:
        existing = database.find_event(team_1=parsed.team_1, team_2=parsed.team_2, time=parsed.time_utc)
        if existing is not None:
            skipped.append(parsed)
            continue
        database.add_event(Event(
            uuid=utils.generate_uuid(),
            team_1=parsed.team_1,
            team_2=parsed.team_2,
            time=parsed.time_utc,
            event_type=parsed.event_type,
        ))
        added.append(parsed)
        if parsed.time_utc <= now_utc:
            in_past += 1

    lines = [f'{strings.OK}. Добавлено матчей: {len(added)}.']
    if skipped:
        lines.append(f'Пропущено дубликатов: {len(skipped)}.')
    if in_past:
        # Самый ранний матч задаёт момент закрытия приёма спецставок: матч «в прошлом» закроет его сразу.
        lines.append(f'⚠️ Матчей со временем в прошлом: {in_past}. Проверь дату — приём спецставок '
                     f'закрывается по самому раннему матчу.')
    for parsed in added:
        moscow = parsed.time_utc.astimezone(moscow_tz)
        lines.append(f'{parsed.team_1} – {parsed.team_2}, {datetime_utils.to_display_string(moscow)} МСК')
    telegram_utils.safe_send_message(bot=bot, chat_id=message.chat.id, text='\n'.join(lines))


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

    result = EventResult(
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores,
        team_1_has_gone_through=team_1_has_gone_through
    )
    finish_event_and_announce(event=existing_event, result=result, confirmation_chat_id=message.chat.id)


# Общий путь завершения матча для ручного /result и авто-завершения по API:
# записывает результат, начисляет очки и публикует итоги в группу.
# Возвращает False, если матч уже завершён (защита от гонки /result vs авто-тик).
def finish_event_and_announce(event: Event, result: EventResult, confirmation_chat_id: int | None = None) -> bool:
    with finish_event_lock:
        existing_event = database.get_event_by_uuid(uuid=event.uuid)
        if existing_event is None or existing_event.result is not None:
            return False
        existing_event.result = result
        database.update_event(event=existing_event)
        guessers = calculate_scores_after_finished_event(event=existing_event)
    event_type = existing_event.event_type
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

    if confirmation_chat_id is not None:
        bot.send_message(chat_id=confirmation_chat_id, text=msg_text)

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
    try:
        bot.send_message(chat_id=get_target_chat_id(), text=msg_text)
    except Exception as e:
        # Очки уже начислены — повторно завершать матч нельзя, но итоги в группу не ушли.
        # Маякнём мейнтейнерам, чтобы запостили вручную.
        logging.exception(e)
        for user_id in get_maintainer_ids():
            send_joker_reminder_message(
                chat_id=user_id,
                text=f'Матч {existing_event.team_1} – {existing_event.team_2} завершён, '
                     f'но итоги не отправились в группу. Запости их вручную.')
    return True


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
    send_special_bets_hint(chat_id=message.chat.id, user_id=user.id)


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


# --- Спецставки: команды мейнтейнера (структура, открытие приёма) -------------------
# ВАЖНО: эти обработчики команд должны быть зарегистрированы ВЫШЕ catch-all
# @bot.message_handler(content_types=['text']), иначе многострочные /setup_tournament и
# /set_group_winners попадут в текстовый обработчик. telebot матчит commands по первому
# токену, поэтому многострочное тело команды безопасно.

def is_betting_closed() -> bool:
    # Единственное определение «турнир стартовал»: самый ранний матч уже начался.
    # Общее для гардов открытия приёма и авто-закрытия.
    start = tournament_utils.get_tournament_start(database.get_all_events())
    return start is not None and start <= datetime_utils.get_utc_time()


def check_can_open_bet() -> str | None:
    if not database.tournament_exists():
        return strings.NEED_STRUCTURE_FIRST
    if is_betting_closed():
        return strings.OPEN_TOO_LATE
    return None


def bet_status_text(is_open: bool) -> str:
    if not is_open:
        return 'не открывалась'
    if is_betting_closed():
        return 'открывалась, приём закрыт'
    return 'открыта, приём идёт'


def format_structure_confirmation(tournament: Tournament) -> str:
    teams = tournament.all_teams()
    lines = [f'{strings.OK}. Структура сохранена.']
    lines.append(f'Групп: {tournament.group_count()}, команд всего: {len(teams)}.')
    for group in tournament.groups:
        lines.append(f'{group.name}: {", ".join(group.teams)}')
    lines.append('')
    lines.append(f'Кандидаты на чемпиона ({len(teams)}): {", ".join(teams)}')
    return '\n'.join(lines)


def format_tournament_info(tournament: Tournament) -> str:
    lines = ['Структура турнира:']
    for group in tournament.groups:
        lines.append(f'{group.name}: {", ".join(group.teams)}')
    lines.append('')
    lines.append(f'Ставка на чемпиона: {bet_status_text(tournament.champion_bet_open)}')
    lines.append(f'Ставка на победителей групп: {bet_status_text(tournament.group_bet_open)}')
    start = tournament_utils.get_tournament_start(database.get_all_events())
    if start is None:
        lines.append('Приём ставок: открыт (матчи ещё не добавлены)')
    elif is_betting_closed():
        lines.append('Приём ставок: ЗАКРЫТ (турнир стартовал)')
    else:
        start_display = datetime_utils.to_display_string(start.astimezone(pytz.timezone('Europe/Moscow')))
        lines.append(f'Приём ставок: открыт до старта первого матча ({start_display} МСК)')
    lines.append(f'Чемпион (факт): {tournament.champion_winner or "—"}')
    if tournament.group_winners:
        gw = ', '.join(f'{group_id}: {team}' for group_id, team in tournament.group_winners.items())
    else:
        gw = '—'
    lines.append(f'Победители групп (факт): {gw}')
    return '\n'.join(lines)


# Формат: одна группа на строку, "A: Канада, Мексика, США". Для перезаписи уже открытой
# структуры добавить слово FORCE отдельной строкой (или сразу после команды).
@bot.message_handler(commands=['setup_tournament'])
def setup_tournament(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    if is_betting_closed():
        bot.send_message(chat_id=message.chat.id, text=strings.SETUP_AFTER_START)
        return
    groups, errors = tournament_utils.parse_structure(message.text)
    if errors:
        bot.send_message(chat_id=message.chat.id, text='\n'.join(errors))
        return
    existing = database.get_tournament()
    force = tournament_utils.has_force_token(message.text)
    if existing is not None and (existing.champion_bet_open or existing.group_bet_open) and not force:
        bot.send_message(chat_id=message.chat.id, text=strings.SETUP_NEEDS_FORCE)
        return
    tournament = Tournament(
        name=existing.name if existing is not None else 'Турнир',
        created_at=existing.created_at if existing is not None else datetime.now(timezone.utc),
        groups=groups,
        # Перезапись структуры сохраняет состояние ставок (флаги/факты/расчёт),
        # чтобы правка опечатки не сбрасывала уже открытый приём и начисления.
        champion_bet_open=existing.champion_bet_open if existing is not None else False,
        group_bet_open=existing.group_bet_open if existing is not None else False,
        champion_winner=existing.champion_winner if existing is not None else None,
        group_winners=existing.group_winners if existing is not None else None,
        champion_settled=existing.champion_settled if existing is not None else False,
        group_settled=existing.group_settled if existing is not None else False,
    )
    database.save_tournament(tournament)
    bot.send_message(chat_id=message.chat.id, text=format_structure_confirmation(tournament))


@bot.message_handler(commands=['tournament_info'])
def tournament_info(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    tournament = database.get_tournament()
    if tournament is None:
        bot.send_message(chat_id=message.chat.id, text=strings.STRUCTURE_NOT_SET)
        return
    bot.send_message(chat_id=message.chat.id, text=format_tournament_info(tournament))


@bot.message_handler(commands=['open_champion_bet'])
def open_champion_bet(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    error = check_can_open_bet()
    if error:
        bot.send_message(chat_id=message.chat.id, text=error)
        return
    tournament = database.get_tournament()
    if tournament.champion_bet_open:
        bot.send_message(chat_id=message.chat.id, text=strings.CHAMPION_BET_ALREADY_OPEN)
        return
    database.set_champion_bet_open(True)
    bot.send_message(chat_id=message.chat.id, text=strings.OK)
    teams_count = len(tournament.all_teams())
    announcement = (f'🏆 Открыт приём ставок на ЧЕМПИОНА турнира!\n'
                    f'Выбери победителя из {teams_count} команд. '
                    f'Приём закроется автоматически со стартом первого матча.\n'
                    f'За верный прогноз: +{tournament_utils.CHAMPION_BET_POINTS} очков.\n'
                    f'Сделать прогноз: /champion')
    bot.send_message(chat_id=get_target_chat_id(), text=announcement)


@bot.message_handler(commands=['open_group_bet'])
def open_group_bet(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    error = check_can_open_bet()
    if error:
        bot.send_message(chat_id=message.chat.id, text=error)
        return
    tournament = database.get_tournament()
    if tournament.group_bet_open:
        bot.send_message(chat_id=message.chat.id, text=strings.GROUP_BET_ALREADY_OPEN)
        return
    database.set_group_bet_open(True)
    bot.send_message(chat_id=message.chat.id, text=strings.OK)
    groups_count = tournament.group_count()
    announcement = (f'🥇 Открыт приём ставок на ПОБЕДИТЕЛЕЙ ГРУПП!\n'
                    f'Угадай, кто займёт 1-е место в каждой из {groups_count} групп. '
                    f'+1 за каждую угаданную группу и +{tournament_utils.ALL_GROUPS_BONUS} бонусом, '
                    f'если угаданы ВСЕ.\n'
                    f'Приём закроется автоматически со стартом первого матча.\n'
                    f'Сделать прогноз: /group_bets')
    bot.send_message(chat_id=get_target_chat_id(), text=announcement)


# Формат: "/set_champion Бразилия". Начисляет +10 угадавшим (единовременно, идемпотентно).
@bot.message_handler(commands=['set_champion'])
def set_champion(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    tournament = database.get_tournament()
    if tournament is None:
        bot.send_message(chat_id=message.chat.id, text=strings.STRUCTURE_NOT_SET)
        return
    if not tournament.champion_bet_open:
        bot.send_message(chat_id=message.chat.id, text=strings.CHAMPION_NOT_OPENED)
        return
    # Рассчитывать можно только после закрытия приёма (старт первого матча): иначе
    # участник может сменить выбор уже после начисления, и scores разойдётся с пиком.
    if not is_betting_closed():
        bot.send_message(chat_id=message.chat.id, text=strings.SETTLE_TOO_EARLY)
        return
    if tournament.champion_winner:
        bot.send_message(chat_id=message.chat.id, text=strings.CHAMPION_ALREADY_SET % tournament.champion_winner)
        return
    team_input = message.text.removeprefix('/set_champion').strip()
    if not team_input:
        bot.send_message(chat_id=message.chat.id, text=strings.WRONG_MESSAGE_FORMAT_ERROR)
        return
    canonical = tournament.find_team(team_input)
    if canonical is None:
        bot.send_message(chat_id=message.chat.id, text=strings.UNKNOWN_TEAM % team_input)
        return
    database.set_champion_winner(canonical)
    winners = []
    for user_model in database.get_all_users():
        pick = database.get_champion_bet(user_model.id)
        if tournament_utils.calculate_champion_bet_points(pick, canonical) <= 0:
            continue
        # Per-user одноразовый guard: +10 не начислится дважды даже при повторном запуске/перезапуске.
        if not database.claim_reminder(f'settle:champion:{user_model.id}'):
            continue
        database.add_scores_to_user(user_id=user_model.id, amount=tournament_utils.CHAMPION_BET_POINTS)
        winners.append(user_model)
    database.mark_champion_settled()
    bot.send_message(chat_id=message.chat.id, text=strings.OK)

    text = f'🏆 Чемпион турнира — {canonical}!\n'
    if winners:
        text += f'Угадал(и) чемпиона (+{tournament_utils.CHAMPION_BET_POINTS}):\n'
        text += '\n'.join(w.get_full_name() for w in winners)
    else:
        text += 'Чемпиона не угадал никто.'
    text += '\n-----\n' + get_leaderboard_text()
    telegram_utils.safe_send_message(bot=bot, chat_id=get_target_chat_id(), text=text)


# Формат (по группе на строку): "/set_group_winners\nA: США\nB: ...".
# Начисляет +1 за группу и +10 бонусом за все угаданные (единовременно, идемпотентно).
@bot.message_handler(commands=['set_group_winners'])
def set_group_winners(message):
    user = message.from_user
    if not is_maintainer(user=user):
        return
    save_user_or_update_interaction(user=user)
    tournament = database.get_tournament()
    if tournament is None:
        bot.send_message(chat_id=message.chat.id, text=strings.STRUCTURE_NOT_SET)
        return
    if not tournament.group_bet_open:
        bot.send_message(chat_id=message.chat.id, text=strings.GROUP_NOT_OPENED)
        return
    # Рассчитывать можно только после закрытия приёма (старт первого матча): иначе
    # участник может сменить выбор уже после начисления, и scores разойдётся с пиком.
    if not is_betting_closed():
        bot.send_message(chat_id=message.chat.id, text=strings.SETTLE_TOO_EARLY)
        return
    if tournament.group_winners:
        bot.send_message(chat_id=message.chat.id, text=strings.GROUP_WINNERS_ALREADY_SET)
        return
    winners_map, errors = tournament_utils.parse_group_winners(message.text, tournament)
    if errors:
        bot.send_message(chat_id=message.chat.id, text='\n'.join(errors))
        return
    database.set_group_winners(winners_map)
    total = tournament.group_count()
    results = []  # (user_model, GroupBetResult)
    for user_model in database.get_all_users():
        picks = database.get_group_champion_bets(user_model.id)
        if not picks:
            continue
        result = tournament_utils.calculate_group_bet_points(picks, winners_map, total_groups=total)
        if result.total_points > 0 and database.claim_reminder(f'settle:groups:{user_model.id}'):
            database.add_scores_to_user(user_id=user_model.id, amount=result.total_points)
        results.append((user_model, result))
    database.mark_group_settled()
    bot.send_message(chat_id=message.chat.id, text=strings.OK)

    lines = ['🥇 Победители групп зафиксированы:']
    for group in tournament.groups:
        lines.append(f'{group.name} — {winners_map.get(group.id, "—")}')
    lines.append('-----')
    if results:
        lines.append('Результаты прогнозов:')
        results.sort(key=lambda x: x[1].correct_count, reverse=True)
        for user_model, result in results:
            mark = ' 🎯' if result.all_correct else ''
            lines.append(f'{user_model.get_full_name()}: {result.correct_count}/{result.total_groups} '
                         f'(+{result.total_points}){mark}')
    else:
        lines.append('Никто не делал ставку на группы.')
    lines.append('-----')
    lines.append(get_leaderboard_text())
    telegram_utils.safe_send_message(bot=bot, chat_id=get_target_chat_id(), text='\n'.join(lines))


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

        elif callback_data_utils.is_champion_open(call.data):
            send_champion_bet_message(chat_id=chat_id, user_id=user.id, message_id=call.message.message_id)
        elif callback_data_utils.is_champion_group(call.data):
            group_index = callback_data_utils.extract_group_index_from_champion_group(call.data)
            send_champion_team_menu(chat_id=chat_id, user_id=user.id, group_index=group_index,
                                    message_id=call.message.message_id)
        elif callback_data_utils.is_champion_team(call.data):
            group_index, team_index = callback_data_utils.extract_indexes_from_champion_team(call.data)
            process_champion_pick(user_id=user.id, chat_id=chat_id, message_id=call.message.message_id,
                                  group_index=group_index, team_index=team_index)
        elif callback_data_utils.is_group_overview(call.data):
            send_group_bets_overview_message(chat_id=chat_id, user_id=user.id, message_id=call.message.message_id)
        elif callback_data_utils.is_group_done(call.data):
            send_group_bets_overview_message(chat_id=chat_id, user_id=user.id, message_id=call.message.message_id,
                                             as_summary=True)
        elif callback_data_utils.is_group_pick(call.data):
            group_index = callback_data_utils.extract_group_index_from_group_pick(call.data)
            send_group_team_menu(chat_id=chat_id, user_id=user.id, group_index=group_index,
                                 message_id=call.message.message_id)
        elif callback_data_utils.is_group_team(call.data):
            group_index, team_index = callback_data_utils.extract_indexes_from_group_team(call.data)
            process_group_pick(user_id=user.id, chat_id=chat_id, message_id=call.message.message_id,
                               group_index=group_index, team_index=team_index)


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


# --- Спецставки: интерфейс участника (кнопочные мастера) ---------------------------
# Полностью на inline-кнопках, current_event НЕ используют — поэтому не конфликтуют
# с единственным текстовым обработчиком get_text_messages. Состояние = сохранённые поля
# champion_bet / group_champion_bets, поэтому мастер резюмируется и поддерживает частичное
# заполнение.

def _chunked(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def send_or_edit(chat_id: int, text: str, reply_markup=None, message_id: int | None = None):
    if message_id is not None:
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
            return
        except Exception as e:
            logging.exception(e)
    bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


def is_champion_bet_acceptable(tournament: Tournament | None) -> bool:
    return tournament is not None and tournament.champion_bet_open and not is_betting_closed()


def is_group_bet_acceptable(tournament: Tournament | None) -> bool:
    return tournament is not None and tournament.group_bet_open and not is_betting_closed()


def count_valid_group_picks(tournament: Tournament, picks: dict) -> int:
    # Учитываем только выборы для существующих групп с командой из этой группы (игнор устаревших).
    valid = 0
    for group in tournament.groups:
        team = picks.get(group.id)
        if team and tournament.find_team_in_group(group.id, team) is not None:
            valid += 1
    return valid


def format_group_picks_summary(tournament: Tournament, picks: dict) -> str:
    parts = []
    for group in tournament.groups:
        team = picks.get(group.id)
        parts.append(f'{group.name} — {team}' if team else f'⬜ {group.name}')
    return ', '.join(parts)


# ----- Чемпион турнира -----

def send_champion_bet_message(chat_id: int, user_id: int, message_id: int | None = None):
    tournament = database.get_tournament()
    pick = database.get_champion_bet(user_id) if tournament is not None else None
    if tournament is None or not tournament.champion_bet_open:
        text = strings.SPECIAL_BET_NOT_OPEN_YET
        if pick:
            text += f'\n\nТвой выбор чемпиона: {pick}.'
        send_or_edit(chat_id, text, message_id=message_id)
        return
    if is_betting_closed():
        text = strings.SPECIAL_BET_CLOSED
        if pick:
            text += f'\n\nТвой выбор чемпиона: {pick}.'
        send_or_edit(chat_id, text, message_id=message_id)
        return
    if pick:
        header = (f'Твой выбор чемпиона: {pick}. Можно изменить до старта турнира.\n'
                  f'Выбери группу, из которой будет чемпион:')
    else:
        header = 'Выбери группу, из которой будет чемпион турнира:'
    markup = InlineKeyboardMarkup()
    for row in _chunked(list(enumerate(tournament.groups)), 4):
        buttons = []
        for group_index, group in row:
            label = group.name
            if pick and tournament.find_team_in_group(group.id, pick) is not None:
                label = f'✅ {group.name}'
            buttons.append(InlineKeyboardButton(
                label, callback_data=callback_data_utils.create_champion_group(group_index)))
        markup.row(*buttons)
    send_or_edit(chat_id, header, reply_markup=markup, message_id=message_id)


def send_champion_team_menu(chat_id: int, user_id: int, group_index: int, message_id: int | None = None):
    tournament = database.get_tournament()
    if not is_champion_bet_acceptable(tournament) or group_index < 0 or group_index >= len(tournament.groups):
        send_champion_bet_message(chat_id, user_id, message_id=message_id)
        return
    group = tournament.groups[group_index]
    pick = database.get_champion_bet(user_id)
    markup = InlineKeyboardMarkup()
    for team_index, team in enumerate(group.teams):
        label = f'✅ {team}' if (pick and tournament_utils.equals_team(pick, team)) else team
        markup.add(InlineKeyboardButton(
            label, callback_data=callback_data_utils.create_champion_team(group_index, team_index)))
    markup.add(InlineKeyboardButton(
        '⬅️ Назад к группам', callback_data=callback_data_utils.create_champion_open()))
    send_or_edit(chat_id, f'Группа {group.name}. Выбери чемпиона турнира:', reply_markup=markup, message_id=message_id)


def process_champion_pick(user_id: int, chat_id: int, message_id: int, group_index: int, team_index: int):
    tournament = database.get_tournament()
    if not is_champion_bet_acceptable(tournament):
        send_champion_bet_message(chat_id, user_id, message_id=message_id)
        return
    if group_index < 0 or group_index >= len(tournament.groups):
        send_champion_bet_message(chat_id, user_id, message_id=message_id)
        return
    group = tournament.groups[group_index]
    if team_index < 0 or team_index >= len(group.teams):
        send_champion_team_menu(chat_id, user_id, group_index, message_id=message_id)
        return
    team = group.teams[team_index]
    database.set_champion_bet(user_id, team)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton('✏️ Изменить', callback_data=callback_data_utils.create_champion_open()))
    send_or_edit(chat_id, f'Чемпион турнира: {team} ✅. Изменить можно до старта турнира.',
                 reply_markup=markup, message_id=message_id)


# ----- Победители групп -----

def send_group_bets_overview_message(chat_id: int, user_id: int, message_id: int | None = None,
                                     as_summary: bool = False):
    tournament = database.get_tournament()
    picks = database.get_group_champion_bets(user_id) if tournament is not None else {}
    if tournament is None or not tournament.group_bet_open:
        send_or_edit(chat_id, strings.SPECIAL_BET_NOT_OPEN_YET, message_id=message_id)
        return
    if is_betting_closed():
        text = strings.SPECIAL_BET_CLOSED + '\n\n' + format_group_picks_summary(tournament, picks)
        send_or_edit(chat_id, text, message_id=message_id)
        return
    filled = count_valid_group_picks(tournament, picks)
    total = tournament.group_count()
    if as_summary:
        header = (f'Готово. Заполнено {filled}/{total}.\n'
                  f'{format_group_picks_summary(tournament, picks)}\n\n'
                  f'Можно изменить до старта турнира. Выбери группу:')
    else:
        header = (f'Победители групп: заполнено {filled}/{total}.\n'
                  f'+1 за каждую угаданную группу. Бонус +{tournament_utils.ALL_GROUPS_BONUS}, если угаданы ВСЕ.\n'
                  f'Выбери группу:')
    markup = InlineKeyboardMarkup()
    for row in _chunked(list(enumerate(tournament.groups)), 4):
        buttons = []
        for group_index, group in row:
            chosen = picks.get(group.id)
            valid = chosen and tournament.find_team_in_group(group.id, chosen) is not None
            label = f'✅ {group.name}' if valid else f'⬜ {group.name}'
            buttons.append(InlineKeyboardButton(
                label, callback_data=callback_data_utils.create_group_pick(group_index)))
        markup.row(*buttons)
    markup.add(InlineKeyboardButton('Готово', callback_data=callback_data_utils.create_group_done()))
    send_or_edit(chat_id, header, reply_markup=markup, message_id=message_id)


def send_group_team_menu(chat_id: int, user_id: int, group_index: int, message_id: int | None = None):
    tournament = database.get_tournament()
    if not is_group_bet_acceptable(tournament) or group_index < 0 or group_index >= len(tournament.groups):
        send_group_bets_overview_message(chat_id, user_id, message_id=message_id)
        return
    group = tournament.groups[group_index]
    chosen = database.get_group_champion_bets(user_id).get(group.id)
    markup = InlineKeyboardMarkup()
    for team_index, team in enumerate(group.teams):
        label = f'✅ {team}' if (chosen and tournament_utils.equals_team(chosen, team)) else team
        markup.add(InlineKeyboardButton(
            label, callback_data=callback_data_utils.create_group_team(group_index, team_index)))
    markup.add(InlineKeyboardButton('⬅️ К группам', callback_data=callback_data_utils.create_group_overview()))
    send_or_edit(chat_id, f'Группа {group.name}. Кто станет победителем группы?',
                 reply_markup=markup, message_id=message_id)


def process_group_pick(user_id: int, chat_id: int, message_id: int, group_index: int, team_index: int):
    tournament = database.get_tournament()
    if not is_group_bet_acceptable(tournament):
        send_group_bets_overview_message(chat_id, user_id, message_id=message_id)
        return
    if group_index < 0 or group_index >= len(tournament.groups):
        send_group_bets_overview_message(chat_id, user_id, message_id=message_id)
        return
    group = tournament.groups[group_index]
    if team_index < 0 or team_index >= len(group.teams):
        send_group_team_menu(chat_id, user_id, group_index, message_id=message_id)
        return
    team = group.teams[team_index]
    database.set_group_champion_bet(user_id, group.id, team)
    send_group_bets_overview_message(chat_id, user_id, message_id=message_id)


# ----- Интеграция в /coming_events и /my_bets -----

def send_special_bets_hint(chat_id: int, user_id: int):
    # Отдельным сообщением показываем спецставки, ПОКА хотя бы одна открыта и принимается.
    tournament = database.get_tournament()
    if tournament is None:
        return
    champ_ok = is_champion_bet_acceptable(tournament)
    group_ok = is_group_bet_acceptable(tournament)
    if not champ_ok and not group_ok:
        return
    lines = ['Спецпрогнозы:']
    markup = InlineKeyboardMarkup()
    if champ_ok:
        pick = database.get_champion_bet(user_id)
        if pick:
            lines.append(f'🏆 Чемпион турнира: {pick} (можно изменить)')
            markup.add(InlineKeyboardButton(
                '🏆 Изменить чемпиона', callback_data=callback_data_utils.create_champion_open()))
        else:
            lines.append('🏆 Чемпион турнира — приём открыт, ты ещё не выбрал.')
            markup.add(InlineKeyboardButton(
                '🏆 Выбрать чемпиона', callback_data=callback_data_utils.create_champion_open()))
    if group_ok:
        picks = database.get_group_champion_bets(user_id)
        filled = count_valid_group_picks(tournament, picks)
        lines.append(f'🥇 Победители групп — заполнено {filled}/{tournament.group_count()}.')
        markup.add(InlineKeyboardButton(
            '🥇 Победители групп', callback_data=callback_data_utils.create_group_overview()))
    bot.send_message(chat_id=chat_id, text='\n'.join(lines), reply_markup=markup)


def format_special_bets_section(tournament: Tournament | None, user_id: int) -> str:
    # Read-only текст спецпрогнозов для /my_bets. '' если показывать нечего.
    if tournament is None:
        return ''
    lines = []
    if tournament.champion_bet_open:
        pick = database.get_champion_bet(user_id)
        line = f'🏆 Чемпион: {pick}' if pick else '🏆 Чемпион: не выбран'
        if tournament.champion_winner:
            points = tournament_utils.calculate_champion_bet_points(pick, tournament.champion_winner)
            line += f' (факт: {tournament.champion_winner}, +{points})'
        lines.append(line)
    if tournament.group_bet_open:
        picks = database.get_group_champion_bets(user_id)
        filled = count_valid_group_picks(tournament, picks)
        line = (f'🥇 Победители групп ({filled}/{tournament.group_count()}): '
                f'{format_group_picks_summary(tournament, picks)}')
        if tournament.group_winners:
            result = tournament_utils.calculate_group_bet_points(
                picks, tournament.group_winners, total_groups=tournament.group_count())
            line += f'\nУгадано {result.correct_count}/{result.total_groups}, начислено +{result.total_points}'
        lines.append(line)
    if not lines:
        return ''
    return 'Спецпрогнозы:\n' + '\n'.join(lines)


@bot.message_handler(commands=['champion'])
def champion_command(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    send_champion_bet_message(chat_id=message.chat.id, user_id=user.id)


@bot.message_handler(commands=['group_bets'])
def group_bets_command(message):
    user = message.from_user
    if not is_club_member(user=user):
        return
    if message.chat.type != 'private':
        bot.send_message(chat_id=message.chat.id, text=strings.WRITE_TO_PRIVATE_MESSAGES)
        return
    save_user_or_update_interaction(user=user)
    send_group_bets_overview_message(chat_id=message.chat.id, user_id=user.id)


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
    tournament = database.get_tournament()
    special_section = format_special_bets_section(tournament, user_id)
    if len(bets_with_events) == 0:
        base = f'{status_text}\n\nПока ничего нет. Начни с команды /coming_events.'
        if special_section:
            base += f'\n\n{special_section}'
        markup = InlineKeyboardMarkup()
        if is_champion_bet_acceptable(tournament):
            markup.add(InlineKeyboardButton(
                '🏆 Чемпион турнира', callback_data=callback_data_utils.create_champion_open()))
        if is_group_bet_acceptable(tournament):
            markup.add(InlineKeyboardButton(
                '🥇 Победители групп', callback_data=callback_data_utils.create_group_overview()))
        bot.send_message(chat_id=chat_id, text=base, reply_markup=markup if markup.keyboard else None)
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
    if special_section:
        text += f'\n\n{special_section}'
    if is_champion_bet_acceptable(tournament):
        reply_markup.add(InlineKeyboardButton(
            '🏆 Чемпион турнира', callback_data=callback_data_utils.create_champion_open()))
    if is_group_bet_acceptable(tournament):
        reply_markup.add(InlineKeyboardButton(
            '🥇 Победители групп', callback_data=callback_data_utils.create_group_overview()))
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
    run_scheduled_task(check_api_results)
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logging.exception(e)
        time.sleep(1)


def run_scheduled_task(task):
    # Любое исключение из одной плановой задачи не должно срывать остальные проверки тика.
    try:
        task()
    except Exception as e:
        logging.exception(e)


def do_every_ten_minutes():
    tasks = (
        send_morning_message_with_games_today,
        check_coming_soon_events,
        check_for_night_events,
        check_api_results,  # сначала пробуем завершить матчи по API, чтобы не алертить зря
        check_for_unfinished_events,
        check_playoff_joker_reminders,
        check_tournament_end_joker_reminders,
        check_for_burned_jokers_after_playoff_start,
        check_special_bets_reminders,
        check_special_bets_close,
    )
    for task in tasks:
        run_scheduled_task(task)


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
    # Одно сообщение в общий чат с упоминанием всех, у кого сгорели джокеры, и числом сгоревших.
    lines = []
    for user_model, status in get_all_users_with_joker_status():
        if status.will_burn_at_playoff_start <= 0:
            continue
        lines.append(f'{get_user_mention(user_model)} — {status.will_burn_at_playoff_start}')
    if len(lines) == 0:
        return
    text = 'Стартовал плей-офф, сгорели неиспользованные джокеры:\n' + '\n'.join(lines)
    send_joker_reminder_message(chat_id=get_target_chat_id(), text=text)


def send_special_bet_reminder(header: str, users_without_bet: list[UserModel]):
    # Публичное напоминание в общий чат с упоминанием только тех, кто ещё не сделал ставку.
    # Маркер claim_reminder ставится вызывающим ДО построения списка, поэтому при пустом списке
    # просто ничего не шлём (но порог уже погашен) — как в check_special_bets_close.
    if len(users_without_bet) == 0:
        return
    mentions = '\n'.join(map(get_user_mention, users_without_bet))
    send_joker_reminder_message(chat_id=get_target_chat_id(), text=f'{header}\n{mentions}')


def check_special_bets_reminders():
    # Напоминания тем, кто не сделал открытую спецставку: за сутки до старта первого матча,
    # в день матча и «последний зов» (пороги в tournament_utils.SPECIAL_BET_REMINDER_THRESHOLDS).
    # Пороговый подход + claim_reminder (как send_joker_threshold_reminder_if_due) надёжен к дрейфу
    # планировщика. Чемпион и группы независимы: свои ключи, свой гард открытия, своё гашение порогов.
    events = database.get_all_events()
    if len(events) == 0:
        return
    start = tournament_utils.get_tournament_start(events)
    now_utc = datetime_utils.get_utc_time()
    if start is None or now_utc >= start:
        return  # после старта добивает check_special_bets_close (разовое «вы опоздали»)
    tournament = database.get_tournament()
    if tournament is None:
        return
    if not tournament.champion_bet_open and not tournament.group_bet_open:
        return

    due_label, crossed = tournament_utils.select_due_threshold(
        now_utc, start, tournament_utils.SPECIAL_BET_REMINDER_THRESHOLDS
    )
    if due_label is None:
        return

    if tournament.champion_bet_open:
        if database.claim_reminder(f'special_remind:champion:{due_label}:{start.isoformat()}'):
            without_bet = [u for u in database.get_all_users() if not database.get_champion_bet(u.id)]
            send_special_bet_reminder(strings.CHAMPION_REMINDER_HEADERS[due_label], without_bet)
        # Гасим менее срочные пройденные пороги, чтобы они не выстрелили запоздалым сообщением.
        for label in crossed:
            if label != due_label:
                database.claim_reminder(f'special_remind:champion:{label}:{start.isoformat()}')

    if tournament.group_bet_open:
        if database.claim_reminder(f'special_remind:group:{due_label}:{start.isoformat()}'):
            group_ids = [group.id for group in tournament.groups]
            without_bet = [
                u for u in database.get_all_users()
                if tournament_utils.missing_group_ids(database.get_group_champion_bets(u.id), group_ids)
            ]
            send_special_bet_reminder(strings.GROUP_REMINDER_HEADERS[due_label], without_bet)
        for label in crossed:
            if label != due_label:
                database.claim_reminder(f'special_remind:group:{label}:{start.isoformat()}')


def check_special_bets_close():
    # Авто-закрытие спецставок при старте первого матча: разовое «вы опоздали» тем,
    # кто не сделал открытую ставку. Сам приём закрывается «по часам» (is_betting_closed),
    # здесь только разовая рассылка. Идемпотентно через claim_reminder (как burned-at-playoff).
    events = database.get_all_events()
    if len(events) == 0:
        return
    start = tournament_utils.get_tournament_start(events)
    if start is None or datetime_utils.get_utc_time() < start:
        return
    tournament = database.get_tournament()
    if tournament is None:
        return
    if tournament.champion_bet_open and database.claim_reminder(f'special_bet_late:champion:{start.isoformat()}'):
        for user_model in database.get_all_users():
            if not database.get_champion_bet(user_model.id):
                send_joker_reminder_message(chat_id=user_model.id, text=strings.CHAMPION_BET_MISSED)
    if tournament.group_bet_open and database.claim_reminder(f'special_bet_late:group:{start.isoformat()}'):
        for user_model in database.get_all_users():
            if not database.get_group_champion_bets(user_model.id):
                send_joker_reminder_message(chat_id=user_model.id, text=strings.GROUP_BET_MISSED)


api_token_missing_logged = False  # чтобы предупредить об отсутствии токена один раз, а не каждые 10 минут


# Авто-завершение матчей: пока идёт хотя бы один матч, раз в тик спрашиваем у football-data.org
# завершённые матчи и закрываем наши события тем же путём, что и ручной /result.
# Любая ошибка здесь не должна сорвать остальные проверки тика, поэтому всё в try/except.
def check_api_results():
    global api_token_missing_logged
    try:
        token = os.environ.get(constants.ENV_FOOTBALL_DATA_TOKEN, '').strip()
        if not token:
            if not api_token_missing_logged:
                logging.warning(f'{constants.ENV_FOOTBALL_DATA_TOKEN} is not set, '
                                'auto-finishing events by API is disabled')
                api_token_missing_logged = True
            return
        events_in_progress = list(filter(lambda x: x.is_in_progress(), database.get_all_events()))
        if len(events_in_progress) == 0:
            logging.info('football-data.org auto-finish skipped: no events in progress')
            return
        # Окно от даты самого раннего идущего матча до завтра: ночные матчи могут
        # начаться до полуночи UTC, а закончиться после.
        date_from = min(map(lambda x: x.get_time_in_utc(), events_in_progress)).date()
        date_to = (datetime_utils.get_utc_time() + timedelta(days=1)).date()
        logging.info(f'Checking football-data.org for {len(events_in_progress)} event(s) in progress '
                     f'from {date_from.isoformat()} to {date_to.isoformat()}')
        api_matches = football_api.fetch_matches(token=token, date_from=date_from, date_to=date_to)
        if api_matches is None:
            return  # причина уже в логе; сработает обычный алерт о незавершённых матчах
        logging.info(f'football-data.org returned {len(api_matches)} parsed match(es)')
        for event in events_in_progress:
            try:
                settle_event_from_api(event=event, api_matches=api_matches)
            except Exception as e:
                logging.exception(e)  # ошибка по одному матчу не должна помешать остальным
    except Exception as e:
        logging.exception(e)


def settle_event_from_api(event: Event, api_matches: list):
    api_match, reason = football_api.find_api_match_for_event(event=event, api_matches=api_matches)
    if api_match is None:
        if reason.startswith('unmapped_team') and database.claim_reminder(f'api_unmapped:{event.uuid}'):
            # Незамапленная команда — постоянная проблема, маякнём мейнтейнеру один раз.
            # Best-effort отправка: один заблокировавший бота получатель не должен лишить алерта остальных.
            msg = strings.API_UNMAPPED_EVENT % (event.team_1, event.team_2)
            msg += '\n'
            msg += event.uuid
            for user_id in get_maintainer_ids():
                send_joker_reminder_message(chat_id=user_id, text=msg)
        else:
            # not_found/ambiguous — возможно транзиентно; часовой алерт прикроет.
            logging.info(f'No API match for event {event.uuid} ({event.team_1} – {event.team_2}): {reason}')
        return
    if api_match.status not in football_api.FINAL_STATUSES:
        logging.info(f'API match for event {event.uuid} ({event.team_1} – {event.team_2}) '
                     f'is not final yet: status={api_match.status}')
        return  # матч ещё идёт — это норма
    result, reason = football_api.build_event_result(event=event, api_match=api_match)
    if result is None:
        logging.warning(f'Cannot build result for event {event.uuid} '
                        f'({event.team_1} – {event.team_2}) from API match '
                        f'{api_match.home_team} – {api_match.away_team}: {reason}')
        return
    if finish_event_and_announce(event=event, result=result):
        logging.info(f'Auto-finished event {event.uuid} ({event.team_1} – {event.team_2}) '
                     f'with result {result.team_1_scores}:{result.team_2_scores}')


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
        msg += '\n'
        msg += event.uuid
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


scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.start()


if __name__ == '__main__':
    bot.infinity_polling()
