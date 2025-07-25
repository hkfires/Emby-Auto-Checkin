import os
import logging
from flask import Flask
from flask_login import LoginManager
from config import load_config, save_config, migrate_session_names
from log import init_log_db
from utils.common import format_datetime_filter

migrate_session_names()

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = ""

def create_app():
    app = Flask(__name__, instance_relative_config=True)

    config = load_config()

    if 'secret_key' not in config or not config['secret_key']:
        config['secret_key'] = os.urandom(24).hex()
        save_config(config)
        logging.info("Generated and saved a new secret key.")
    app.secret_key = bytes.fromhex(config['secret_key'])

    with app.app_context():
        init_log_db()

    login_manager.init_app(app)

    app.jinja_env.filters['format_datetime'] = format_datetime_filter

    from . import auth, views, api
    app.register_blueprint(auth.auth)
    app.register_blueprint(views.views)
    app.register_blueprint(api.api, url_prefix='/api')

    return app