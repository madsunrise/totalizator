import os
from datetime import datetime
from typing import Any

from pymongo import MongoClient

import constants
import mapper
from models import Event, Bet


class Database:
    def __init__(self):
        self.client = MongoClient('localhost', 27017)
        self.db = self.client[os.environ[constants.ENV_DATABASE_NAME]]
        self.user_collection = self.db['users']
        self.event_collection = self.db['events']

    def check_if_user_exists(self, user_id: int, raise_error: bool = False) -> bool:
        result = self.get_user(user_id=user_id)
        if result is None and raise_error:
            raise ValueError(f'User with ID={user_id} does not exist')
        return result is not None

    def check_if_event_exists(self, uuid: str, raise_error: bool = False) -> bool:
        result = self.get_event_by_uuid(uuid=uuid)
        if result is None and raise_error:
            raise ValueError(f'Event does not exist')
        return result is not None

    def get_all_users(self):
        result = list(self.user_collection.find())
        result = list(map(lambda x: mapper.parse_user(x), result))
        result.sort(key=lambda x: x.scores, reverse=True)
        return result

    def get_user(self, user_id: int):
        return self.user_collection.find_one({'_id': user_id})

    def register_user_if_required(
            self,
            user_id: int,
            username: str,
            first_name: str,
            last_name: str,
    ) -> bool:
        if self.check_if_user_exists(user_id=user_id, raise_error=False):
            # Trying to insert record with _id that already contains in database will raise Error
            return False

        user_dict = {
            '_id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'last_interaction': datetime.now(),
            'created_at': datetime.now(),
            'scores': 0,
            'bets': [],
        }
        self.user_collection.insert_one(user_dict)
        return True

    def get_user_last_interaction(self, user_id: int) -> datetime:
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        return self.get_user_attribute(user_id, "last_interaction")

    def update_last_interaction(self, user_id: int):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        self.set_user_attribute(user_id=user_id, key='last_interaction', value=datetime.now())

    def get_user_scores(self, user_id: int) -> int:
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        return self.get_user_attribute(user_id=user_id, key='scores')

    def add_scores_to_user(self, user_id: int, amount: int):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        current_value = self.get_user_attribute(user_id=user_id, key='scores')
        new_value = max(current_value + amount, 0)
        self.set_user_attribute(user_id=user_id, key='scores', value=new_value)

    def add_bet(self, user_id: int, bet: Bet):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        bet_dict = mapper.bet_to_dict(bet)
        current_bets = self.get_user_attribute(user_id=user_id, key='bets')
        current_bets.append(bet_dict)
        self.set_user_attribute(user_id=user_id, key='bets', value=current_bets)

    def update_bet(self, user_id: int, bet: Bet):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        existing_bet = self.find_bet(user_id=user_id, event_uuid=bet.event_uuid)
        if existing_bet is None:
            raise ValueError('Unable to update bet as it does not exist')
        new_bet_dict = mapper.bet_to_dict(bet)
        current_bets = self.get_user_attribute(user_id=user_id, key='bets')
        for i, bet in enumerate(current_bets):
            if bet['event_uuid'] == new_bet_dict['event_uuid']:
                current_bets[i] = new_bet_dict
                break
        self.set_user_attribute(user_id=user_id, key='bets', value=current_bets)

    def delete_bet(self, user_id: int, event_uuid: str):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        current_bets = self.get_user_attribute(user_id=user_id, key='bets')
        new_bets = [x for x in current_bets if x['event_uuid'] != event_uuid]
        self.set_user_attribute(user_id=user_id, key='bets', value=new_bets)

    def get_all_user_bets(self, user_id: int) -> list:
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        result = self.get_user_attribute(user_id=user_id, key='bets')
        result = list(map(lambda x: mapper.parse_bet(x), result))
        result.sort(key=lambda x: x.created_at, reverse=False)
        return result

    def find_bet(self, user_id: int, event_uuid: str) -> Bet | None:
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        all_bets = self.get_all_user_bets(user_id=user_id)
        return next((bet for bet in all_bets if bet.event_uuid == event_uuid), None)

    def get_current_event_for_user(self, user_id: int) -> str | None:
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        return self.get_user_attribute(user_id=user_id, key='current_event')

    def save_current_event_to_user(self, user_id: int, event_uuid: str):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        self.set_user_attribute(user_id=user_id, key='current_event', value=event_uuid)

    def clear_current_event_for_user(self, user_id: int):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        self.delete_user_attribute(user_id=user_id, key='current_event')

    def add_event(self, event: Event):
        existing = self.find_event(team_1=event.team_1, team_2=event.team_2, time=event.time)
        if existing is not None:
            raise ValueError('Event already exists')
        event_dict = mapper.event_to_dict(event)
        self.event_collection.insert_one(event_dict)

    def get_all_events(self) -> list:
        result = list(self.event_collection.find())
        result = list(map(lambda x: mapper.parse_event(x), result))
        result.sort(key=lambda x: x.time, reverse=False)
        return result

    def get_event_by_uuid(self, uuid: str) -> Event | None:
        event_dict = self.event_collection.find_one({'uuid': uuid})
        if event_dict:
            return mapper.parse_event(event_dict=dict(event_dict))
        return None

    def find_event(self, team_1: str, team_2: str, time: datetime) -> Event | None:
        event_dict = self.event_collection.find_one({'team_1': team_1, 'team_2': team_2, 'time': time})
        if event_dict:
            return mapper.parse_event(event_dict=dict(event_dict))
        return None

    def find_events_in_time_range(self, from_inclusive: datetime, to_exclusive: datetime) -> list:
        result = list(self.event_collection.find({
            'time': {
                "$gte": from_inclusive,
                "$lt": to_exclusive
            }
        }))
        result = list(map(lambda x: mapper.parse_event(x), result))
        result.sort(key=lambda x: x.time, reverse=False)
        return result

    def update_event(self, event: Event):
        self.check_if_event_exists(uuid=event.uuid, raise_error=True)
        event_dict = mapper.event_to_dict(event)
        self.event_collection.update_one(
            {'uuid': event.uuid},
            {'$set': event_dict}
        )

    def get_user_attribute(self, user_id: int, key: str):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        user_dict = self.user_collection.find_one({'_id': user_id})
        if key not in user_dict:
            return None
        return user_dict[key]

    def set_user_attribute(self, user_id: int, key: str, value: Any):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        self.user_collection.update_one({'_id': user_id}, {'$set': {key: value}})

    def delete_user_attribute(self, user_id: int, key: str):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        self.user_collection.update_one({'_id': user_id}, {'$unset': {key: ''}})
