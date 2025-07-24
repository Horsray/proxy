# User management server for Huiying Proxy

import os
import json
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, send_file
from werkzeug.utils import secure_filename

from openpyxl import Workbook, load_workbook

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'users.json')
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'huiying-secret')


def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f).get('users', {})
            except Exception:
                return {}
    return {}


def save_users(users: dict) -> None:
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'users': users}, f, indent=4, ensure_ascii=False)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('admin') != 'horsray':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        users = load_users()
        user = users.get(username)
        if user and user.get('password') == password and user.get('is_admin'):
            session['admin'] = username
            user['last_login'] = datetime.now().isoformat()
            users[username] = user
            save_users(users)
            return redirect(url_for('user_list'))
        return render_template('login.html', error='登录失败')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))


@app.route('/')
@admin_required
def index():
    return redirect(url_for('user_list'))


@app.route('/users')
@admin_required
def user_list():
    users = load_users()
    return render_template('users.html', users=users)


@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')
    is_admin = bool(request.form.get('is_admin'))
    if not username or not password:
        return redirect(url_for('user_list'))
    users = load_users()
    if username in users:
        return redirect(url_for('user_list'))
    users[username] = {
        'password': password,
        'is_admin': is_admin,
        'created_at': datetime.now().isoformat(),
        'last_login': None
    }
    save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/<name>/delete', methods=['POST'])
@admin_required
def delete_user(name):
    users = load_users()
    if name in users:
        users.pop(name)
        save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/<name>/update', methods=['POST'])
@admin_required
def update_user(name):
    new_name = request.form.get('username')
    password = request.form.get('password')
    is_admin = bool(request.form.get('is_admin'))
    users = load_users()
    if name in users:
        user = users[name]
        if new_name and new_name != name:
            users[new_name] = user
            users.pop(name)
            name = new_name
        if password:
            users[name]['password'] = password
        users[name]['is_admin'] = is_admin
        save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/import', methods=['POST'])
@admin_required
def import_users():
    file = request.files.get('file')
    if not file:
        return redirect(url_for('user_list'))
    filename = secure_filename(file.filename)
    if not filename:
        return redirect(url_for('user_list'))
    wb = load_workbook(file)
    ws = wb.active
    users = load_users()
    first = True
    for row in ws.iter_rows(values_only=True):
        if first:
            first = False
            continue
        username = str(row[0]) if row and row[0] else None
        password = str(row[1]) if row and len(row) > 1 else None
        is_admin = bool(row[2]) if row and len(row) > 2 else False
        if username and password:
            users[username] = {
                'password': password,
                'is_admin': is_admin,
                'created_at': datetime.now().isoformat(),
                'last_login': None
            }
    save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/export')
@admin_required
def export_users():
    users = load_users()
    wb = Workbook()
    ws = wb.active
    ws.append(['username', 'password', 'is_admin', 'created_at', 'last_login'])
    for name, info in users.items():
        ws.append([
            name,
            info.get('password'),
            info.get('is_admin'),
            info.get('created_at'),
            info.get('last_login'),
        ])
    path = 'users_export.xlsx'
    wb.save(path)
    return send_file(path, as_attachment=True)


@app.route('/users/bulk_create', methods=['POST'])
@admin_required
def bulk_create():
    count = int(request.form.get('count', 0))
    users = load_users()
    for _ in range(count):
        while True:
            uname = f"huiying{os.urandom(4).hex()}"[:12]
            if uname not in users:
                break
        pwd = os.urandom(4).hex()
        users[uname] = {
            'password': pwd,
            'is_admin': False,
            'created_at': datetime.now().isoformat(),
            'last_login': None
        }
    save_users(users)
    return redirect(url_for('user_list'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
