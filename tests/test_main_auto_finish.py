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
from models import Bet, Event, EventResult, EventType, UserModel

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
    def __init__(self, events=None, users=None):
        self.events = events or []
        self.users = users or []
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
        return list(self.users)

    def find_bet(self, user_id, event_uuid):
        user = next((u for u in self.users if u.id == user_id), None)
        if user is None:
            return None
        return next((b for b in user.bets if b.event_uuid == event_uuid), None)

    def add_scores_to_user(self, user_id, amount):
        user = next((u for u in self.users if u.id == user_id), None)
        if user is None:
            raise ValueError('User does not exist')
        user.scores = max(user.scores + amount, 0)

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


def make_user(user_id: int, first_name: str, bets=None, scores=0) -> UserModel:
    return UserModel(
        id=user_id,
        username=first_name.lower(),
        first_name=first_name,
        last_name='',
        last_interaction=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        scores=scores,
        bets=bets or [],
    )


def make_bet(user_id: int, event: Event, team_1_scores: int, team_2_scores: int,
             is_joker: bool = False) -> Bet:
    return Bet(
        user_id=user_id,
        event_uuid=event.uuid,
        team_1_scores=team_1_scores,
        team_2_scores=team_2_scores,
        team_1_will_go_through=None,
        created_at=datetime.now(timezone.utc),
        is_joker=is_joker,
    )


def make_api_match_with_status(event: Event, status: str):
    payload = {'matches': [{
        'utcDate': event.get_time_in_utc().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'status': status,
        'homeTeam': {'name': 'Spain', 'shortName': 'Spain', 'tla': 'ESP'},
        'awayTeam': {'name': 'Germany', 'shortName': 'Germany', 'tla': 'GER'},
        'score': {},
    }]}
    return football_api.parse_matches_response(payload)


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
        main.api_poll_logged_states.clear()  # события в тестах делят один uuid

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

    def test_in_progress_status_logged_only_on_change(self):
        # Опрос идёт раз в 30 секунд: неизменное состояние матча не должно плодить строки в логе,
        # но смена статуса должна попадать в лог.
        with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
            with mock.patch.object(main.football_api, 'fetch_matches',
                                   return_value=make_api_match_with_status(self.event, 'IN_PLAY')):
                with self.assertLogs(level='INFO') as logs:
                    main.check_api_results()
                    main.check_api_results()
            self.assertEqual(len([line for line in logs.output if 'not final yet' in line]), 1)
            with mock.patch.object(main.football_api, 'fetch_matches',
                                   return_value=make_api_match_with_status(self.event, 'PAUSED')):
                with self.assertLogs(level='INFO') as logs:
                    main.check_api_results()
            self.assertEqual(len([line for line in logs.output if 'status=PAUSED' in line]), 1)

    def test_cannot_build_result_warns_once(self):
        # Второй матч пары всегда закрывается вручную — завершённый в API матч будет
        # давать second_leg_manual_only при каждом опросе, но WARNING должен быть один.
        self.event = make_event(event_type=EventType.PLAY_OFF_SECOND_MATCH)
        main.database = FakeDatabase(events=[self.event])
        api_matches = make_finished_api_match(self.event)
        with mock.patch.object(main.football_api, 'fetch_matches', return_value=api_matches):
            with mock.patch.dict(os.environ, {'FOOTBALL_DATA_API_TOKEN': 'token'}):
                with self.assertLogs(level='WARNING') as logs:
                    main.check_api_results()
                    main.check_api_results()
        warning_lines = [line for line in logs.output if 'second_leg_manual_only' in line]
        self.assertEqual(len(warning_lines), 1)
        self.assertTrue(warning_lines[0].startswith('WARNING:'))
        self.assertIsNone(self.event.result)

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

    def test_announces_only_triggered_jokers(self):
        active_joker = make_user(
            user_id=101,
            first_name='Анна',
            bets=[make_bet(101, self.event, 2, 1, is_joker=True)],
        )
        burned_joker = make_user(
            user_id=102,
            first_name='Борис',
            bets=[make_bet(102, self.event, 0, 0, is_joker=True)],
        )
        regular_guesser = make_user(
            user_id=103,
            first_name='Вера',
            bets=[make_bet(103, self.event, 3, 2, is_joker=False)],
        )
        main.database = FakeDatabase(events=[self.event], users=[active_joker, burned_joker, regular_guesser])

        finished = main.finish_event_and_announce(event=self.event, result=EventResult(2, 1, None))

        self.assertTrue(finished)
        self.assertEqual(active_joker.scores, 8)
        self.assertEqual(burned_joker.scores, 0)
        self.assertEqual(regular_guesser.scores, 3)
        group_message = main.bot.messages_to(TARGET_CHAT_ID)[0]
        self.assertIn('Сработали джокеры:\nАнна\n\n', group_message)
        self.assertNotIn('Сработали джокеры:\nБорис', group_message)
        self.assertNotIn('Сработали джокеры:\nВера', group_message)

    def test_announces_leaderboard_movement_when_rank_changed(self):
        leader = make_user(
            user_id=101,
            first_name='Анна',
            scores=8,
            bets=[make_bet(101, self.event, 0, 0)],
        )
        riser = make_user(
            user_id=102,
            first_name='Борис',
            scores=6,
            bets=[make_bet(102, self.event, 2, 1)],
        )
        third = make_user(
            user_id=103,
            first_name='Вера',
            scores=5,
            bets=[make_bet(103, self.event, 0, 0)],
        )
        main.database = FakeDatabase(events=[self.event], users=[leader, riser, third])

        finished = main.finish_event_and_announce(event=self.event, result=EventResult(2, 1, None))

        self.assertTrue(finished)
        group_message = main.bot.messages_to(TARGET_CHAT_ID)[0]
        self.assertIn('Движение в таблице:', group_message)
        self.assertIn('Рывок матча: Борис с 2-го на 1-е место (+1 позиция).', group_message)
        self.assertIn('Первое место теперь единолично: Борис.', group_message)

    def test_skips_leaderboard_movement_when_nothing_changed(self):
        first = make_user(
            user_id=101,
            first_name='Анна',
            scores=8,
            bets=[make_bet(101, self.event, 0, 0)],
        )
        second = make_user(
            user_id=102,
            first_name='Борис',
            scores=6,
            bets=[make_bet(102, self.event, 0, 0)],
        )
        main.database = FakeDatabase(events=[self.event], users=[first, second])

        finished = main.finish_event_and_announce(event=self.event, result=EventResult(2, 1, None))

        self.assertTrue(finished)
        group_message = main.bot.messages_to(TARGET_CHAT_ID)[0]
        self.assertNotIn('Движение в таблице:', group_message)


class LeaderboardMovementFactsTest(unittest.TestCase):
    def make_snapshot(self, users):
        return main.build_leaderboard_snapshot(users)

    def test_leader_gap_uses_correct_points_grammar(self):
        before = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=8),
        ])
        after = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=9),
        ])

        facts = main.build_leaderboard_movement_facts(before=before, after=after)

        self.assertIn('Отрыв лидера сократился: теперь впереди на 1 очко.', facts)

    def test_announces_shared_first_place_only_when_it_appears_now(self):
        before = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=9),
        ])
        after = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=10),
        ])

        facts = main.build_leaderboard_movement_facts(before=before, after=after)

        self.assertIn('Первое место теперь делят: Анна и Борис.', facts)

    def test_announces_first_points(self):
        before = self.make_snapshot([
            make_user(101, 'Анна', scores=3),
            make_user(102, 'Борис', scores=0),
            make_user(103, 'Вера', scores=0),
        ])
        after = self.make_snapshot([
            make_user(101, 'Анна', scores=3),
            make_user(102, 'Борис', scores=1),
            make_user(103, 'Вера', scores=0),
        ])

        facts = main.build_leaderboard_movement_facts(before=before, after=after)

        self.assertIn('Первые очки турнира: Борис.', facts)

    def test_announces_top_three_entry(self):
        before = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=9),
            make_user(103, 'Вера', scores=8),
            make_user(104, 'Глеб', scores=6),
        ])
        after = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=9),
            make_user(103, 'Вера', scores=8),
            make_user(104, 'Глеб', scores=8),
        ])

        fact = main.build_top_entry_fact(before=before, after=after)

        self.assertEqual(fact, 'Глеб теперь в топ-3.')

    def test_announces_tight_top_five_only_when_threshold_crossed(self):
        before = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=9),
            make_user(103, 'Вера', scores=8),
            make_user(104, 'Глеб', scores=7),
            make_user(105, 'Даша', scores=6),
        ])
        after = self.make_snapshot([
            make_user(101, 'Анна', scores=10),
            make_user(102, 'Борис', scores=9),
            make_user(103, 'Вера', scores=8),
            make_user(104, 'Глеб', scores=8),
            make_user(105, 'Даша', scores=8),
        ])

        fact = main.build_tight_top_fact(before=before, after=after)

        self.assertEqual(fact, 'Топ-5 теперь разделяют всего 2 очка.')


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


class DetailedAnalyticsTest(unittest.TestCase):
    def make_finished_event(self, uuid: str, team_1_scores: int, team_2_scores: int) -> Event:
        return Event(
            uuid=uuid,
            team_1=f'{uuid}-1',
            team_2=f'{uuid}-2',
            time=datetime.now(timezone.utc) - timedelta(hours=2),
            event_type=EventType.GROUP_STAGE,
            result=EventResult(team_1_scores, team_2_scores, None),
        )

    def make_pending_event(self, uuid: str) -> Event:
        return Event(
            uuid=uuid,
            team_1=f'{uuid}-1',
            team_2=f'{uuid}-2',
            time=datetime.now(timezone.utc) + timedelta(hours=2),
            event_type=EventType.GROUP_STAGE,
        )

    def test_reports_triggered_jokers_and_bonus_points_for_finished_matches_only(self):
        exact_score_event = self.make_finished_event('exact', 2, 1)
        goal_difference_event = self.make_finished_event('goal-difference', 3, 1)
        missed_event = self.make_finished_event('missed', 1, 0)
        pending_event = self.make_pending_event('pending')
        user = make_user(101, 'Анна', bets=[
            make_bet(101, exact_score_event, 2, 1, is_joker=True),
            make_bet(101, goal_difference_event, 2, 0, is_joker=True),
            make_bet(101, missed_event, 0, 0, is_joker=True),
            make_bet(101, pending_event, 4, 2, is_joker=True),
        ])
        main.database = FakeDatabase(
            events=[exact_score_event, goal_difference_event, missed_event, pending_event],
            users=[user],
        )

        statistic = main.get_user_detailed_statistic(user_model=user)
        text = main.get_user_statistic_formatted_text(statistic=statistic)

        self.assertEqual(statistic.triggered_jokers_count, 2)
        self.assertEqual(statistic.joker_bets_count, 3)
        self.assertEqual(statistic.joker_bonus_scores, 7)
        self.assertIn('Джокеры: 2/3, бонус: +7 очков', text)


if __name__ == '__main__':
    unittest.main()
