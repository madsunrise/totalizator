import unittest
from datetime import timezone

import event_utils
from models import EventType


class ParseEventLineTest(unittest.TestCase):
    def test_valid_group_line(self):
        pe, err = event_utils.parse_event_line('Мексика; ЮАР; 11.06.2026 22:00; group')
        self.assertIsNone(err)
        self.assertEqual(pe.team_1, 'Мексика')
        self.assertEqual(pe.team_2, 'ЮАР')
        self.assertEqual(pe.event_type, EventType.GROUP_STAGE)

    def test_moscow_to_utc_conversion(self):
        # 22:00 МСК (UTC+3) -> 19:00 UTC.
        pe, err = event_utils.parse_event_line('A; B; 11.06.2026 22:00; group')
        self.assertIsNone(err)
        self.assertEqual(pe.time_utc.astimezone(timezone.utc).hour, 19)
        self.assertEqual(pe.time_utc.utcoffset(), timezone.utc.utcoffset(None))

    def test_all_playoff_types(self):
        for token, et in [
            ('playoff_single', EventType.PLAY_OFF_SINGLE_MATCH),
            ('playoff_first_match', EventType.PLAY_OFF_FIRST_MATCH),
            ('playoff_second_match', EventType.PLAY_OFF_SECOND_MATCH),
        ]:
            pe, err = event_utils.parse_event_line(f'A; B; 01.07.2026 20:00; {token}')
            self.assertIsNone(err, token)
            self.assertEqual(pe.event_type, et)

    def test_wrong_field_count(self):
        _, err = event_utils.parse_event_line('A; B; 11.06.2026 22:00')
        self.assertIn('4 поля', err)

    def test_bad_date(self):
        _, err = event_utils.parse_event_line('A; B; 2026-06-11 22:00; group')
        self.assertIn('дату', err)

    def test_unknown_type(self):
        _, err = event_utils.parse_event_line('A; B; 11.06.2026 22:00; semifinal')
        self.assertIn('неизвестный тип', err)

    def test_empty_team(self):
        _, err = event_utils.parse_event_line('; B; 11.06.2026 22:00; group')
        self.assertIn('пустое название', err)

    def test_team_plays_itself(self):
        _, err = event_utils.parse_event_line('Бразилия; бразилия; 11.06.2026 22:00; group')
        self.assertIn('сама с собой', err)


class ParseEventsBlockTest(unittest.TestCase):
    def test_single_line_backward_compatible(self):
        events, errors = event_utils.parse_events_block('/add_event Мексика; ЮАР; 11.06.2026 22:00; group')
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 1)

    def test_multiline(self):
        text = ('/add_event\n'
                'Мексика; ЮАР; 11.06.2026 22:00; group\n'
                'Южная Корея; Чехия; 12.06.2026 05:00; group')
        events, errors = event_utils.parse_events_block(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].team_1, 'Южная Корея')

    def test_botname_suffix_multiline(self):
        text = ('/add_event@MyBot\n'
                'A; B; 11.06.2026 22:00; group\n'
                'C; D; 12.06.2026 05:00; group')
        events, errors = event_utils.parse_events_block(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 2)

    def test_collects_all_errors_and_adds_nothing(self):
        # all-or-nothing: при ошибках возвращаем пустой список событий, но все ошибки.
        text = ('/add_event\n'
                'A; B; 11.06.2026 22:00; group\n'
                'broken line\n'
                'C; D; bad-date; group')
        events, errors = event_utils.parse_events_block(text)
        self.assertEqual(events, [])
        self.assertEqual(len(errors), 2)

    def test_exact_duplicate_within_message_is_error(self):
        text = ('/add_event\n'
                'A; B; 11.06.2026 22:00; group\n'
                'A; B; 11.06.2026 22:00; group')
        events, errors = event_utils.parse_events_block(text)
        self.assertEqual(events, [])
        self.assertTrue(any('дубль' in e for e in errors))

    def test_case_variant_within_message_is_not_duplicate(self):
        # Дедуп точный (как database.find_event), поэтому разный регистр — разные матчи.
        text = ('/add_event\n'
                'A; B; 11.06.2026 22:00; group\n'
                'a; b; 11.06.2026 22:00; group')
        events, errors = event_utils.parse_events_block(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 2)

    def test_same_time_different_teams_not_duplicate(self):
        # Последний тур группы: два матча в одно время — это НЕ дубль.
        text = ('/add_event\n'
                'Швейцария; Канада; 24.06.2026 22:00; group\n'
                'Босния; Катар; 24.06.2026 22:00; group')
        events, errors = event_utils.parse_events_block(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 2)

    def test_empty_body(self):
        events, errors = event_utils.parse_events_block('/add_event')
        self.assertEqual(events, [])
        self.assertTrue(len(errors) > 0)


if __name__ == '__main__':
    unittest.main()
