import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session
from datetime import datetime, date

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default_fallback_key')

DATABASE_URL = os.environ.get('DATABASE_URL')
PIN_WAITER = os.environ.get('PIN_WAITER', '1234')
PIN_MANAGER = os.environ.get('PIN_MANAGER', '9999')

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Vehicle Types Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vehicle_types (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) UNIQUE NOT NULL,
            default_price NUMERIC(10, 2) NOT NULL
        );
    ''')
    
    # Tickets Table
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
    
    # Expense Log Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            category VARCHAR(100) NOT NULL,
            amount NUMERIC(10, 2) NOT NULL,
            description TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    
    # Seed Default Vehicle Types if empty
    cursor.execute('SELECT COUNT(*) FROM vehicle_types;')
    if cursor.fetchone()['count'] == 0:
        cursor.executemany(
            'INSERT INTO vehicle_types (name, default_price) VALUES (%s, %s);',
            [('Saloon / Sedan', 300), ('SUV / Crossover', 450), ('Motorbike', 150), ('Boda / TukTuk', 200)]
        )
    conn.commit()
    cursor.close()
    conn.close()

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

    if role == 'waiter' and pin == PIN_WAITER:
        session['role'] = 'waiter'
        return jsonify({'success': True, 'role': 'waiter'})
    elif role == 'manager' and pin == PIN_MANAGER:
        session['role'] = 'manager'
        return jsonify({'success': True, 'role': 'manager'})
    
    return jsonify({'error': 'Invalid PIN'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

# --- VEHICLE TYPES API ---
@app.route('/api/vehicle-types', methods=['GET', 'POST'])
def vehicle_types():
    conn = get_db()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        if session.get('role') != 'manager':
            cursor.close()
            conn.close()
            return jsonify({'error': 'Unauthorized'}), 403
        data = request.json or {}
        try:
            cursor.execute(
                'INSERT INTO vehicle_types (name, default_price) VALUES (%s, %s);',
                (data['name'], float(data['default_price']))
            )
            conn.commit()
            cursor.close()
            conn.close()
            return jsonify({'success': True})
        except psycopg2.IntegrityError:
            conn.rollback()
            cursor.close()
            conn.close()
            return jsonify({'error': 'Vehicle type already exists'}), 400

    cursor.execute('SELECT * FROM vehicle_types ORDER BY name ASC;')
    types = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(types)

# --- TICKETS & OPERATIONAL QUEUE API ---
@app.route('/api/queue', methods=['GET'])
def get_queue():
    role = session.get('role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = get_db()
    cursor = conn.cursor()
    
    today = date.today().isoformat()
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

    cursor.close()
    conn.close()

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
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO tickets (plate, vehicle_type, service_name, amount, payment_method, washer_name, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'QUEUED');
    ''', (
        data['plate'].upper().strip(),
        data['vehicle_type'],
        data['service_name'],
        float(data['amount']),
        data['payment_method'],
        data['washer_name']
    ))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/tickets/<int:ticket_id>/status', methods=['PUT'])
def update_status(ticket_id):
    if session.get('role') != 'waiter':
        return jsonify({'error': 'Manager is strictly restricted to View-Only on active queue'}), 403

    data = request.json or {}
    new_status = data.get('status')
    conn = get_db()
    cursor = conn.cursor()

    if new_status == 'COMPLETED':
        cursor.execute('UPDATE tickets SET status = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s;', (new_status, ticket_id))
    else:
        cursor.execute('UPDATE tickets SET status = %s WHERE id = %s;', (new_status, ticket_id))
    
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({'success': True})

# --- MANAGER HISTORICAL TRACK RECORD LEDGER ---
@app.route('/api/history', methods=['GET'])
def get_history():
    if session.get('role') != 'manager':
        return jsonify({'error': 'Unauthorized'}), 403

    target_date = request.args.get('date', date.today().isoformat())
    conn = get_db()
    cursor = conn.cursor()

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

    cursor.close()
    conn.close()

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

    conn = get_db()
    cursor = conn.cursor()

    if request.method == 'POST':
        data = request.json or {}
        cursor.execute('''
            INSERT INTO expenses (category, amount, description)
            VALUES (%s, %s, %s);
        ''', (data['category'], float(data['amount']), data.get('description', '')))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'success': True})

    target_date = request.args.get('date', date.today().isoformat())
    cursor.execute('SELECT * FROM expenses WHERE created_at::date = %s ORDER BY id DESC;', (target_date,))
    expense_list = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(expense_list)

# --- CUSTOMER TRACKING ENDPOINT ---
@app.route('/api/customer/track', methods=['POST'])
def track_customer():
    data = request.json or {}
    plate = data.get('plate', '').upper().strip()
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM tickets 
        WHERE plate = %s 
        ORDER BY id DESC LIMIT 1;
    ''', (plate,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return jsonify({'found': False, 'message': 'No vehicle found with that plate number.'})

    ticket = dict(row)
    cars_ahead = 0
    if ticket['status'] != 'COMPLETED':
        cursor.execute('''
            SELECT COUNT(*) FROM tickets 
            WHERE status != 'COMPLETED' AND id < %s AND created_at::date = %s::date;
        ''', (ticket['id'], ticket['created_at']))
        cars_ahead = cursor.fetchone()['count']

    cursor.close()
    conn.close()

    return jsonify({
        'found': True,
        'ticket': {
            'plate': ticket['plate'],
            'type': ticket['vehicle_type'],
            'service': ticket['service_name'],
            'amount': float(ticket['amount']),
            'washer': ticket['washer_name'],
            'status': ticket['status'],
            'time_in': ticket['created_at'].strftime('%Y-%m-%d %H:%M:%S') if ticket['created_at'] else '',
            'cars_ahead': cars_ahead
        }
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)