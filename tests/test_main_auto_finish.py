# Headless-тесты интеграции авто-завершения в main.py: импортируем main без Telegram/Mongo
# (telebot и pymongo не ходят в сеть при конструировании), подменяем bot и database фейками.
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault('TELEGRAM_BOT_TOKEN', '1:test')
os.environ['TELEGRAM_TARGET_CHAT_ID'] = '-100500'
os.environ['TELEGRAM_MAINTAINER_IDS'] = '42'
os.environ.setdefault('DATABASE_NAME', 'totalizator_test')
os.environ['FOOTBALL_DATA_API_TOKEN'] = ''

# При импорте main стартует поток планировщика — глушим Thread.start на время импорта.
_original_thread_start = threading.Thread.start
threading.Thread.start = lambda self: None
try:
    import main
finally:
    threading.Thread.start = _original_thread_start

import football_api
from models import Event, EventResult, EventType

TARGET_CHAT_ID = -100500
MAINTAINER_ID = 42


class FakeBot:
    def __init__(self):
        self.sent = []  # [(chat_id, text)]

    def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent.append((chat_id, text))

    def messages_to(self, chat_id):
        return [text for sent_chat_id, text in self.sent if sent_chat_id == chat_id]


class FakeDatabase:
    def __init__(self, events=None):
        self.events = events or []
        self.claimed = set()

    def get_all_events(self):
        return list(self.events)

    def get_event_by_uuid(self, uuid):
        return next((e for e in self.events if e.uuid == uuid), None)

    def update_event(self, event):
        for i, existing in enumerate(self.events):
            if existing.uuid == event.uuid:
                self.events[i] = event
                return
        raise ValueError('Event does not exist')

    def get_all_users(self):
        return []

    def claim_reminder(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def register_user_if_required(self, **kwargs):
        return False

    def update_last_interaction(self, user_id):
        pass

    def check_if_user_exists(self, user_id, raise_error=False):
        return True


def make_event(event_type=EventType.GROUP_STAGE, started_hours_ago=2) -> Event:
    return Event(
        uuid='event-uuid-1',
        team_1='Испания',
        team_2='Германия',
        time=datetime.now(timezone.utc) - timedelta(hours=started_hours_ago),
        event_type=event_type,
    )


def make_finished_api_match(event: Event, home_score=2, away_score=1):
    payload = {'matches': [{
        'utcDate': event.get_time_in_utc().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'status': 'FINISHED',
        'homeTeam': {'name': 'Spain', 'shortName': 'Spain', 'tla': 'ESP'},
        'awayTeam': {'name': 'Germany', 'shortName': 'Germany', 'tla': 'GER'},
        'score': {
            'winner': 'HOME_TEAM' if home_score > away_score else 'AWAY_TEAM',
            'duration': 'REGULAR',
            'fullTime': {'home': home_score, 'away': away_score},
        },
    }]}
    return football_api.parse_matches_response(payload)


class CheckApiResultsTest(unittest.TestCase):
    def setUp(self):
        self.event = make_event()
        main.bot = FakeBot()
        main.database = FakeDatabase(events=[self.event])

    def test_exception_never_escapes(self):
        # Инвариант: сбой авто-завершения не должен срывать остальные проверки тика.
        main.database.get_all_events = mock.Mock(side_effect=RuntimeError('db down'))
        with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
            main.check_api_results()  # не должно бросить

    def test_no_token_no_api_call(self):
        with mock.patch.object(main.football_api, 'fetch_matches') as fetch:
            with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': ''}):
                main.check_api_results()
        fetch.assert_not_called()

    def test_no_events_in_progress_no_api_call(self):
        self.event.result = EventResult(1, 0, None)
        with mock.patch.object(main.football_api, 'fetch_matches') as fetch:
            with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
                main.check_api_results()
        fetch.assert_not_called()

    def test_auto_finishes_event_and_announces(self):
        api_matches = make_finished_api_match(self.event)
        with mock.patch.object(main.football_api, 'fetch_matches', return_value=api_matches):
            with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
                main.check_api_results()
        self.assertIsNotNone(self.event.result)
        self.assertEqual(self.event.result.team_1_scores, 2)
        self.assertEqual(self.event.result.team_2_scores, 1)
        group_messages = main.bot.messages_to(TARGET_CHAT_ID)
        self.assertEqual(len(group_messages), 1)
        self.assertIn('Матч Испания – Германия завершился (2:1)', group_messages[0])
        # Личных сообщений мейнтейнеру при успешном авто-завершении не шлём.
        self.assertEqual(main.bot.messages_to(MAINTAINER_ID), [])

    def test_second_tick_does_not_refinish(self):
        api_matches = make_finished_api_match(self.event)
        with mock.patch.object(main.football_api, 'fetch_matches', return_value=api_matches):
            with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
                main.check_api_results()
                main.check_api_results()
        self.assertEqual(len(main.bot.messages_to(TARGET_CHAT_ID)), 1)

    def test_unmapped_team_alerts_maintainer_once(self):
        self.event.team_1 = 'Нарния'
        with mock.patch.object(main.football_api, 'fetch_matches', return_value=[]):
            with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
                main.check_api_results()
                main.check_api_results()
        maintainer_messages = main.bot.messages_to(MAINTAINER_ID)
        self.assertEqual(len(maintainer_messages), 1)
        self.assertIn('Нарния', maintainer_messages[0])
        self.assertIn(self.event.uuid, maintainer_messages[0])


class FinishEventAndAnnounceTest(unittest.TestCase):
    def setUp(self):
        self.event = make_event()
        main.bot = FakeBot()
        main.database = FakeDatabase(events=[self.event])

    def test_double_finish_guard(self):
        result = EventResult(2, 1, None)
        self.assertTrue(main.finish_event_and_announce(event=self.event, result=result))
        self.assertFalse(main.finish_event_and_announce(event=self.event, result=result))
        self.assertEqual(len(main.bot.messages_to(TARGET_CHAT_ID)), 1)

    def test_group_send_failure_alerts_maintainer(self):
        # Очки уже начислены, но итоги в группу не ушли — мейнтейнер должен узнать.
        def failing_send(chat_id=None, text=None, **kwargs):
            if chat_id == TARGET_CHAT_ID:
                raise RuntimeError('telegram down')
            main.bot.sent.append((chat_id, text))

        main.bot.send_message = failing_send
        finished = main.finish_event_and_announce(event=self.event, result=EventResult(2, 1, None))
        self.assertTrue(finished)
        self.assertIsNotNone(main.database.get_event_by_uuid(self.event.uuid).result)
        maintainer_messages = main.bot.messages_to(MAINTAINER_ID)
        self.assertTrue(any('не отправились в группу' in text for text in maintainer_messages))


class ManualResultCommandTest(unittest.TestCase):
    # Инвариант рефакторинга: ручной /result ведёт себя как раньше —
    # сначала подтверждение в чат мейнтейнера, затем полные итоги в группу.
    def setUp(self):
        self.event = make_event(event_type=EventType.PLAY_OFF_SINGLE_MATCH)
        main.bot = FakeBot()
        main.database = FakeDatabase(events=[self.event])

    def make_message(self, text: str):
        user = SimpleNamespace(id=MAINTAINER_ID, username='m', first_name='M', last_name='',
                               full_name='M')
        return SimpleNamespace(from_user=user, chat=SimpleNamespace(id=777), text=text)

    def test_result_command_sets_result_and_announces(self):
        main.set_result_for_event(self.make_message(f'/result {self.event.uuid} 2:1'))
        self.assertEqual(self.event.result.team_1_scores, 2)
        self.assertEqual(self.event.result.team_2_scores, 1)
        self.assertTrue(self.event.result.team_1_has_gone_through)
        confirmation = main.bot.messages_to(777)
        self.assertEqual(len(confirmation), 1)
        self.assertIn('Матч Испания – Германия завершился (2:1). Проходит Испания.', confirmation[0])
        group_messages = main.bot.messages_to(TARGET_CHAT_ID)
        self.assertEqual(len(group_messages), 1)
        self.assertIn('Никто не угадал результат.', group_messages[0])
        # Подтверждение мейнтейнеру отправляется раньше публикации в группу.
        self.assertEqual(main.bot.sent[0][0], 777)

    def test_result_command_rejects_already_finished(self):
        self.event.result = EventResult(1, 1, True)
        main.set_result_for_event(self.make_message(f'/result {self.event.uuid} 2:1'))
        self.assertEqual(self.event.result.team_1_scores, 1)
        self.assertTrue(any('уже записан результат' in text for text in main.bot.messages_to(777)))
        self.assertEqual(main.bot.messages_to(TARGET_CHAT_ID), [])


if __name__ == '__main__':
    unittest.main()
