from flask_login import UserMixin
from config import load_config

class User(UserMixin):
    def __init__(self, id, username, password_hash=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash

    @staticmethod
    def get(user_id):
        config = load_config()
        try:
            uid_to_check = int(user_id)
        except ValueError:
            return None

        for user_data in config.get('web_users', []):
            if user_data.get('id') == uid_to_check:
                return User(user_data['id'], user_data['username'], user_data.get('password_hash'))
        return None

    @staticmethod
    def get_by_username(username):
        config = load_config()
        for user_data in config.get('web_users', []):
            if user_data.get('username') == username:
                return User(user_data['id'], user_data['username'], user_data.get('password_hash'))
        return None