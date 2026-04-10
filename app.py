from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import threading
import time
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = "campuscoin_tracker_2026"

# --- CONFIGURATION ---

# 1. Cloudinary Setup (Participants will use this for receipt uploads)
cloudinary.config( 
    cloud_name = os.environ.get("CLOUDINARY_NAME", "your_cloud_name"), 
    api_key = os.environ.get("CLOUDINARY_KEY", "your_api_key"), 
    api_secret = os.environ.get("CLOUDINARY_SECRET", "your_api_secret") 
)

# 2. MongoDB & Mail Setup
# Atlas requires a database name in the URI for flask_pymongo's .db to resolve.
app.config["MONGO_URI"] = "mongodb+srv://khatritanushri28_db_user:Tanu%400928@cluster0.gtjdr9b.mongodb.net/yourtreasurer?retryWrites=true&w=majority"

mongo = PyMongo(app)
try:
    mongo.db.users.find_one()
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print("❌ MongoDB Connection Failed:", e)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", 'your_app_password') 
mail = Mail(app)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 # 5MB limit for receipts

# Routes that skip MongoDB credential re-check (Budget Gateway is the public entry).
PUBLIC_ENDPOINTS = frozenset({
    'budget_gateway',
    'logout',
    'about_us',
    'static',
})

# --- ASYNC BACKGROUND TASKS ---

def send_async_email(app, msg):
    """Function to send email in a background thread to prevent UI freezing."""
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent successfully!")
        except Exception as e:
            print(f"Background Mail Error: {e}")

# --- GLOBAL CHECKS (Zero-persistence: no module-level user state; verify in Mongo each request) ---

def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_expense_datetime(doc):
    """
    Extract a datetime from common expense date fields.
    Supported: datetime objects, ISO-ish strings, and YYYY-MM-DD strings.
    """
    for key in ("spent_at", "date", "expense_date", "created_at"):
        raw = doc.get(key)
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            value = raw.strip()
            if not value:
                continue
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
            try:
                return datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                pass
    return None


def _apply_budget_cycle_reset_if_due(user_doc):
    """Task 1: if budget_start_date + 30 days has passed, reset spend and roll start date."""
    oid = user_doc['_id']
    start = user_doc.get('budget_start_date')
    now = datetime.utcnow()
    if start is None:
        mongo.db.users.update_one(
            {'_id': oid},
            {'$set': {'budget_start_date': now, 'current_spend': 0.0}},
        )
        return
    if isinstance(start, datetime) and now > start + timedelta(days=30):
        mongo.db.users.update_one(
            {'_id': oid},
            {'$set': {'budget_start_date': now, 'current_spend': 0.0}},
        )


def _redirect_after_successful_auth(next_path):
    if next_path and next_path.startswith('/') and not next_path.startswith('//'):
        sep = '&' if '?' in next_path else '?'
        return redirect(f'{next_path}{sep}unlocked=1')
    return redirect(url_for('home', unlocked=1))


@app.before_request
def verify_credentials_against_mongodb():
    """
    Competition rule: on every protected request, re-verify name + password against Atlas.
    Session only holds the clues; truth is always the users collection.
    """
    ep = request.endpoint
    if ep is None or ep in PUBLIC_ENDPOINTS:
        return

    name = session.get('username')
    password_plain = session.get('password_plain')
    if not name or not password_plain:
        return redirect(url_for('budget_gateway', next=request.path))

    try:
        user = mongo.db.users.find_one({'name': name})
    except Exception as e:
        print(f"Credential verification error: {e}")
        session.clear()
        flash('Database is temporarily unreachable. Please re-authenticate.', 'error')
        return redirect(url_for('budget_gateway'))
    if not user or not check_password_hash(user['password'], password_plain):
        session.clear()
        flash('Credentials could not be verified. Please use the Budget Gateway.', 'error')
        return redirect(url_for('budget_gateway'))


@app.route('/budget_gateway', methods=['GET', 'POST'])
def budget_gateway():
    if request.method == 'GET':
        if session.get('username') and session.get('password_plain'):
            existing = mongo.db.users.find_one({'name': session['username']})
            if existing and check_password_hash(
                existing['password'], session['password_plain']
            ):
                return redirect(url_for('home'))
        return render_template(
            'budget_gateway.html',
            name=request.args.get('prefill', ''),
        )

    name = (request.form.get('name') or '').strip()
    password = request.form.get('password') or ''
    monthly_limit_raw = (request.form.get('monthly_limit') or '').strip()
    next_path = (request.form.get('next') or '').strip()

    if not name or not password:
        flash('Name and password are required.', 'error')
        return redirect(url_for('budget_gateway'))

    user = mongo.db.users.find_one({'name': name})

    if user:
        if not check_password_hash(user['password'], password):
            flash('Credentials do not match our records.', 'error')
            return redirect(url_for('budget_gateway'))
        _apply_budget_cycle_reset_if_due(user)
        user = mongo.db.users.find_one({'name': name})
        session['username'] = name
        session['password_plain'] = password
        return _redirect_after_successful_auth(next_path)

    if not monthly_limit_raw:
        flash('New accounts must set a monthly limit.', 'error')
        return redirect(url_for('budget_gateway', prefill=name))

    try:
        monthly_limit = float(monthly_limit_raw)
        if monthly_limit < 0:
            raise ValueError()
    except ValueError:
        flash('Monthly limit must be a valid non-negative number.', 'error')
        return redirect(url_for('budget_gateway', prefill=name))

    mongo.db.users.insert_one({
        'name': name,
        'password': generate_password_hash(password),
        'monthly_limit': monthly_limit,
        'budget_start_date': datetime.utcnow(),
        'current_spend': 0.0,
    })
    session['username'] = name
    session['password_plain'] = password
    return _redirect_after_successful_auth(next_path)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('budget_gateway'))


# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    # TODO: Fetch today's expenses to show a quick summary on the home dashboard
    return render_template('index.html')

@app.route('/my_profile')
def my_profile():
    user = mongo.db.users.find_one({'name': session['username']})
    return render_template('profile.html', user=user)

@app.route('/my_expenses')
def my_expenses():
    # TODO: Fetch all expenses from MongoDB, sort by date, and pass to template
    return render_template('expenses.html')

@app.route('/analysis')
def analysis():
    return render_template('analysis.html')

@app.route('/interval_spend')
def interval_spend():
    # TODO: Fetch EMI and Subscription data to display upcoming dues
    return render_template('interval_spend.html')

@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

# --- DATA SUBMISSION ROUTES (THE LOGIC) ---

@app.route('/add_expense', methods=['POST'])
def add_expense():
    """Handles adding a new daily expense."""
    try:
        form_data = request.form.to_dict()
        
        # 1. TODO: Handle Cloudinary receipt upload if 'receipt_image' exists in request.files
        # 2. TODO: Insert form_data into MongoDB 'expenses' collection
        # 3. TODO: Calculate if total month spend > 90% of threshold. If yes, trigger send_async_email()

        return redirect(url_for('my_expenses'))
    except Exception as e:
        print(f"Expense Submit Error: {e}")
        return f"Submission failed: {e}", 500

@app.route('/add_friend_loan', methods=['POST'])
def add_friend_loan():
    """Handles logging money given to a friend and sending initial email."""
    try:
        form_data = request.form.to_dict()
        # TODO: Save loan to database
        # TODO: Send async email to friend stating "You owe me money for..."
        
        return redirect(url_for('my_expenses'))
    except Exception as e:
        return "Internal Error", 500

@app.route('/add_interval_spend', methods=['POST'])
def add_interval_spend():
    """Handles adding EMIs, Hostel Fees, Subscriptions."""
    try:
        form_data = request.form.to_dict()
        # TODO: Save interval spend to MongoDB, calculate next due date
        return redirect(url_for('interval_spend'))
    except Exception as e:
        return "Internal Error", 500

# --- API ROUTES (FOR REAL-TIME CHARTS) ---

@app.route('/api/spend_data')
def spend_data():
    """Live analysis data for day/month chart views from MongoDB."""
    username = session.get('username')
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = (today_start - timedelta(days=29))

    hourly = [0.0] * 24
    daily_map = {}
    for i in range(30):
        day = (month_start + timedelta(days=i)).date()
        daily_map[day.isoformat()] = 0.0

    try:
        expenses = mongo.db.daily_expenses.find({'username': username})
        for doc in expenses:
            dt = _extract_expense_datetime(doc)
            if not dt:
                continue
            amount = _safe_float(doc.get('amount'))

            if dt.date() == today_start.date():
                hourly[dt.hour] += amount

            if month_start.date() <= dt.date() <= today_start.date():
                key = dt.date().isoformat()
                if key in daily_map:
                    daily_map[key] += amount
    except Exception as e:
        print(f"Spend data fetch error: {e}")

    month_labels = sorted(daily_map.keys())
    month_values = [round(daily_map[k], 2) for k in month_labels]
    day_values = [round(v, 2) for v in hourly]

    return jsonify({
        'day': {
            'labels': [str(i) for i in range(24)],
            'data': day_values,
        },
        'month': {
            'labels': month_labels,
            'data': month_values,
        },
    })


@app.route('/test_db')
def test_db():
    try:
        mongo.db.users.find_one()
        return "DB Connected ✅"
    except Exception as e:
        return f"Error: {e}"

@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)