import os
from datetime import datetime, date
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session
from flask.json.provider import DefaultJSONProvider

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default_fallback_key')

DATABASE_URL = os.environ.get('DATABASE_URL')
DEFAULT_PIN_WAITER = os.environ.get('PIN_WAITER', '1234')
DEFAULT_PIN_MANAGER = os.environ.get('PIN_MANAGER', '9999')

class CustomJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)

app.json_provider_class = CustomJSONProvider
app.json = CustomJSONProvider(app)

@contextmanager
def get_db_cursor(commit=False):
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing.")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    cursor = conn.cursor()
    try:
        yield cursor
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

def get_setting(key, default_value):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT value FROM settings WHERE key = %s;", (key,))
        row = cursor.fetchone()
        return row['value'] if row else default_value

def set_setting(key, value):
    with get_db_cursor(commit=True) as cursor:
        cursor.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """, (key, value))

def init_db():
    with get_db_cursor(commit=True) as cursor:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(50) PRIMARY KEY,
                value VARCHAR(255) NOT NULL
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS staff (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                role VARCHAR(20) DEFAULT 'waiter',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vehicle_types (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) UNIQUE NOT NULL,
                default_price NUMERIC(10, 2) NOT NULL
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                plate VARCHAR(20) NOT NULL,
                vehicle_type VARCHAR(50) NOT NULL,
                service_name VARCHAR(100) NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                payment_method VARCHAR(50) NOT NULL,
                washer_name VARCHAR(100) NOT NULL,
                status VARCHAR(20) DEFAULT 'QUEUED',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                category VARCHAR(100) NOT NULL,
                amount NUMERIC(10, 2) NOT NULL,
                description TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        
        cursor.execute("INSERT INTO settings (key, value) VALUES ('PIN_WAITER', %s) ON CONFLICT DO NOTHING;", (DEFAULT_PIN_WAITER,))
        cursor.execute("INSERT INTO settings (key, value) VALUES ('PIN_MANAGER', %s) ON CONFLICT DO NOTHING;", (DEFAULT_PIN_MANAGER,))

        cursor.execute('SELECT COUNT(*) FROM vehicle_types;')
        if cursor.fetchone()['count'] == 0:
            cursor.executemany(
                'INSERT INTO vehicle_types (name, default_price) VALUES (%s, %s);',
                [('Saloon / Sedan', 300), ('SUV / Crossover', 450), ('Motorbike', 150), ('Boda / TukTuk', 200)]
            )

init_db()

# --- AUTHENTICATION ENDPOINTS ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    role = data.get('role')
    pin = data.get('pin')

    waiter_pin = get_setting('PIN_WAITER', DEFAULT_PIN_WAITER)
    manager_pin = get_setting('PIN_MANAGER', DEFAULT_PIN_MANAGER)

    if role == 'waiter' and pin == waiter_pin:
        session['role'] = 'waiter'
        return jsonify({'success': True, 'role': 'waiter'})
    elif role == 'manager' and pin == manager_pin:
        session['role'] = 'manager'
        return jsonify({'success': True, 'role': 'manager'})
    
    return jsonify({'error': 'Invalid PIN credentials'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

# --- MANAGER ADMIN API: PIN CHANGE, STAFF & RESET ---
@app.route('/api/admin/change-pin', methods=['POST'])
def change_pin():
    if session.get('role') != 'manager':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.json or {}
    target_role = data.get('target_role')
    new_pin = str(data.get('new_pin', '')).strip()

    if not new_pin or len(new_pin) < 4:
        return jsonify({'error': 'PIN must be at least 4 digits'}), 400

    if target_role == 'waiter':
        set_setting('PIN_WAITER', new_pin)
    elif target_role == 'manager':
        set_setting('PIN_MANAGER', new_pin)
    else:
        return jsonify({'error': 'Invalid role target'}), 400

    return jsonify({'success': True, 'message': f'{target_role.capitalize()} PIN updated successfully'})

@app.route('/api/admin/staff', methods=['GET', 'POST', 'DELETE'])
def manage_staff():
    if session.get('role') != 'manager' and request.method != 'GET':
        return jsonify({'error': 'Unauthorized'}), 403

    if request.method == 'POST':
        data = request.json or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Staff name is required'}), 400
        try:
            with get_db_cursor(commit=True) as cursor:
                cursor.execute('INSERT INTO staff (name, role) VALUES (%s, %s);', (name, 'waiter'))
            return jsonify({'success': True})
        except psycopg2.IntegrityError:
            return jsonify({'error': 'Staff member already exists'}), 400

    elif request.method == 'DELETE':
        staff_id = request.args.get('id')
        with get_db_cursor(commit=True) as cursor:
            cursor.execute('DELETE FROM staff WHERE id = %s;', (staff_id,))
        return jsonify({'success': True})

    with get_db_cursor() as cursor:
        cursor.execute('SELECT * FROM staff ORDER BY name ASC;')
        staff_list = cursor.fetchall()
    return jsonify(staff_list)

@app.route('/api/admin/reset-database', methods=['POST'])
def reset_database():
    if session.get('role') != 'manager':
        return jsonify({'error': 'Unauthorized. Only managers can reset records.'}), 403

    data = request.json or {}
    reset_type = data.get('reset_type', 'records_only')

    with get_db_cursor(commit=True) as cursor:
        if reset_type == 'full':
            cursor.execute('TRUNCATE TABLE tickets, expenses, staff, vehicle_types, settings RESTART IDENTITY;')
        else:
            cursor.execute('TRUNCATE TABLE tickets, expenses RESTART IDENTITY;')

    init_db()

    return jsonify({'success': True, 'message': 'Database records reset successfully!'})

# --- VEHICLE TYPES API ---
@app.route('/api/vehicle-types', methods=['GET', 'POST'])
def vehicle_types():
    if request.method == 'POST':
        if session.get('role') != 'manager':
            return jsonify({'error': 'Unauthorized'}), 403
        data = request.json or {}
        try:
            with get_db_cursor(commit=True) as cursor:
                cursor.execute(
                    'INSERT INTO vehicle_types (name, default_price) VALUES (%s, %s);',
                    (data['name'].strip(), float(data['default_price']))
                )
            return jsonify({'success': True})
        except psycopg2.IntegrityError:
            return jsonify({'error': 'Vehicle type already exists'}), 400

    with get_db_cursor() as cursor:
        cursor.execute('SELECT * FROM vehicle_types ORDER BY name ASC;')
        types = cursor.fetchall()
    return jsonify(types)

# --- TICKETS & OPERATIONAL QUEUE API ---
@app.route('/api/queue', methods=['GET'])
def get_queue():
    role = session.get('role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
    
    today = date.today().isoformat()
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM tickets 
            WHERE status != 'COMPLETED' AND created_at::date = %s
            ORDER BY id ASC;
        ''', (today,))
        queue_list = cursor.fetchall()

        cursor.execute('SELECT COALESCE(SUM(amount), 0) AS total FROM tickets WHERE status = \'COMPLETED\' AND completed_at::date = %s;', (today,))
        today_revenue = float(cursor.fetchone()['total'])

        cursor.execute('SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE created_at::date = %s;', (today,))
        today_expenses = float(cursor.fetchone()['total'])

        cursor.execute('SELECT COUNT(*) FROM tickets WHERE status = \'COMPLETED\' AND completed_at::date = %s;', (today,))
        completed_count = cursor.fetchone()['count']

    return jsonify({
        'queue': queue_list,
        'summary': {
            'role': role,
            'active_count': len(queue_list),
            'completed_count': completed_count,
            'today_revenue': today_revenue,
            'today_expenses': today_expenses,
            'net_profit': today_revenue - today_expenses
        }
    })

@app.route('/api/tickets', methods=['POST'])
def add_ticket():
    if session.get('role') != 'waiter':
        return jsonify({'error': 'Only waiters can register new intake tickets'}), 403

    data = request.json or {}
    with get_db_cursor(commit=True) as cursor:
        cursor.execute('''
            INSERT INTO tickets (plate, vehicle_type, service_name, amount, payment_method, washer_name, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED');
        ''', (
            data['plate'].upper().strip(),
            data['vehicle_type'],
            data['service_name'].strip(),
            float(data['amount']),
            data['payment_method'],
            data['washer_name'].strip()
        ))
    return jsonify({'success': True})

@app.route('/api/tickets/<int:ticket_id>/status', methods=['PUT'])
def update_status(ticket_id):
    if session.get('role') != 'waiter':
        return jsonify({'error': 'Manager is strictly restricted to View-Only on active queue'}), 403

    data = request.json or {}
    new_status = data.get('status')
    
    with get_db_cursor(commit=True) as cursor:
        if new_status == 'COMPLETED':
            cursor.execute('UPDATE tickets SET status = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s;', (new_status, ticket_id))
        else:
            cursor.execute('UPDATE tickets SET status = %s WHERE id = %s;', (new_status, ticket_id))
            
    return jsonify({'success': True})

# --- MANAGER HISTORICAL TRACK RECORD LEDGER ---
@app.route('/api/history', methods=['GET'])
def get_history():
    if session.get('role') != 'manager':
        return jsonify({'error': 'Unauthorized'}), 403

    target_date = request.args.get('date', date.today().isoformat())
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM tickets 
            WHERE status = 'COMPLETED' AND completed_at::date = %s
            ORDER BY completed_at DESC;
        ''', (target_date,))
        history = cursor.fetchall()

        cursor.execute('SELECT COALESCE(SUM(amount), 0) AS total FROM tickets WHERE status = \'COMPLETED\' AND completed_at::date = %s;', (target_date,))
        revenue = float(cursor.fetchone()['total'])

        cursor.execute('SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE created_at::date = %s;', (target_date,))
        expenses = float(cursor.fetchone()['total'])

    return jsonify({
        'history': history,
        'metrics': {
            'revenue': revenue,
            'expenses': expenses,
            'profit': revenue - expenses,
            'count': len(history)
        }
    })

# --- EXPENSES API ---
@app.route('/api/expenses', methods=['GET', 'POST'])
def expenses():
    if session.get('role') != 'manager':
        return jsonify({'error': 'Unauthorized'}), 403

    if request.method == 'POST':
        data = request.json or {}
        with get_db_cursor(commit=True) as cursor:
            cursor.execute('''
                INSERT INTO expenses (category, amount, description)
                VALUES (%s, %s, %s);
            ''', (data['category'], float(data['amount']), data.get('description', '').strip()))
        return jsonify({'success': True})

    target_date = request.args.get('date', date.today().isoformat())
    with get_db_cursor() as cursor:
        cursor.execute('SELECT * FROM expenses WHERE created_at::date = %s ORDER BY id DESC;', (target_date,))
        expense_list = cursor.fetchall()
    return jsonify(expense_list)

# --- CUSTOMER TRACKING ENDPOINT ---
@app.route('/api/customer/track', methods=['POST'])
def track_customer():
    data = request.json or {}
    plate = data.get('plate', '').upper().strip()
    
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM tickets 
            WHERE plate = %s 
            ORDER BY id DESC LIMIT 1;
        ''', (plate,))
        row = cursor.fetchone()

        if not row:
            return jsonify({'found': False, 'message': 'No vehicle found with that plate number.'})

        ticket = dict(row)
        cars_ahead = 0
        if ticket['status'] != 'COMPLETED':
            cursor.execute('''
                SELECT COUNT(*) FROM tickets 
                WHERE status != 'COMPLETED' AND id < %s AND created_at::date = %s::date;
            ''', (ticket['id'], ticket['created_at']))
            cars_ahead = cursor.fetchone()['count']

    time_in_str = ticket['created_at'].strftime('%Y-%m-%d %H:%M:%S') if isinstance(ticket['created_at'], datetime) else str(ticket['created_at'])

    return jsonify({
        'found': True,
        'ticket': {
            'plate': ticket['plate'],
            'type': ticket['vehicle_type'],
            'service': ticket['service_name'],
            'amount': float(ticket['amount']),
            'washer': ticket['washer_name'],
            'status': ticket['status'],
            'time_in': time_in_str,
            'cars_ahead': cars_ahead
        }
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)