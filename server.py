# Enhanced User management server for Huiying Proxy

import os
import json
import inspect
import argparse
import requests
from datetime import datetime
from io import BytesIO
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, send_file, jsonify
from werkzeug.utils import secure_filename

from openpyxl import Workbook, load_workbook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
LEDGER_FILE = os.path.join(BASE_DIR, 'ledger.json')
PRODUCTS_FILE = os.path.join(BASE_DIR, 'products.json')
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'huiying-secret')


def get_location_from_ip(ip_address):
    """根据IP地址获取地理位置信息"""
    try:
        # 使用免费的IP地理位置API
        response = requests.get(f'http://ip-api.com/json/{ip_address}?lang=zh-CN', timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data['status'] == 'success':
                city = data.get('city', '未知')
                return f"{city}-{ip_address}"
        return f"未知-{ip_address}"
    except:
        return f"未知-{ip_address}"


def get_client_ip():
    """获取客户端真实IP地址"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        return request.headers.get('X-Real-IP')
    else:
        return request.remote_addr


def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            try:
                users = json.load(f).get('users', {})
                # ensure default fields
                for name, info in users.items():
                    info.setdefault('nickname', '')
                    info.setdefault('enabled', True)
                    info.setdefault('source', 'add')
                    info.setdefault('price', 0)
                    info.setdefault('ip_address', '')
                    info.setdefault('location', '')
                    info.setdefault('remark', '')
                    info.setdefault('role', 'admin' if info.get('is_admin') else '')
                    info.setdefault('agent', '')
                    info.setdefault('for_sale', False)
                return users
            except Exception:
                return {}
    return {}


def save_users(users: dict) -> None:
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'users': users}, f, indent=4, ensure_ascii=False)


def load_ledger() -> list:
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f).get('records', [])
            except Exception:
                return []
    return []


def save_ledger(records: list) -> None:
    with open(LEDGER_FILE, 'w', encoding='utf-8') as f:
        json.dump({'records': records}, f, indent=4, ensure_ascii=False)


def load_products() -> dict:
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
            try:
                products = json.load(f).get('products', {})
                for p in products.values():
                    p.setdefault('price', 0)
                    p.setdefault('default', False)
                return products
            except Exception:
                return {}
    return {}


def save_products(products: dict) -> None:
    with open(PRODUCTS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'products': products}, f, indent=4, ensure_ascii=False)


APPLICATIONS_FILE = os.path.join(BASE_DIR, 'applications.json')


def load_applications() -> list:
    if os.path.exists(APPLICATIONS_FILE):
        with open(APPLICATIONS_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f).get('applications', [])
            except Exception:
                return []
    return []


def save_applications(apps: list) -> None:
    with open(APPLICATIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'applications': apps}, f, indent=4, ensure_ascii=False)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'admin':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def agent_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'agent':
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
        if user and user.get('password') == password:
            client_ip = get_client_ip()
            user['last_login'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            user['ip_address'] = client_ip
            user['location'] = get_location_from_ip(client_ip)
            users[username] = user
            save_users(users)
            session['username'] = username
            if user.get('is_admin') or user.get('role') == 'admin':
                session['role'] = 'admin'
                return redirect(url_for('user_list'))
            elif user.get('role') == 'agent':
                session['role'] = 'agent'
                return redirect(url_for('agent_user_list'))
        return render_template('login.html', error='登录失败')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@admin_required
def index():
    if session.get('role') == 'agent':
        return redirect(url_for('agent_user_list'))
    return redirect(url_for('user_list'))


@app.route('/bulk')
@admin_required
def bulk_manage():
    accounts = session.get('bulk_accounts')
    info = session.get('bulk_info')
    products = load_products()
    page = int(request.args.get('page', 1))
    per_page = 20
    if accounts:
        start = (page - 1) * per_page
        page_accounts = accounts[start:start + per_page]
    else:
        page_accounts = None
    return render_template(
        'bulk.html', accounts=page_accounts, products=products,
        info=info, page=page, per_page=per_page,
        total=len(accounts) if accounts else 0, role=session.get('role')
    )


@app.route('/agent/apply', methods=['GET', 'POST'])
@agent_required
def agent_apply():
    if request.method == 'POST':
        count = int(request.form.get('count', 0))
        price = float(request.form.get('price') or 0)
        product = request.form.get('product', '')
        apps = load_applications()
        apps.append({
            'id': int(datetime.now().timestamp() * 1000),
            'agent': session.get('username'),
            'count': count,
            'price': price,
            'product': product,
            'status': 'pending',
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        save_applications(apps)
        return redirect(url_for('agent_apply'))
    apps = [a for a in load_applications() if a.get('agent') == session.get('username')]
    products = load_products()
    return render_template('agent_apply.html', apps=apps, products=products, role=session.get('role'))


@app.route('/users/random')
@admin_required
def random_account():
    users = load_users()
    while True:
        uname = f"huiying{os.urandom(4).hex()}"[:12]
        if uname not in users:
            break
    pwd = os.urandom(4).hex()
    return jsonify({'username': uname, 'password': pwd})


@app.route('/bulk/export')
@admin_required
def bulk_export():
    """Download the accounts created in the last bulk operation."""
    accounts = session.get('bulk_accounts')
    if not accounts:
        return redirect(url_for('bulk_manage'))
    wb = Workbook()
    ws = wb.active
    ws.append(['用户名', '密码'])
    for acc in accounts:
        ws.append([acc['username'], acc['password']])
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    # Build response first for compatibility across Flask versions
    filename = 'bulk_accounts.xlsx'
    kwargs = {}
    if 'download_name' in inspect.signature(send_file).parameters:
        kwargs['download_name'] = filename
    else:
        kwargs['attachment_filename'] = filename
    response = send_file(
        bio,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        **kwargs
    )
    return response
    # Clear session so the list is removed on next visit


@app.route('/users')
@admin_required
def user_list():
    query = request.args.get('q', '')
    source = request.args.get('source', '')
    status = request.args.get('status', '')
    sort = request.args.get('sort', 'desc')
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    page = int(request.args.get('page', 1))
    per_page = 10
    users = load_users()
    if query:
        users = {k: v for k, v in users.items() if query.lower() in k.lower()}
    if source:
        users = {k: v for k, v in users.items() if v.get('source') == source}
    if status:
        flag = status == 'enabled'
        users = {k: v for k, v in users.items() if v.get('enabled', True) == flag}
    if start:
        users = {k: v for k, v in users.items() if v.get('created_at', '') >= start}
    if end:
        users = {k: v for k, v in users.items() if v.get('created_at', '') <= end}
    items = list(users.items())
    admins = [i for i in items if i[1].get('is_admin')]
    others = [i for i in items if not i[1].get('is_admin')]
    others.sort(key=lambda x: x[1].get('created_at', ''), reverse=(sort != 'asc'))
    items = admins + others
    total = len(items)
    page_items = items[(page - 1) * per_page: page * per_page]
    products = load_products()
    return render_template(
        'users.html', users=dict(page_items), total=total,
        page=page, per_page=per_page, query=query, source=source,
        status=status, sort=sort, start=start, end=end, products=products, role=session.get('role')
    )


@app.route('/agent/users')
@agent_required
def agent_user_list():
    query = request.args.get('q', '')
    status = request.args.get('status', '')
    sort = request.args.get('sort', 'desc')
    page = int(request.args.get('page', 1))
    per_page = 10
    users = load_users()
    users = {k: v for k, v in users.items() if v.get('agent') == session.get('username')}
    if query:
        users = {k: v for k, v in users.items() if query.lower() in k.lower()}
    if status:
        flag = status == 'enabled'
        users = {k: v for k, v in users.items() if v.get('enabled', True) == flag}
    items = list(users.items())
    items.sort(key=lambda x: x[1].get('created_at', ''), reverse=(sort != 'asc'))
    total = len(items)
    page_items = items[(page - 1) * per_page: page * per_page]
    products = load_products()
    return render_template(
        'users.html', users=dict(page_items), total=total,
        page=page, per_page=per_page, query=query, source='',
        status=status, sort=sort, start='', end='', products=products, role=session.get('role')
    )


@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')
    nickname = request.form.get('nickname', '')
    is_admin = bool(request.form.get('is_admin'))
    price = float(request.form.get('price') or 0)
    product = request.form.get('product', '')
    if not username or not password:
        return redirect(url_for('user_list'))
    users = load_users()
    if username in users:
        return redirect(url_for('user_list'))
    users[username] = {
        'password': password,
        'nickname': nickname,
        'is_admin': is_admin,
        'role': 'admin' if is_admin else '',
        'enabled': True,
        'source': 'add',
        'price': price,
        'product': product,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_login': None,
        'ip_address': '',
        'location': '',
        'remark': '',
        'agent': '',
        'for_sale': False
    }
    save_users(users)
    # ledger
    records = load_ledger()
    records.append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'admin': session.get('username'),
        'product': product,
        'price': price,
        'count': 1,
        'revenue': price
    })
    save_ledger(records)
    return redirect(url_for('user_list'))


@app.route('/users/<name>/delete', methods=['POST'])
@admin_required
def delete_user(name):
    users = load_users()
    if name in users:
        users.pop(name)
        save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/<name>/toggle', methods=['POST'])
@admin_required
def toggle_user(name):
    users = load_users()
    if name in users:
        users[name]['enabled'] = not users[name].get('enabled', True)
        save_users(users)
        # 支持AJAX请求
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({'success': True, 'enabled': users[name]['enabled']})
    return redirect(url_for('user_list'))


@app.route('/users/<name>/sold', methods=['POST'])
@agent_required
def mark_sold(name):
    users = load_users()
    agent = session.get('username')
    if name in users and users[name].get('agent') == agent and users[name].get('for_sale'):
        users[name]['for_sale'] = False
        save_users(users)
        if request.is_json:
            return jsonify({'success': True})
    return redirect(url_for('agent_user_list'))


@app.route('/users/batch_action', methods=['POST'])
@admin_required
def batch_action():
    action = request.form.get('action')
    names = request.form.getlist('names')
    users = load_users()
    for name in names:
        if name not in users:
            continue
        if action == 'delete':
            users.pop(name)
        elif action == 'enable':
            users[name]['enabled'] = True
        elif action == 'disable':
            users[name]['enabled'] = False
    save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/<n>/update', methods=['POST'])
@admin_required
def update_user(name):
    new_name = request.form.get('username')
    password = request.form.get('password')
    nickname = request.form.get('nickname')
    is_admin = bool(request.form.get('is_admin'))
    enabled = bool(request.form.get('enabled'))
    product = request.form.get('product')
    remark = request.form.get('remark', '')
    users = load_users()
    if name in users:
        user = users[name]
        if new_name and new_name != name:
            users[new_name] = user
            users.pop(name)
            name = new_name
        if password:
            users[name]['password'] = password
        if nickname is not None:
            users[name]['nickname'] = nickname
        users[name]['is_admin'] = is_admin
        users[name]['enabled'] = enabled
        if product is not None:
            users[name]['product'] = product
        users[name]['remark'] = remark
        save_users(users)
    return redirect(url_for('user_list'))


@app.route('/users/<name>/remark', methods=['POST'])
@admin_required
def update_remark(name):
    """Update user's remark via AJAX"""
    remark = request.form.get('remark', '')
    users = load_users()
    if name in users:
        users[name]['remark'] = remark
        save_users(users)
        if request.is_json or request.headers.get('Content-Type') == 'application/json':
            return jsonify({'success': True})
    return redirect(url_for('user_list'))


@app.route('/users/import', methods=['POST'])
@admin_required
def import_users():
    file = request.files.get('file')
    price = float(request.form.get('price') or 0)
    product = request.form.get('product', '')
    if not file:
        return redirect(url_for('user_list'))
    filename = secure_filename(file.filename)
    if not filename:
        return redirect(url_for('user_list'))
    wb = load_workbook(file)
    ws = wb.active
    users = load_users()
    first = True
    count = 0
    for row in ws.iter_rows(values_only=True):
        if first:
            first = False
            continue
        username = str(row[0]) if row and row[0] else None
        password = str(row[1]) if row and len(row) > 1 else None
        nickname = str(row[2]) if row and len(row) > 2 else ''
        is_admin = bool(row[3]) if row and len(row) > 3 else False
        if username and password:
            users[username] = {
                'password': password,
                'nickname': nickname,
                'is_admin': is_admin,
                'enabled': True,
                'source': 'import',
                'product': product,
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'last_login': None,
                'price': price,
                'ip_address': '',
                'location': ''
            }
            count += 1
    save_users(users)
    if count > 0 and price > 0:
        records = load_ledger()
        records.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'admin': session.get('username'),
            'product': product,
            'price': price,
            'count': count,
            'revenue': price * count
        })
        save_ledger(records)
    return redirect(url_for('user_list'))


@app.route('/users/export')
@admin_required
def export_users():
    users = load_users()
    wb = Workbook()
    ws = wb.active
    ws.append([
        '用户名', '密码', '昵称', '是否管理员',
        '启用', '来源', '创建时间', '最后登录', '产品', 'IP地址', '位置'
    ])
    for name, info in users.items():
        ws.append([
            name,
            info.get('password'),
            info.get('nickname'),
            info.get('is_admin'),
            info.get('enabled'),
            info.get('source'),
            info.get('created_at'),
            info.get('last_login'),
            info.get('product',''),
            info.get('ip_address', ''),
            info.get('location', '')
        ])
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, download_name='users_export.xlsx', as_attachment=True)


@app.route('/users/template')
@admin_required
def download_template():
    wb = Workbook()
    ws = wb.active
    ws.append(['用户名', '密码', '昵称', '是否管理员'])
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    return send_file(bio, download_name='import_template.xlsx', as_attachment=True)


@app.route('/products')
@admin_required
def products():
    products = load_products()
    return render_template('products.html', products=products)


@app.route('/products/add', methods=['POST'])
@admin_required
def add_product():
    name = request.form.get('name')
    version = request.form.get('version', '')
    ptype = request.form.get('ptype', '')
    price = float(request.form.get('price') or 0)
    default = bool(request.form.get('default'))
    if not name:
        return redirect(url_for('products'))
    products = load_products()
    if default:
        for p in products.values():
            p['default'] = False
    products[name] = {
        'name': name,
        'version': version,
        'type': ptype,
        'price': price,
        'default': default
    }
    save_products(products)
    return redirect(url_for('products'))


@app.route('/products/<name>/delete', methods=['POST'])
@admin_required
def delete_product(name):
    products = load_products()
    if name in products:
        products.pop(name)
        save_products(products)
    return redirect(url_for('products'))


@app.route('/products/<name>/default', methods=['POST'])
@admin_required
def set_default_product(name):
    products = load_products()
    if name in products:
        for p in products.values():
            p['default'] = False
        products[name]['default'] = True
        save_products(products)
    return redirect(url_for('products'))


def generate_accounts(count, price, product, agent=None):
    users = load_users()
    new_accounts = []
    for _ in range(count):
        while True:
            uname = f"huiying{os.urandom(4).hex()}"[:12]
            if uname not in users:
                break
        pwd = os.urandom(4).hex()
        users[uname] = {
            'password': pwd,
            'nickname': '',
            'is_admin': False,
            'enabled': True,
            'source': 'batch',
            'product': product,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'last_login': None,
            'price': price,
            'ip_address': '',
            'location': '',
            'agent': agent or '',
            'for_sale': bool(agent)
        }
        new_accounts.append({'username': uname, 'password': pwd})
    save_users(users)
    if count > 0:
        records = load_ledger()
        records.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'admin': agent or session.get('username'),
            'product': product,
            'price': price,
            'count': count,
            'revenue': price * count
        })
        save_ledger(records)
    return new_accounts


@app.route('/users/bulk_create', methods=['POST'])
@admin_required
def bulk_create():
    count = int(request.form.get('count', 0))
    price = float(request.form.get('price') or 0)
    product = request.form.get('product', '')
    new_accounts = generate_accounts(count, price, product)
    session['bulk_accounts'] = new_accounts
    session['bulk_info'] = {
        'product': product,
        'price': price,
        'admin': session.get('username'),
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    return redirect(url_for('bulk_manage'))


@app.route('/approvals', methods=['GET', 'POST'])
@admin_required
def approvals():
    apps = load_applications()
    if request.method == 'POST':
        app_id = request.form.get('id')
        action = request.form.get('action')
        for app_data in apps:
            if str(app_data.get('id')) == str(app_id) and app_data.get('status') == 'pending':
                app_data['count'] = int(request.form.get('count', app_data['count']))
                app_data['price'] = float(request.form.get('price', app_data['price']))
                app_data['product'] = request.form.get('product', app_data['product'])
                if action == 'approve':
                    accounts = generate_accounts(app_data['count'], app_data['price'], app_data['product'], agent=app_data['agent'])
                    app_data['status'] = 'approved'
                    app_data['accounts'] = accounts
                elif action == 'reject':
                    app_data['status'] = 'rejected'
                break
        save_applications(apps)
        return redirect(url_for('approvals'))
    products = load_products()
    return render_template('approvals.html', apps=apps, products=products, role=session.get('role'))


@app.route('/ledger')
@admin_required
def ledger_view():
    records = load_ledger()
    product_filter = request.args.get('product', '')
    admin_filter = request.args.get('admin', '')
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    
    # 过滤记录
    filtered_records = records[:]
    if product_filter:
        filtered_records = [r for r in filtered_records if r.get('product') == product_filter]
    if admin_filter:
        filtered_records = [r for r in filtered_records if r.get('admin') == admin_filter]
    if start:
        filtered_records = [r for r in filtered_records if r.get('time', '') >= start]
    if end:
        filtered_records = [r for r in filtered_records if r.get('time', '') <= end]
    
    # 计算统计数据
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    this_month = datetime.now().strftime('%Y-%m')
    this_year = datetime.now().strftime('%Y')
    
    # 计算各时间段收入
    daily = sum(r.get('revenue', 0) for r in records if r.get('time', '').startswith(today))
    monthly = sum(r.get('revenue', 0) for r in records if r.get('time', '').startswith(this_month))
    yearly = sum(r.get('revenue', 0) for r in records if r.get('time', '').startswith(this_year))
    total = sum(r.get('revenue', 0) for r in records)
    
    products = load_products()
    return render_template(
        'ledger.html', records=filtered_records, 
        product_filter=product_filter, admin_filter=admin_filter,
        start=start, end=end, products=products,
        daily=daily, monthly=monthly, yearly=yearly, total=total, role=session.get('role')
    )


@app.route('/agent/ledger')
@agent_required
def agent_ledger():
    records = [r for r in load_ledger() if r.get('admin') == session.get('username')]
    product_filter = request.args.get('product', '')
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if product_filter:
        records = [r for r in records if r.get('product') == product_filter]
    if start:
        records = [r for r in records if r.get('time', '') >= start]
    if end:
        records = [r for r in records if r.get('time', '') <= end]
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    this_month = datetime.now().strftime('%Y-%m')
    this_year = datetime.now().strftime('%Y')
    daily = sum(r.get('revenue', 0) for r in records if r.get('time', '').startswith(today))
    monthly = sum(r.get('revenue', 0) for r in records if r.get('time', '').startswith(this_month))
    yearly = sum(r.get('revenue', 0) for r in records if r.get('time', '').startswith(this_year))
    total = sum(r.get('revenue', 0) for r in records)
    products = load_products()
    return render_template(
        'ledger.html', records=records,
        product_filter=product_filter, admin_filter=session.get('username'),
        start=start, end=end, products=products,
        daily=daily, monthly=monthly, yearly=yearly, total=total, role=session.get('role')
    )



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001, help='Port to run the server on')
    args = parser.parse_args()
    app.run(host='0.0.0.0', port=args.port, debug=True)

