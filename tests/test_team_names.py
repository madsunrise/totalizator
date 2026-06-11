import unittest

import team_names


class TeamNamesTest(unittest.TestCase):
    def test_has_all_48_teams(self):
        self.assertEqual(len(team_names._TEAMS), 48)

    def test_no_empty_names(self):
        for ru_names, en_keys in team_names._TEAMS:
            for name in ru_names + en_keys:
                self.assertTrue(name.strip())

    def test_ru_aliases_unique_across_teams(self):
        seen = set()
        for ru_names, _ in team_names._TEAMS:
            for ru_name in ru_names:
                normalized = team_names.normalize(ru_name)
                self.assertNotIn(normalized, seen, ru_name)
                seen.add(normalized)

    def test_casefold_lookup(self):
        self.assertEqual(team_names.get_english_name('испания'), 'Spain')
        self.assertEqual(team_names.get_english_name('  ИСПАНИЯ  '), 'Spain')

    def test_apostrophe_normalization(self):
        # Типографский апостроф (’) и обычный (') должны давать один результат.
        self.assertEqual(team_names.get_english_name('Кот-д’Ивуар'), 'Ivory Coast')
        self.assertEqual(team_names.get_english_name("Кот-д'Ивуар"), 'Ivory Coast')

    def test_aliases(self):
        self.assertEqual(team_names.get_english_name('Корея'), 'South Korea')
        self.assertEqual(team_names.get_english_name('Южная Африка'), 'South Africa')
        self.assertEqual(team_names.get_english_name('Голландия'), 'Netherlands')

    def test_unknown_team_returns_none(self):
        self.assertIsNone(team_names.get_english_name('Нарния'))
        self.assertIsNone(team_names.get_api_keys('Нарния'))

    def test_api_keys_contain_tla(self):
        keys = team_names.get_api_keys('Германия')
        self.assertIn('ger', keys)
        self.assertIn('germany', keys)


if __name__ == '__main__':
    unittest.main()
