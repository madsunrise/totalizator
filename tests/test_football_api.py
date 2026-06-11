import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import football_api
from models import Event, EventType


def make_event(team_1: str, team_2: str, event_type: EventType = EventType.GROUP_STAGE,
               time: datetime = None) -> Event:
    return Event(
        uuid='test-uuid',
        team_1=team_1,
        team_2=team_2,
        time=time or datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc),
        event_type=event_type,
    )


def make_match_dict(home: str, away: str, home_tla: str = '', away_tla: str = '',
                    utc_date: str = '2026-06-15T19:00:00Z', status: str = 'FINISHED',
                    score: dict = None) -> dict:
    return {
        'utcDate': utc_date,
        'status': status,
        'homeTeam': {'name': home, 'shortName': home, 'tla': home_tla},
        'awayTeam': {'name': away, 'shortName': away, 'tla': away_tla},
        'score': score or {},
    }


def regular_score(home: int, away: int, winner: str = None) -> dict:
    if winner is None:
        winner = 'HOME_TEAM' if home > away else ('AWAY_TEAM' if away > home else 'DRAW')
    return {
        'winner': winner,
        'duration': 'REGULAR',
        'fullTime': {'home': home, 'away': away},
        'halfTime': {'home': 0, 'away': 0},
    }


class ParseMatchesResponseTest(unittest.TestCase):
    def test_parses_regular_finished_match(self):
        payload = {'matches': [make_match_dict('Spain', 'Germany', 'ESP', 'GER',
                                               score=regular_score(2, 1))]}
        matches = football_api.parse_matches_response(payload)
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m.status, 'FINISHED')
        self.assertEqual(m.full_time, (2, 1))
        self.assertEqual(m.winner, 'HOME_TEAM')
        self.assertEqual(m.utc_date, datetime(2026, 6, 15, 19, 0, tzinfo=timezone.utc))
        self.assertIn('esp', m.home_team_keys)

    def test_parses_legacy_score_keys(self):
        # В старой документации v4 пары счёта имеют ключи homeTeam/awayTeam вместо home/away.
        score = {
            'winner': 'HOME_TEAM',
            'duration': 'PENALTY_SHOOTOUT',
            'fullTime': {'homeTeam': 4, 'awayTeam': 3},
            'regularTime': {'homeTeam': 1, 'awayTeam': 1},
            'penalties': {'homeTeam': 3, 'awayTeam': 2},
        }
        payload = {'matches': [make_match_dict('Spain', 'Germany', score=score)]}
        m = football_api.parse_matches_response(payload)[0]
        self.assertEqual(m.full_time, (4, 3))
        self.assertEqual(m.regular_time, (1, 1))

    def test_missing_scores_give_none_pairs(self):
        payload = {'matches': [make_match_dict('Spain', 'Germany', status='TIMED',
                                               score={'winner': None, 'duration': None,
                                                      'fullTime': {'home': None, 'away': None}})]}
        m = football_api.parse_matches_response(payload)[0]
        self.assertIsNone(m.full_time)
        self.assertIsNone(m.regular_time)

    def test_negative_scores_rejected(self):
        payload = {'matches': [make_match_dict('Spain', 'Germany',
                                               score={'fullTime': {'home': -1, 'away': 2}})]}
        m = football_api.parse_matches_response(payload)[0]
        self.assertIsNone(m.full_time)

    def test_unparseable_entries_skipped(self):
        payload = {'matches': [
            {'utcDate': 'not-a-date', 'status': 'FINISHED', 'homeTeam': {'name': 'A'}, 'awayTeam': {'name': 'B'}},
            {'status': 'FINISHED'},
            'garbage',
            None,
            42,
            make_match_dict('Spain', 'Germany', score={'homeTeam': 'Spain'}),
            make_match_dict('Spain', 'Germany', score=regular_score(1, 0)),
        ]}
        matches = football_api.parse_matches_response(payload)
        self.assertEqual(len(matches), 2)

    def test_non_dict_team_entries_skipped(self):
        match_dict = make_match_dict('Spain', 'Germany', score=regular_score(1, 0))
        match_dict['homeTeam'] = 'Spain'
        self.assertEqual(football_api.parse_matches_response({'matches': [match_dict]}), [])

    def test_empty_payload(self):
        self.assertEqual(football_api.parse_matches_response({}), [])

    def test_non_dict_payload(self):
        self.assertEqual(football_api.parse_matches_response('not json object'), [])
        self.assertEqual(football_api.parse_matches_response({'matches': 'abc'}), [])


class FetchMatchesTest(unittest.TestCase):
    def make_response(self, status_code: int = 200, headers: dict = None, payload: dict = None):
        response = mock.Mock()
        response.status_code = status_code
        response.headers = headers or {}
        response.json.return_value = payload if payload is not None else {'matches': []}
        response.text = ''
        return response

    def test_success(self):
        payload = {'matches': [make_match_dict('Spain', 'Germany', score=regular_score(2, 0))]}
        with mock.patch.object(football_api.requests, 'get',
                               return_value=self.make_response(payload=payload)) as mock_get:
            matches = football_api.fetch_matches('token', datetime(2026, 6, 15).date(),
                                                 datetime(2026, 6, 16).date())
        self.assertEqual(len(matches), 1)
        # Контракт запроса: правильный URL, токен в заголовке, окно дат, таймаут.
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], football_api.MATCHES_URL)
        self.assertEqual(kwargs['headers'], {'X-Auth-Token': 'token'})
        self.assertEqual(kwargs['params'], {'dateFrom': '2026-06-15', 'dateTo': '2026-06-16'})
        self.assertIn('timeout', kwargs)

    def test_non_json_response_returns_none(self):
        response = self.make_response()
        response.json.side_effect = ValueError('Expecting value')
        with mock.patch.object(football_api.requests, 'get', return_value=response):
            with self.assertLogs(level='WARNING') as logs:
                result = football_api.fetch_matches('token', datetime(2026, 6, 15).date(),
                                                    datetime(2026, 6, 16).date())
        self.assertIsNone(result)
        self.assertTrue(any('non-JSON' in line for line in logs.output))

    def test_rate_limited_returns_none(self):
        response = self.make_response(status_code=429, headers={'X-RequestCounter-Reset': '42'})
        with mock.patch.object(football_api.requests, 'get', return_value=response):
            with self.assertLogs(level='WARNING') as logs:
                result = football_api.fetch_matches('token', datetime(2026, 6, 15).date(),
                                                    datetime(2026, 6, 16).date())
        self.assertIsNone(result)
        self.assertTrue(any('rate limit' in line for line in logs.output))

    def test_low_requests_available_logs_warning(self):
        # Название заголовка различается в документации и реальных ответах — поддерживаем все варианты.
        for header_name in ('X-Requests-Available-Minute', 'X-Requests-Available', 'X-RequestsAvailable'):
            response = self.make_response(headers={header_name: '2'})
            with mock.patch.object(football_api.requests, 'get', return_value=response):
                with self.assertLogs(level='WARNING') as logs:
                    result = football_api.fetch_matches('token', datetime(2026, 6, 15).date(),
                                                        datetime(2026, 6, 16).date())
            self.assertTrue(any('2 requests available' in line for line in logs.output), header_name)
            # Предупреждение — не отказ: запрос всё равно обработан.
            self.assertEqual(result, [], header_name)

    def test_http_error_returns_none(self):
        with mock.patch.object(football_api.requests, 'get',
                               return_value=self.make_response(status_code=403)):
            with self.assertLogs(level='WARNING'):
                result = football_api.fetch_matches('token', datetime(2026, 6, 15).date(),
                                                    datetime(2026, 6, 16).date())
        self.assertIsNone(result)

    def test_network_error_returns_none(self):
        import requests as requests_lib
        with mock.patch.object(football_api.requests, 'get',
                               side_effect=requests_lib.ConnectionError('boom')):
            with self.assertLogs(level='WARNING'):
                result = football_api.fetch_matches('token', datetime(2026, 6, 15).date(),
                                                    datetime(2026, 6, 16).date())
        self.assertIsNone(result)


class FindApiMatchForEventTest(unittest.TestCase):
    def parse_one(self, match_dict: dict):
        return football_api.parse_matches_response({'matches': [match_dict]})[0]

    def test_direct_orientation(self):
        event = make_event('Испания', 'Германия')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        found, reason = football_api.find_api_match_for_event(event, [api_match])
        self.assertIsNotNone(found)
        self.assertEqual(reason, '')

    def test_swapped_orientation(self):
        event = make_event('Германия', 'Испания')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        found, _ = football_api.find_api_match_for_event(event, [api_match])
        self.assertIsNotNone(found)

    def test_match_by_tla_when_name_differs(self):
        # У нас "Чехия" -> в т.ч. ключ 'cze'; API может звать команду иначе ("Czechia"
        # уже есть в маппинге, но проверяем якорь через tla при незнакомом name).
        event = make_event('Чехия', 'Англия')
        api_match = self.parse_one(make_match_dict('Czech National Team', 'England',
                                                   'CZE', 'ENG', score=regular_score(0, 0)))
        found, _ = football_api.find_api_match_for_event(event, [api_match])
        self.assertIsNotNone(found)

    def test_simultaneous_kickoffs_disambiguated_by_teams(self):
        # Последний тур группы: два матча в одно и то же время.
        event = make_event('Испания', 'Германия')
        other = self.parse_one(make_match_dict('France', 'Brazil', score=regular_score(1, 1)))
        target = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        found, _ = football_api.find_api_match_for_event(event, [other, target])
        self.assertIs(found, target)

    def test_kickoff_outside_tolerance_not_found(self):
        event = make_event('Испания', 'Германия')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany',
                                                   utc_date='2026-06-15T16:00:00Z',
                                                   score=regular_score(2, 1)))
        found, reason = football_api.find_api_match_for_event(event, [api_match])
        self.assertIsNone(found)
        self.assertEqual(reason, 'not_found')

    def test_kickoff_tolerance_boundary(self):
        # Ровно 15 минут — ещё совпадение, 16 — уже нет.
        event = make_event('Испания', 'Германия')
        at_boundary = self.parse_one(make_match_dict('Spain', 'Germany',
                                                     utc_date='2026-06-15T19:15:00Z',
                                                     score=regular_score(2, 1)))
        found, _ = football_api.find_api_match_for_event(event, [at_boundary])
        self.assertIsNotNone(found)

        past_boundary = self.parse_one(make_match_dict('Spain', 'Germany',
                                                       utc_date='2026-06-15T19:16:00Z',
                                                       score=regular_score(2, 1)))
        found, reason = football_api.find_api_match_for_event(event, [past_boundary])
        self.assertIsNone(found)
        self.assertEqual(reason, 'not_found')

    def test_unmapped_team(self):
        event = make_event('Нарния', 'Германия')
        found, reason = football_api.find_api_match_for_event(event, [])
        self.assertIsNone(found)
        self.assertEqual(reason, 'unmapped_team:Нарния')

    def test_duplicate_candidates_ambiguous(self):
        event = make_event('Испания', 'Германия')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        found, reason = football_api.find_api_match_for_event(event, [api_match, api_match])
        self.assertIsNone(found)
        self.assertEqual(reason, 'ambiguous')


class BuildEventResultTest(unittest.TestCase):
    def parse_one(self, match_dict: dict):
        return football_api.parse_matches_response({'matches': [match_dict]})[0]

    def test_group_stage_with_swap(self):
        # У нас команды записаны в обратном порядке относительно API: счёт переворачивается.
        event = make_event('Германия', 'Испания')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertEqual(reason, '')
        self.assertEqual(result.team_1_scores, 1)
        self.assertEqual(result.team_2_scores, 2)
        self.assertIsNone(result.team_1_has_gone_through)

    def test_playoff_single_regular_time_winner(self):
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        result, _ = football_api.build_event_result(event, api_match)
        self.assertTrue(result.team_1_has_gone_through)

    def test_playoff_single_winner_derived_from_score_if_missing(self):
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        score = regular_score(0, 2)
        score['winner'] = None
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, _ = football_api.build_event_result(event, api_match)
        self.assertFalse(result.team_1_has_gone_through)

    def test_playoff_extra_time_records_regular_time_draw(self):
        # По правилам тотализатора записываем счёт основного времени (ничья) + кто прошёл.
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        score = {
            'winner': 'AWAY_TEAM',
            'duration': 'EXTRA_TIME',
            'fullTime': {'home': 1, 'away': 2},
            'regularTime': {'home': 1, 'away': 1},
            'extraTime': {'home': 0, 'away': 1},
        }
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, _ = football_api.build_event_result(event, api_match)
        self.assertEqual((result.team_1_scores, result.team_2_scores), (1, 1))
        self.assertFalse(result.team_1_has_gone_through)

    def test_playoff_penalties_with_legacy_keys_and_swap(self):
        event = make_event('Германия', 'Испания', EventType.PLAY_OFF_SINGLE_MATCH)
        score = {
            'winner': 'HOME_TEAM',
            'duration': 'PENALTY_SHOOTOUT',
            'fullTime': {'homeTeam': 4, 'awayTeam': 3},
            'regularTime': {'homeTeam': 1, 'awayTeam': 1},
            'penalties': {'homeTeam': 3, 'awayTeam': 2},
        }
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, _ = football_api.build_event_result(event, api_match)
        self.assertEqual((result.team_1_scores, result.team_2_scores), (1, 1))
        # Победил HOME_TEAM (Испания) = team_2 нашего события.
        self.assertFalse(result.team_1_has_gone_through)

    def test_in_play_not_finished(self):
        event = make_event('Испания', 'Германия')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', status='IN_PLAY',
                                                   score=regular_score(1, 0)))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'not_finished')

    def test_awarded_is_final(self):
        event = make_event('Испания', 'Германия')
        score = {'winner': 'HOME_TEAM', 'duration': None, 'fullTime': {'home': 3, 'away': 0}}
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', status='AWARDED', score=score))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertEqual(reason, '')
        self.assertEqual((result.team_1_scores, result.team_2_scores), (3, 0))

    def test_second_leg_never_auto_finished(self):
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SECOND_MATCH)
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'second_leg_manual_only')

    def test_extra_time_without_regular_time_rejected(self):
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        score = {'winner': 'HOME_TEAM', 'duration': 'EXTRA_TIME', 'fullTime': {'home': 2, 'away': 1}}
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'no_regular_time')

    def test_extra_time_with_non_draw_regular_time_rejected(self):
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        score = {
            'winner': 'HOME_TEAM',
            'duration': 'EXTRA_TIME',
            'fullTime': {'home': 3, 'away': 1},
            'regularTime': {'home': 2, 'away': 1},
        }
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'inconsistent_regular_time')

    def test_playoff_draw_without_winner_rejected(self):
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        score = {'winner': None, 'duration': 'REGULAR', 'fullTime': {'home': 1, 'away': 1}}
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'no_winner')

    def test_playoff_contradictory_winner_rejected(self):
        # winner=DRAW при не-ничейном счёте — противоречивые данные, не угадываем.
        event = make_event('Испания', 'Германия', EventType.PLAY_OFF_SINGLE_MATCH)
        score = {'winner': 'DRAW', 'duration': 'REGULAR', 'fullTime': {'home': 2, 'away': 1}}
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=score))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'inconsistent_winner')

    def test_wrong_teams_orientation_mismatch(self):
        event = make_event('Испания', 'Франция')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'orientation_mismatch')

    def test_half_swapped_orientation_mismatch(self):
        # team_1 совпадает с гостями API, но team_2 не совпадает с хозяевами — отклоняем.
        event = make_event('Германия', 'Франция')
        api_match = self.parse_one(make_match_dict('Spain', 'Germany', score=regular_score(2, 1)))
        result, reason = football_api.build_event_result(event, api_match)
        self.assertIsNone(result)
        self.assertEqual(reason, 'orientation_mismatch')


if __name__ == '__main__':
    unittest.main()
