from datetime import datetime
from typing import Any

from pymongo import MongoClient


class Database:
    def __init__(self):
        self.client = MongoClient('localhost', 27017)
        self.db = self.client['totalizator']
        self.user_collection = self.db['users']
        self.events_collection = self.db['events']

    def check_if_user_exists(self, user_id: int, raise_error: bool = False) -> bool:
        result = self.get_user(user_id=user_id)
        if result is None and raise_error:
            raise ValueError(f'User with ID={user_id} does not exist')
        return result is not None

    def get_all_users(self):
        return self.user_collection.find()

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

    def add_to_user_scores(self, user_id: int, points: int):
        self.check_if_user_exists(user_id=user_id, raise_error=True)
        current_value = self.get_user_attribute(user_id=user_id, key='scores')
        new_value = max(current_value + points, 0)
        self.set_user_attribute(user_id=user_id, key='scores', value=new_value)

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
