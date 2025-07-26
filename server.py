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
APPLICATIONS_FILE = os.path.join(BASE_DIR, 'applications.json')
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
                    info.setdefault('is_agent', False)
                    info.setdefault('owner', '')
                    info.setdefault('forsale', False)
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


def load_applications() -> list:
    if os.path.exists(APPLICATIONS_FILE):
        with open(APPLICATIONS_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f).get('apps', [])
            except Exception:
                return []
    return []


def save_applications(apps: list) -> None:
    with open(APPLICATIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'apps': apps}, f, indent=4, ensure_ascii=False)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('admin') != 'horsray':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def agent_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('agent'):
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
            if user.get('is_admin'):
                session['admin'] = username
                return redirect(url_for('user_list'))
            elif user.get('is_agent'):
                session['agent'] = username
                return redirect(url_for('agent_users'))
        return render_template('login.html', error='登录失败')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('admin', None)
    session.pop('agent', None)
    return redirect(url_for('login'))


@app.route('/')
@admin_required
def index():
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
        total=len(accounts) if accounts else 0
    )


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
        status=status, sort=sort, start=start, end=end, products=products
    )


@app.route('/users/add', methods=['POST'])
@admin_required
def add_user():
    username = request.form.get('username')
    password = request.form.get('password')
    nickname = request.form.get('nickname', '')
    is_admin = bool(request.form.get('is_admin'))
    is_agent = bool(request.form.get('is_agent'))
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
        'is_agent': is_agent,
        'enabled': True,
        'source': 'add',
        'price': price,
        'product': product,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'last_login': None,
        'ip_address': '',
        'location': '',
        'remark': ''
    }
    save_users(users)
    # ledger
    records = load_ledger()
    records.append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'admin': session.get('admin'),
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


@app.route('/sales/users/<name>/sold', methods=['POST'])
@agent_required
def mark_sold(name):
    users = load_users()
    current = session.get('agent')
    if name in users and users[name].get('owner') == current and users[name].get('forsale'):
        users[name]['forsale'] = False
        save_users(users)
        if request.is_json:
            return jsonify({'success': True})
    if request.is_json:
        return jsonify({'success': False}), 404
    return redirect(url_for('agent_users'))


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


@app.route('/users/<name>/update', methods=['POST'])
@admin_required
def update_user(name):
    data = request.get_json(silent=True) or request.form
    new_name = data.get('username')
    password = data.get('password')
    nickname = data.get('nickname')
    is_admin = bool(data.get('is_admin'))
    is_agent = bool(data.get('is_agent'))
    enabled = bool(data.get('enabled'))
    product = data.get('product')
    remark = data.get('remark', '')
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
        users[name]['is_agent'] = is_agent
        users[name]['enabled'] = enabled
        if product is not None:
            users[name]['product'] = product
        users[name]['remark'] = remark
        save_users(users)
        if request.is_json:
            return jsonify({'success': True})
    if request.is_json:
        return jsonify({'success': False}), 404
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
            'admin': session.get('admin'),
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


@app.route('/users/bulk_create', methods=['POST'])
@admin_required
def bulk_create():
    count = int(request.form.get('count', 0))
    price = float(request.form.get('price') or 0)
    product = request.form.get('product', '')
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
            'location': ''
        }
        new_accounts.append({'username': uname, 'password': pwd})
    save_users(users)
    session['bulk_accounts'] = new_accounts
    session['bulk_info'] = {
        'product': product,
        'price': price,
        'admin': session.get('admin'),
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    if count > 0 and price > 0:
        records = load_ledger()
        records.append({
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'admin': session.get('admin'),
            'product': product,
            'price': price,
            'count': count,
            'revenue': price * count
        })
        save_ledger(records)
    return redirect(url_for('bulk_manage'))


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
        daily=daily, monthly=monthly, yearly=yearly, total=total
    )


@app.route('/sales/users')
@agent_required
def agent_users():
    users = load_users()
    current = session.get('agent')
    my_users = {k: v for k, v in users.items() if v.get('owner') == current}
    return render_template('users.html', users=my_users, total=len(my_users), page=1, per_page=len(my_users), query='', source='', status='', sort='desc', start='', end='', products=load_products())


@app.route('/sales/ledger')
@agent_required
def agent_ledger():
    records = [r for r in load_ledger() if r.get('admin') == session.get('agent')]
    products = load_products()
    return render_template('ledger.html', records=records, product_filter='', admin_filter=session.get('agent'), start='', end='', products=products, daily=0, monthly=0, yearly=0, total=0)


@app.route('/sales/apply', methods=['GET', 'POST'])
@agent_required
def apply_bulk():
    products = load_products()
    if request.method == 'POST':
        count = int(request.form.get('count', 0))
        price = float(request.form.get('price') or 0)
        product = request.form.get('product', '')
        apps = load_applications()
        apps.append({
            'id': os.urandom(6).hex(),
            'agent': session.get('agent'),
            'count': count,
            'price': price,
            'product': product,
            'status': 'pending',
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        save_applications(apps)
        return redirect(url_for('apply_bulk'))
    return render_template('bulk.html', accounts=None, products=products, info=None, page=1, per_page=20, total=0)


@app.route('/applications')
@admin_required
def applications_list():
    apps = load_applications()
    return render_template('applications.html', apps=apps)


@app.route('/applications/<app_id>/approve', methods=['POST'])
@admin_required
def approve_application(app_id):
    apps = load_applications()
    app_record = next((a for a in apps if a['id'] == app_id), None)
    if not app_record:
        return redirect(url_for('applications_list'))
    if app_record['status'] != 'pending':
        return redirect(url_for('applications_list'))
    # generate accounts
    users = load_users()
    new_accounts = []
    for _ in range(app_record['count']):
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
            'source': 'agent',
            'product': app_record['product'],
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'last_login': None,
            'price': app_record['price'],
            'ip_address': '',
            'location': '',
            'owner': app_record['agent'],
            'forsale': True
        }
        new_accounts.append({'username': uname, 'password': pwd})
    save_users(users)
    # ledger
    records = load_ledger()
    records.append({
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'admin': app_record['agent'],
        'product': app_record['product'],
        'price': app_record['price'],
        'count': app_record['count'],
        'revenue': app_record['price'] * app_record['count']
    })
    save_ledger(records)
    app_record['status'] = 'approved'
    save_applications(apps)
    return redirect(url_for('applications_list'))


@app.route('/applications/<app_id>/reject', methods=['POST'])
@admin_required
def reject_application(app_id):
    apps = load_applications()
    for a in apps:
        if a['id'] == app_id and a['status'] == 'pending':
            a['status'] = 'rejected'
            break
    save_applications(apps)
    return redirect(url_for('applications_list'))



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001, help='Port to run the server on')
    args = parser.parse_args()
    app.run(host='0.0.0.0', port=args.port, debug=True)

