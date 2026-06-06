import unittest
from datetime import datetime, timedelta, timezone

import tournament_utils
from models import Event, EventType, Group, Tournament


def make_tournament(group_specs: dict) -> Tournament:
    # group_specs: {'A': ['Канада', 'Мексика'], ...}
    groups = [Group(id=gid, name=gid, teams=list(teams)) for gid, teams in group_specs.items()]
    return Tournament(name='Test', created_at=datetime(2026, 6, 1), groups=groups)


class NormalizeTest(unittest.TestCase):
    def test_handles_none_and_whitespace(self):
        self.assertEqual(tournament_utils.normalize_team_name(None), '')
        self.assertEqual(tournament_utils.normalize_team_name('  США '), 'сша')

    def test_equals_team_empty_is_false(self):
        self.assertFalse(tournament_utils.equals_team('', 'США'))
        self.assertFalse(tournament_utils.equals_team('США', None))
        self.assertTrue(tournament_utils.equals_team(' испания', 'ИСПАНИЯ '))


class GetTournamentStartTest(unittest.TestCase):
    def make_event(self, offset_hours: int) -> Event:
        base = datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        return Event(uuid=str(offset_hours), team_1='a', team_2='b',
                     time=base + timedelta(hours=offset_hours), event_type=EventType.GROUP_STAGE)

    def test_none_when_no_events(self):
        self.assertIsNone(tournament_utils.get_tournament_start([]))

    def test_returns_earliest(self):
        events = [self.make_event(10), self.make_event(0), self.make_event(5)]
        start = tournament_utils.get_tournament_start(events)
        self.assertEqual(start, self.make_event(0).get_time_in_utc())


class ChampionScoringTest(unittest.TestCase):
    def test_correct_awards_ten(self):
        self.assertEqual(tournament_utils.calculate_champion_bet_points('Испания', 'Испания'), 10)

    def test_incorrect_awards_zero(self):
        self.assertEqual(tournament_utils.calculate_champion_bet_points('Франция', 'Испания'), 0)

    def test_case_insensitive_and_trimmed(self):
        self.assertEqual(tournament_utils.calculate_champion_bet_points('  испания ', 'ИСПАНИЯ'), 10)

    def test_none_or_empty_pick_awards_zero(self):
        self.assertEqual(tournament_utils.calculate_champion_bet_points(None, 'Испания'), 0)
        self.assertEqual(tournament_utils.calculate_champion_bet_points('', 'Испания'), 0)

    def test_unknown_actual_awards_zero(self):
        self.assertEqual(tournament_utils.calculate_champion_bet_points('Испания', None), 0)


class GroupScoringTest(unittest.TestCase):
    def test_all_twelve_correct_gives_twentytwo(self):
        groups = {f'g{i}': f'team{i}' for i in range(12)}
        result = tournament_utils.calculate_group_bet_points(picks=groups, actual_winners=groups, total_groups=12)
        self.assertEqual(result.correct_count, 12)
        self.assertEqual(result.base_points, 12)
        self.assertEqual(result.bonus_points, 10)
        self.assertEqual(result.total_points, 22)
        self.assertTrue(result.all_correct)

    def test_eleven_of_twelve_gives_eleven_no_bonus(self):
        actual = {f'g{i}': f'team{i}' for i in range(12)}
        picks = dict(actual)
        picks['g11'] = 'wrong'
        result = tournament_utils.calculate_group_bet_points(picks=picks, actual_winners=actual, total_groups=12)
        self.assertEqual(result.correct_count, 11)
        self.assertEqual(result.total_points, 11)
        self.assertFalse(result.all_correct)

    def test_five_filled_all_correct_no_bonus_because_tournament_has_twelve(self):
        actual = {f'g{i}': f'team{i}' for i in range(12)}
        picks = {f'g{i}': f'team{i}' for i in range(5)}
        result = tournament_utils.calculate_group_bet_points(picks=picks, actual_winners=actual, total_groups=12)
        self.assertEqual(result.correct_count, 5)
        self.assertEqual(result.total_points, 5)
        self.assertFalse(result.all_correct)

    def test_bonus_requires_all_tournament_groups_not_just_entered(self):
        actual = {'A': 'X', 'B': 'Y'}
        result = tournament_utils.calculate_group_bet_points(picks=actual, actual_winners=actual, total_groups=3)
        self.assertEqual(result.total_points, 2)
        self.assertFalse(result.all_correct)

    def test_case_insensitive_matching(self):
        actual = {'A': 'Канада', 'B': 'Мексика'}
        picks = {'A': ' канада', 'B': 'МЕКСИКА '}
        result = tournament_utils.calculate_group_bet_points(picks=picks, actual_winners=actual, total_groups=2)
        self.assertEqual(result.correct_count, 2)
        self.assertTrue(result.all_correct)
        self.assertEqual(result.total_points, 12)

    def test_missing_pick_for_a_group_counts_as_wrong(self):
        actual = {'A': 'Канада', 'B': 'Мексика'}
        picks = {'A': 'Канада'}
        result = tournament_utils.calculate_group_bet_points(picks=picks, actual_winners=actual, total_groups=2)
        self.assertEqual(result.correct_count, 1)
        self.assertEqual(result.total_points, 1)

    def test_missing_actual_winner_is_not_correct(self):
        actual = {'A': 'Канада', 'B': None}
        picks = {'A': 'Канада', 'B': 'Мексика'}
        result = tournament_utils.calculate_group_bet_points(picks=picks, actual_winners=actual, total_groups=2)
        self.assertEqual(result.correct_count, 1)
        self.assertFalse(result.all_correct)

    def test_empty_tournament_no_bonus(self):
        result = tournament_utils.calculate_group_bet_points(picks={}, actual_winners={}, total_groups=0)
        self.assertEqual(result.total_points, 0)
        self.assertFalse(result.all_correct)

    def test_default_total_groups_uses_actual_winner_count(self):
        actual = {'A': 'X', 'B': 'Y'}
        result = tournament_utils.calculate_group_bet_points(picks=actual, actual_winners=actual)
        self.assertEqual(result.total_groups, 2)
        self.assertTrue(result.all_correct)


class ParseStructureTest(unittest.TestCase):
    def test_valid_structure(self):
        text = '/setup_tournament\nA: Канада, Мексика, США\nB: Швейцария, Норвегия'
        groups, errors = tournament_utils.parse_structure(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0].id, 'A')
        self.assertEqual(groups[0].teams, ['Канада', 'Мексика', 'США'])
        self.assertEqual(groups[1].teams, ['Швейцария', 'Норвегия'])

    def test_force_token_first_line_ignored(self):
        text = '/setup_tournament FORCE\nA: Канада, Мексика'
        groups, errors = tournament_utils.parse_structure(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(groups), 1)

    def test_line_without_colon(self):
        text = '/setup_tournament\nA Канада Мексика'
        groups, errors = tournament_utils.parse_structure(text)
        self.assertTrue(any('без двоеточия' in e for e in errors))

    def test_duplicate_group(self):
        text = '/setup_tournament\nA: Канада, Мексика\nA: США, Панама'
        groups, errors = tournament_utils.parse_structure(text)
        self.assertTrue(any('дважды' in e for e in errors))

    def test_too_few_teams(self):
        text = '/setup_tournament\nA: Канада'
        groups, errors = tournament_utils.parse_structure(text)
        self.assertTrue(any('меньше' in e for e in errors))

    def test_duplicate_team_across_tournament(self):
        text = '/setup_tournament\nA: Канада, Мексика\nB: Канада, США'
        groups, errors = tournament_utils.parse_structure(text)
        self.assertTrue(any('несколько раз' in e for e in errors))

    def test_empty_body(self):
        groups, errors = tournament_utils.parse_structure('/setup_tournament')
        self.assertTrue(len(errors) > 0)
        self.assertEqual(groups, [])


class ParseGroupWinnersTest(unittest.TestCase):
    def setUp(self):
        self.tournament = make_tournament({'A': ['Канада', 'Мексика'], 'B': ['Швейцария', 'Норвегия']})

    def test_valid_winners(self):
        text = '/set_group_winners\nA: Канада\nB: норвегия'
        winners, errors = tournament_utils.parse_group_winners(text, self.tournament)
        self.assertEqual(errors, [])
        self.assertEqual(winners, {'A': 'Канада', 'B': 'Норвегия'})  # canonical spelling

    def test_unknown_group(self):
        text = '/set_group_winners\nA: Канада\nZ: Кто-то'
        winners, errors = tournament_utils.parse_group_winners(text, self.tournament)
        self.assertTrue(any('Нет группы' in e for e in errors))

    def test_team_not_in_group(self):
        text = '/set_group_winners\nA: Норвегия\nB: Швейцария'
        winners, errors = tournament_utils.parse_group_winners(text, self.tournament)
        self.assertTrue(any('не из группы' in e for e in errors))

    def test_missing_group(self):
        text = '/set_group_winners\nA: Канада'
        winners, errors = tournament_utils.parse_group_winners(text, self.tournament)
        self.assertTrue(any('Не указан победитель' in e for e in errors))


class FakeReminderGuard:
    # Имитирует database.claim_reminder: True только в первый раз на ключ.
    def __init__(self):
        self.claimed = set()

    def claim(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class SettlementIdempotencyTest(unittest.TestCase):
    def _settle_once(self, guard, totals, user_id, points):
        if points <= 0:
            return
        if not guard.claim(f'settle:champion:{user_id}'):
            return
        totals[user_id] = totals.get(user_id, 0) + points

    def test_resettle_awards_nothing_extra(self):
        guard = FakeReminderGuard()
        totals = {}
        self._settle_once(guard, totals, user_id=1, points=10)
        self._settle_once(guard, totals, user_id=1, points=10)
        self.assertEqual(totals[1], 10)

    def test_zero_point_user_is_never_claimed(self):
        guard = FakeReminderGuard()
        totals = {}
        self._settle_once(guard, totals, user_id=2, points=0)
        self.assertNotIn(2, totals)
        self.assertTrue(guard.claim('settle:champion:2'))

    def test_distinct_users_each_awarded_once(self):
        guard = FakeReminderGuard()
        totals = {}
        for uid in (1, 2, 3):
            self._settle_once(guard, totals, user_id=uid, points=10)
            self._settle_once(guard, totals, user_id=uid, points=10)
        self.assertEqual(totals, {1: 10, 2: 10, 3: 10})


if __name__ == '__main__':
    unittest.main()
