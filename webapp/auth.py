from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from .models import User
from config import load_config, save_config
from . import login_manager

auth = Blueprint('auth', __name__)

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@auth.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        config = load_config()
        web_users = config.get('web_users', [])
        
        user_data = next((u for u in web_users if u.get('username') == username), None)

        if not web_users:
            if not password or len(password) < 6:
                flash('密码长度至少为6位。', 'danger')
                return render_template('login.html')
            if password != confirm_password:
                flash('两次输入的密码不一致。', 'danger')
                return render_template('login.html')

            new_user_id = 1
            hashed_password = generate_password_hash(password)
            new_user_data = {'id': new_user_id, 'username': username, 'password_hash': hashed_password}
            web_users.append(new_user_data)
            config['web_users'] = web_users
            save_config(config)
            
            new_user = User(new_user_id, username, hashed_password)
            login_user(new_user)
            flash('管理员账户注册成功并已登录！', 'success')
            return redirect(url_for('views.index'))
        
        elif user_data:
            user = User(user_data['id'], user_data['username'], user_data.get('password_hash'))
            if user.password_hash and check_password_hash(user.password_hash, password):
                login_user(user)
                flash('登录成功！', 'success')
                return redirect(url_for('views.index'))
            else:
                flash('用户名或密码错误。', 'danger')
        else:
            flash('用户名不存在或不允许注册新用户。', 'danger')
            
    return render_template('login.html')

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    flash('您已成功登出。', 'info')
    return redirect(url_for('auth.login'))

@auth.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_new_password = request.form.get('confirm_new_password')

        if not current_user.password_hash or not check_password_hash(current_user.password_hash, current_password):
            flash('当前密码不正确。', 'danger')
        elif new_password != confirm_new_password:
            flash('新密码和确认密码不匹配。', 'danger')
        elif len(new_password) < 6:
            flash('新密码长度至少为6位。', 'danger')
        else:
            config = load_config()
            user_updated = False
            for user_data in config.get('web_users', []):
                if user_data.get('id') == current_user.id:
                    user_data['password_hash'] = generate_password_hash(new_password)
                    user_updated = True
                    break
            
            if user_updated:
                save_config(config)
                flash('密码修改成功！请使用新密码重新登录。', 'success')
                logout_user()
                return redirect(url_for('auth.login'))
            else:
                flash('更新密码时发生错误。', 'danger')

    return render_template('change_password.html')