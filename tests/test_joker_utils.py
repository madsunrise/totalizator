import unittest
from datetime import datetime, timedelta, timezone

import joker_utils
from models import Bet, Event, EventType


class JokerUtilsTest(unittest.TestCase):
    def make_event(self, uuid: str, offset_hours: int, event_type: EventType) -> Event:
        return Event(
            uuid=uuid,
            team_1=f'{uuid}-1',
            team_2=f'{uuid}-2',
            time=self.base_time + timedelta(hours=offset_hours),
            event_type=event_type,
        )

    def make_bet(self, event: Event, is_joker: bool) -> Bet:
        return Bet(
            user_id=1,
            event_uuid=event.uuid,
            team_1_scores=1,
            team_2_scores=0,
            team_1_will_go_through=None,
            created_at=self.base_time,
            is_joker=is_joker,
        )

    def setUp(self):
        self.base_time = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    def test_status_before_playoff_reports_burn_count(self):
        group_1 = self.make_event('group-1', 24, EventType.GROUP_STAGE)
        group_2 = self.make_event('group-2', 48, EventType.GROUP_STAGE)
        playoff = self.make_event('playoff-1', 120, EventType.PLAY_OFF_SINGLE_MATCH)

        status = joker_utils.calculate_joker_status(
            bets_with_events=[
                (self.make_bet(group_1, True), group_1),
                (self.make_bet(group_2, True), group_2),
            ],
            events=[group_1, group_2, playoff],
            now_utc=self.base_time,
        )

        self.assertEqual(status.used_total, 2)
        self.assertEqual(status.used_playoff, 0)
        self.assertEqual(status.will_burn_at_playoff_start, 2)
        self.assertEqual(status.remaining_usable_now, 6)

        text = joker_utils.get_joker_status_text(status)
        self.assertIn('Использовано всего: 2/8', text)
        self.assertIn('К старту плей-офф сгорит: 2', text)

    def test_status_after_playoff_reports_burned_and_current_limit(self):
        group_1 = self.make_event('group-1', -24, EventType.GROUP_STAGE)
        group_2 = self.make_event('group-2', -12, EventType.GROUP_STAGE)
        playoff = self.make_event('playoff-1', -1, EventType.PLAY_OFF_SINGLE_MATCH)

        status = joker_utils.calculate_joker_status(
            bets_with_events=[
                (self.make_bet(group_1, True), group_1),
                (self.make_bet(group_2, True), group_2),
            ],
            events=[group_1, group_2, playoff],
            now_utc=self.base_time,
        )

        self.assertTrue(status.playoff_started)
        self.assertEqual(status.will_burn_at_playoff_start, 2)
        self.assertEqual(status.remaining_usable_now, 4)

        text = joker_utils.get_joker_status_text(status)
        self.assertIn('Доступно сейчас: 4', text)
        self.assertIn('К старту плей-офф сгорело: 2', text)

    def test_fifth_playoff_joker_is_not_allowed_even_before_playoff_start(self):
        group = self.make_event('group-1', 24, EventType.GROUP_STAGE)
        playoff_events = [
            self.make_event(f'playoff-{index}', 120 + index, EventType.PLAY_OFF_SINGLE_MATCH)
            for index in range(5)
        ]

        bets_with_events = [
            (self.make_bet(playoff_events[index], True), playoff_events[index])
            for index in range(4)
        ]
        candidate_bet = self.make_bet(playoff_events[4], False)

        can_assign = joker_utils.can_assign_joker_to_bet(
            bet=candidate_bet,
            event=playoff_events[4],
            bets_with_events=bets_with_events,
            events=[group, *playoff_events],
            now_utc=self.base_time,
        )

        self.assertFalse(can_assign)

    def test_total_limit_of_eight_is_enforced(self):
        events = [
            self.make_event(f'group-{index}', 24 + index, EventType.GROUP_STAGE)
            for index in range(9)
        ]
        bets_with_events = [(self.make_bet(event, True), event) for event in events[:8]]
        candidate_bet = self.make_bet(events[8], False)

        can_assign = joker_utils.can_assign_joker_to_bet(
            bet=candidate_bet,
            event=events[8],
            bets_with_events=bets_with_events,
            events=events,
            now_utc=self.base_time,
        )

        self.assertFalse(can_assign)

    def test_cannot_remove_joker_from_started_match(self):
        started_event = self.make_event('group-1', -1, EventType.GROUP_STAGE)
        started_bet = self.make_bet(started_event, True)

        self.assertFalse(
            joker_utils.can_remove_joker_from_bet(
                bet=started_bet,
                event=started_event,
                now_utc=self.base_time,
            )
        )

    def test_playoff_counter_is_capped_by_total_remaining(self):
        group_events = [
            self.make_event(f'group-{index}', 24 + index, EventType.GROUP_STAGE)
            for index in range(6)
        ]
        playoff = self.make_event('playoff-1', 120, EventType.PLAY_OFF_SINGLE_MATCH)
        bets_with_events = [(self.make_bet(event, True), event) for event in group_events]

        status = joker_utils.calculate_joker_status(
            bets_with_events=bets_with_events,
            events=[*group_events, playoff],
            now_utc=self.base_time,
        )

        self.assertEqual(status.remaining_total, 2)
        self.assertEqual(status.remaining_playoff, 4)

        text = joker_utils.get_joker_status_text(status)
        self.assertIn('На матчи плей-офф ещё можно поставить: 2', text)

    def test_play_off_first_match_counts_toward_playoff_quota(self):
        first_leg_events = [
            self.make_event(f'first-leg-{index}', 48 + index, EventType.PLAY_OFF_FIRST_MATCH)
            for index in range(4)
        ]
        final = self.make_event('final', 200, EventType.PLAY_OFF_SINGLE_MATCH)
        bets_with_events = [(self.make_bet(event, True), event) for event in first_leg_events]
        candidate_bet = self.make_bet(final, False)

        status = joker_utils.calculate_joker_status(
            bets_with_events=bets_with_events,
            events=[*first_leg_events, final],
            now_utc=self.base_time,
        )
        self.assertEqual(status.used_playoff, 4)
        self.assertEqual(status.remaining_playoff, 0)
        self.assertEqual(status.playoff_start, first_leg_events[0].get_time_in_utc())

        can_assign = joker_utils.can_assign_joker_to_bet(
            bet=candidate_bet,
            event=final,
            bets_with_events=bets_with_events,
            events=[*first_leg_events, final],
            now_utc=self.base_time,
        )
        self.assertFalse(can_assign)

    def test_joker_doubles_only_base_scores(self):
        self.assertEqual(joker_utils.calculate_scores_with_joker(base_scores=4, is_joker=True), 8)
        self.assertEqual(joker_utils.calculate_scores_with_joker(base_scores=3, is_joker=False), 3)


if __name__ == '__main__':
    unittest.main()
