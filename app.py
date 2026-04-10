import os
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Mail, Message
from flask_pymongo import PyMongo
from pymongo.errors import ConfigurationError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__, template_folder="Templates", static_folder="Static", static_url_path="/static")
app.secret_key = "campuscoin_tracker_2026"

# --- CONFIGURATION ---

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_NAME", "your_cloud_name"),
    api_key=os.environ.get("CLOUDINARY_KEY", "your_api_key"),
    api_secret=os.environ.get("CLOUDINARY_SECRET", "your_api_secret"),
)

# MongoDB Setup
def build_mongo_uri_with_timeouts(raw_uri):
    split_result = urlsplit(raw_uri)
    query = dict(parse_qsl(split_result.query, keep_blank_values=True))
    timeout_defaults = {
        "serverSelectionTimeoutMS": "5000",
        "connectTimeoutMS": "5000",
        "socketTimeoutMS": "5000",
    }
    for key, value in timeout_defaults.items():
        query.setdefault(key, value)
    new_query = urlencode(query)
    return urlunsplit(
        (
            split_result.scheme,
            split_result.netloc,
            split_result.path,
            new_query,
            split_result.fragment,
        )
    )

app = Flask(__name__)
app.secret_key = "secret123"

raw_mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/yourtreasurer")
app.config["MONGO_URI"] = build_mongo_uri_with_timeouts(raw_mongo_uri)
app.config["MONGO_DBNAME"] = os.environ.get("MONGO_DBNAME", "yourtreasurer")
MONGO_INIT_ERROR = ""
try:
    mongo = PyMongo(app)
except Exception as error:
    mongo = None
    MONGO_INIT_ERROR = str(error)

# Mail Setup
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USER", "your_email@gmail.com")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASS", "your_app_password")
mail = Mail(app)

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# Local storage files
LOCAL_USERS_FILE = os.path.join(app.root_path, "local_users.json")
LOCAL_EXPENSES_FILE = os.path.join(app.root_path, "local_daily_expenses.json")
LOCAL_RECURRING_FILE = os.path.join(app.root_path, "local_recurring.json")

MONGO_AVAILABLE = None
MONGO_LAST_CHECK = None
MONGO_LAST_ERROR = ""
PASSWORD_RULES_TEXT = "Password must be at least 8 characters and include uppercase, lowercase, number, and special character."
NAME_RULES_TEXT = "Name must contain only letters and spaces."

# --- HELPER FUNCTIONS ---

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent successfully!")
        except Exception as e:
            print(f"Background Mail Error: {e}")

def is_mongo_available():
    global MONGO_AVAILABLE, MONGO_LAST_CHECK, MONGO_LAST_ERROR
    if mongo is None:
        MONGO_AVAILABLE = False
        MONGO_LAST_ERROR = MONGO_INIT_ERROR or "MongoDB client is not initialized."
        MONGO_LAST_CHECK = datetime.utcnow()
        return False
    now = datetime.utcnow()
    if MONGO_LAST_CHECK and (now - MONGO_LAST_CHECK).total_seconds() < 30:
        return bool(MONGO_AVAILABLE)
    try:
        mongo.cx.admin.command("ping")
        MONGO_AVAILABLE = True
        MONGO_LAST_ERROR = ""
    except PyMongoError as error:
        MONGO_AVAILABLE = False
        MONGO_LAST_ERROR = str(error)
    MONGO_LAST_CHECK = now
    return bool(MONGO_AVAILABLE)

def is_password_valid(password):
    if len(password) < 8:
        return False
    has_upper = any(char.isupper() for char in password)
    has_lower = any(char.islower() for char in password)
    has_digit = any(char.isdigit() for char in password)
    has_special = any(not char.isalnum() for char in password)
    return has_upper and has_lower and has_digit and has_special

def is_name_valid(name):
    return bool(re.fullmatch(r"[A-Za-z ]+", name))

# --- LOCAL STORAGE FUNCTIONS ---

def load_local_users():
    if not os.path.exists(LOCAL_USERS_FILE):
        return []
    try:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []

def save_local_users(users):
    with open(LOCAL_USERS_FILE, "w", encoding="utf-8") as file:
        json.dump(users, file, indent=2)

def get_local_user_by_name(name):
    users = load_local_users()
    for user in users:
        if user.get("name") == name:
            return user
    return None

def get_local_user_by_id(user_id):
    users = load_local_users()
    for user in users:
        if user.get("_id") == user_id:
            return user
    return None

def upsert_local_user(updated_user):
    users = load_local_users()
    replaced = False
    for index, user in enumerate(users):
        if user.get("_id") == updated_user.get("_id"):
            users[index] = updated_user
            replaced = True
            break
    if not replaced:
        users.append(updated_user)
    save_local_users(users)

def load_local_expenses():
    if not os.path.exists(LOCAL_EXPENSES_FILE):
        return []
    try:
        with open(LOCAL_EXPENSES_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []

def save_local_expenses(expenses):
    with open(LOCAL_EXPENSES_FILE, "w", encoding="utf-8") as file:
        json.dump(expenses, file, indent=2)

def append_local_expense(expense_doc):
    expenses = load_local_expenses()
    expenses.append(expense_doc)
    save_local_expenses(expenses)

def load_local_recurring():
    if not os.path.exists(LOCAL_RECURRING_FILE):
        return []
    try:
        with open(LOCAL_RECURRING_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []

def save_local_recurring(payments):
    with open(LOCAL_RECURRING_FILE, "w", encoding="utf-8") as file:
        json.dump(payments, file, indent=2)

# --- DATABASE COLLECTION FUNCTIONS ---

def users_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    db_name = app.config["MONGO_DBNAME"]
    return mongo.cx[db_name]["users"]

def daily_expenses_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    db_name = app.config["MONGO_DBNAME"]
    return mongo.cx[db_name]["daily_expenses"]

def recurring_payments_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    db_name = app.config["MONGO_DBNAME"]
    return mongo.cx[db_name]["recurring_payments"]

# --- USER HELPER FUNCTIONS ---

def increment_current_spend(amount):
    user_id = session.get("user_id")
    if not user_id:
        return
    if user_id.startswith("local:"):
        local_user_id = user_id.replace("local:", "", 1)
        user_doc = get_local_user_by_id(local_user_id)
        if user_doc:
            current = float(user_doc.get("current_spend", 0) or 0)
            user_doc["current_spend"] = current + amount
            upsert_local_user(user_doc)
        return
    if is_mongo_available():
        try:
            users_collection().update_one({"_id": ObjectId(user_id)}, {"$inc": {"current_spend": amount}})
        except (InvalidId, PyMongoError):
            return

def parse_start_date(raw_start_date):
    if isinstance(raw_start_date, datetime):
        return raw_start_date
    if isinstance(raw_start_date, str):
        try:
            return datetime.fromisoformat(raw_start_date)
        except ValueError:
            return None
    return None

def build_user_payload(user_doc):
    return {
        "name": user_doc.get("name"),
        "monthly_limit": float(user_doc.get("monthly_limit", 0) or 0),
        "current_spend": float(user_doc.get("current_spend", 0) or 0),
    }

def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False

# --- DUMMY EXPENSES DATA ---
def get_dummy_expenses(user_name):
    """Generate 10 dummy expenses across different categories"""
    return [
        {
            '_id': str(uuid4()),
            'category': '🍔 Food & Dining',
            'amount': 450,
            'description': 'Lunch at Pizza Hut',
            'created_at': (datetime.utcnow() - timedelta(days=0, hours=2)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '🚗 Transportation',
            'amount': 250,
            'description': 'Uber ride to office',
            'created_at': (datetime.utcnow() - timedelta(days=0, hours=5)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '🛒 Groceries',
            'amount': 1200,
            'description': 'Weekly grocery shopping',
            'created_at': (datetime.utcnow() - timedelta(days=1)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '🎬 Entertainment',
            'amount': 599,
            'description': 'Netflix subscription',
            'created_at': (datetime.utcnow() - timedelta(days=2)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '🏠 Rent',
            'amount': 15000,
            'description': 'Monthly apartment rent',
            'created_at': (datetime.utcnow() - timedelta(days=5)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '📚 Education',
            'amount': 2500,
            'description': 'Online course purchase',
            'created_at': (datetime.utcnow() - timedelta(days=7)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '💊 Healthcare',
            'amount': 850,
            'description': 'Pharmacy medicines',
            'created_at': (datetime.utcnow() - timedelta(days=10)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '👕 Shopping',
            'amount': 3200,
            'description': 'New clothes purchase',
            'created_at': (datetime.utcnow() - timedelta(days=12)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '📱 Utilities',
            'amount': 999,
            'description': 'Mobile recharge',
            'created_at': (datetime.utcnow() - timedelta(days=15)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        },
        {
            '_id': str(uuid4()),
            'category': '✈️ Travel',
            'amount': 5000,
            'description': 'Flight booking',
            'created_at': (datetime.utcnow() - timedelta(days=20)).isoformat(),
            'is_loan': False,
            'created_by': user_name
        }
    ]

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/my_profile')
@app.route('/profile')
def my_profile():
    return render_template("profile.html")

@app.route('/my_expenses')
def my_expenses():
    return render_template('expenses.html')

@app.route('/analysis')
def analysis():
    return render_template('analysis.html')

@app.route('/interval_spend')
def interval_spend():
    return render_template('interval_spend.html')

@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

# --- LOGIN/SIGNUP ROUTES ---

# ---------------- LOGIN ---------------- #
@app.route('/login', methods=['GET','POST'])
def login():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""

    if not name or not password:
        return jsonify({"success": False, "message": "Name and password are required."}), 400
    if len(name) < 3:
        return jsonify({"success": False, "message": "Name must be at least 3 characters."}), 400
    if not is_name_valid(name):
        return jsonify({"success": False, "message": NAME_RULES_TEXT}), 400
    if not is_password_valid(password):
        return jsonify({"success": False, "message": PASSWORD_RULES_TEXT}), 400

    use_local_store = not is_mongo_available()
    if use_local_store:
        user_doc = get_local_user_by_name(name)
    else:
        try:
            user_doc = users_collection().find_one({"name": name})
        except PyMongoError:
            user_doc = get_local_user_by_name(name)
            use_local_store = True

    if not user_doc:
        return jsonify({"success": False, "needs_signup": True, "message": "User not found."}), 404

    if not check_password_hash(user_doc.get("password", ""), password):
        return jsonify({"success": False, "message": "Invalid credentials."}), 401

    if use_local_store:
        session["user_id"] = user_doc.get("_id")
    else:
        session["user_id"] = str(user_doc["_id"])
    session["user_name"] = user_doc["name"]

    return jsonify({
        "success": True,
        "message": "Login successful.",
        "user": build_user_payload(user_doc),
        "storage": "local" if use_local_store else "atlas",
        "redirect_url": url_for("home"),
    })

@app.route("/signup", methods=["POST"])
def signup():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""
    monthly_limit_input = payload.get("monthly_limit")

    if not name or not password:
        return jsonify({"success": False, "message": "Name and password are required."}), 400
    if len(name) < 3:
        return jsonify({"success": False, "message": "Name must be at least 3 characters."}), 400
    if not is_name_valid(name):
        return jsonify({"success": False, "message": NAME_RULES_TEXT}), 400
    if not is_password_valid(password):
        return jsonify({"success": False, "message": PASSWORD_RULES_TEXT}), 400

    try:
        monthly_limit = float(monthly_limit_input) if monthly_limit_input not in (None, "") else 0.0
    except ValueError:
        return jsonify({"success": False, "message": "Monthly limit must be a number."}), 400
    if monthly_limit <= 0:
        return jsonify({"success": False, "message": "Monthly limit must be greater than 0."}), 400

    use_local_store = not is_mongo_available()
    if use_local_store:
        existing_user = get_local_user_by_name(name)
    else:
        try:
            existing_user = users_collection().find_one({"name": name})
        except PyMongoError:
            existing_user = get_local_user_by_name(name)
            use_local_store = True

    if existing_user:
        return jsonify({"success": False, "message": "User already exists."}), 409

    now = datetime.utcnow()
    new_user = {
        "name": name,
        "password": generate_password_hash(password),
        "monthly_limit": monthly_limit,
        "current_spend": 0.0,
        "start_date": now,
    }

    if use_local_store:
        user_id = str(uuid4())
        local_user = {
            "_id": user_id,
            "name": name,
            "password": new_user["password"],
            "monthly_limit": monthly_limit,
            "current_spend": 0.0,
            "start_date": now.isoformat(),
        }
        upsert_local_user(local_user)
        session["user_id"] = user_id
    else:
        try:
            insert_result = users_collection().insert_one(new_user)
            session["user_id"] = str(insert_result.inserted_id)
        except PyMongoError:
            return jsonify({"success": False, "message": "Database error."}), 503

    session["user_name"] = name

    return jsonify({
        "success": True,
        "message": "Profile created successfully.",
        "user": {"name": name, "monthly_limit": monthly_limit, "current_spend": 0.0},
        "redirect_url": url_for("home"),
    })

    return render_template("profile.html")

# ==================== REAL-TIME SPEND HISTORY ====================

@app.route('/api/get_expenses', methods=['GET'])
def get_expenses():
    """Get all expenses for the logged-in user"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    user_name = session.get('user_name', '')
    expenses = []
    
    # Try to get from MongoDB first
    if is_mongo_available():
        try:
            cursor = daily_expenses_collection().find({
                'created_by': user_name
            }).sort('created_at', -1)
            
            for exp in cursor:
                expenses.append({
                    '_id': str(exp['_id']),
                    'category': exp.get('category', 'Other'),
                    'amount': exp.get('amount', 0),
                    'description': exp.get('description', ''),
                    'created_at': exp.get('created_at', datetime.utcnow().isoformat()),
                    'is_loan': exp.get('is_loan', False)
                })
        except PyMongoError as e:
            print(f"MongoDB error: {e}")
    
    # If no expenses in DB, load from local or create dummy data
    if not expenses:
        # Try local storage
        local_expenses = load_local_expenses()
        for exp in local_expenses:
            if exp.get('created_by') == user_name:
                expenses.append(exp)
        
        # If still no expenses, create dummy data
        if not expenses:
            expenses = get_dummy_expenses(user_name)
            # Save dummy data to local storage
            for exp in expenses:
                append_local_expense(exp)
    
    return jsonify(expenses)

@app.route('/api/add_expense_direct', methods=['POST'])
def add_expense_direct():
    """Add a new expense directly"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    data = request.get_json()
    
    category = data.get('category', 'Other')
    amount = float(data.get('amount', 0))
    description = data.get('description', '')
    
    if amount <= 0:
        return jsonify({'success': False, 'error': 'Amount must be greater than 0'}), 400
    
    expense_doc = {
        '_id': str(uuid4()),
        'category': category,
        'amount': amount,
        'description': description,
        'created_at': datetime.utcnow().isoformat(),
        'is_loan': False,
        'created_by': session.get('user_name', '')
    }
    
    # Save to MongoDB or local storage
    if is_mongo_available():
        try:
            daily_expenses_collection().insert_one(expense_doc)
        except PyMongoError as e:
            print(f"MongoDB error: {e}")
            append_local_expense(expense_doc)
    else:
        append_local_expense(expense_doc)
    
    # Update total spent
    increment_current_spend(amount)
    
    return jsonify({'success': True, 'message': 'Expense added successfully', 'expense': expense_doc})

@app.route('/api/delete_expense/<expense_id>', methods=['DELETE'])
def delete_expense(expense_id):
    """Delete an expense"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    user_name = session.get('user_name', '')
    deleted = False
    amount = 0
    
    # Try to delete from MongoDB
    if is_mongo_available():
        try:
            expense = daily_expenses_collection().find_one({'_id': ObjectId(expense_id), 'created_by': user_name})
            if expense:
                amount = expense.get('amount', 0)
                daily_expenses_collection().delete_one({'_id': ObjectId(expense_id)})
                deleted = True
        except:
            pass
    
    # If not found in MongoDB, try local storage
    if not deleted:
        expenses = load_local_expenses()
        for i, exp in enumerate(expenses):
            if exp.get('_id') == expense_id and exp.get('created_by') == user_name:
                amount = exp.get('amount', 0)
                expenses.pop(i)
                save_local_expenses(expenses)
                deleted = True
                break
    
    if deleted:
        # Decrease total spent
        increment_current_spend(-amount)
        return jsonify({'success': True, 'message': 'Expense deleted successfully'})
    
    return jsonify({'success': False, 'error': 'Expense not found'}), 404

# ==================== INTERVAL SPEND MANAGER ====================

@app.route('/api/add_recurring_payment', methods=['POST'])
def add_recurring_payment():
    """Add a recurring payment"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    data = request.get_json()
    
    if not data:
        return jsonify({'success': False, 'error': 'No data received'}), 400
    
    required = ['name', 'amount', 'due_date', 'remind_days_before']
    for field in required:
        if field not in data:
            return jsonify({'success': False, 'error': f'Missing field: {field}'}), 400
    
    payment = {
        '_id': str(uuid4()),
        'user_id': session['user_id'],
        'user_name': session['user_name'],
        'name': data['name'],
        'description': data.get('description', ''),
        'amount': float(data['amount']),
        'payment_type': data.get('payment_type', 'emi'),
        'due_date': data['due_date'],
        'remind_days_before': int(data['remind_days_before']),
        'is_active': True,
        'created_at': datetime.now().isoformat(),
        'payment_history': []
    }
    
    # Save to MongoDB or local storage
    if is_mongo_available():
        try:
            recurring_payments_collection().insert_one(payment)
        except PyMongoError:
            recurring_local = load_local_recurring()
            recurring_local.append(payment)
            save_local_recurring(recurring_local)
    else:
        recurring_local = load_local_recurring()
        recurring_local.append(payment)
        save_local_recurring(recurring_local)
    
    return jsonify({
        'success': True,
        'message': f'Recurring payment "{data["name"]}" added successfully',
        'payment_id': payment['_id']
    })

@app.route('/api/get_recurring_payments', methods=['GET'])
def get_recurring_payments():
    """Get all recurring payments for current user"""
    if not session.get('user_id'):
        return jsonify([])
    
    user_id = session['user_id']
    payments = []
    
    if is_mongo_available():
        try:
            cursor = recurring_payments_collection().find({
                'user_id': user_id,
                'is_active': True
            })
            for p in cursor:
                payments.append({
                    '_id': str(p['_id']),
                    'name': p.get('name', ''),
                    'description': p.get('description', ''),
                    'amount': p.get('amount', 0),
                    'payment_type': p.get('payment_type', 'emi'),
                    'due_date': p.get('due_date', ''),
                    'remind_days_before': p.get('remind_days_before', 3)
                })
        except PyMongoError:
            pass
    
    if not payments:
        recurring_local = load_local_recurring()
        for p in recurring_local:
            if p.get('user_id') == user_id and p.get('is_active', True):
                payments.append(p)
    
    return jsonify(payments)

@app.route('/api/delete_recurring_payment/<payment_id>', methods=['DELETE'])
def delete_recurring_payment(payment_id):
    """Delete a recurring payment"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    user_id = session['user_id']
    deleted = False
    
    if is_mongo_available():
        try:
            result = recurring_payments_collection().delete_one({'_id': ObjectId(payment_id), 'user_id': user_id})
            deleted = result.deleted_count > 0
        except:
            pass
    
    if not deleted:
        recurring_local = load_local_recurring()
        for i, p in enumerate(recurring_local):
            if p.get('_id') == payment_id and p.get('user_id') == user_id:
                recurring_local.pop(i)
                save_local_recurring(recurring_local)
                deleted = True
                break
    
    if deleted:
        return jsonify({'success': True, 'message': 'Payment deleted successfully'})
    
    return jsonify({'success': False, 'error': 'Payment not found'}), 404

@app.route('/api/mark_payment_paid/<payment_id>', methods=['POST'])
def mark_payment_paid(payment_id):
    """Mark a payment as paid"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    return jsonify({'success': True, 'message': 'Payment marked as paid'})

@app.route('/api/check_upcoming_payments', methods=['GET'])
def check_upcoming_payments():
    """Check for upcoming payments"""
    return jsonify({'upcoming': []})

# ==================== OTHER API ROUTES ====================

# ---------------- EXPENSE PAGE ---------------- #
@app.route('/my_expenses')
def my_expenses():
    exp = list(expenses.find({"user": session["user"]}))
    loan_data = list(loans.find({"user": session["user"]}))

    return render_template("expenses.html", expenses=exp, loans=loan_data)


# ---------------- ADD EXPENSE ---------------- #
@app.route('/add_expense', methods=['POST'])
def add_expense():
    try:
        payload = request.get_json(silent=True) or request.form.to_dict()
        category = (payload.get("category") or "").strip()
        amount_raw = payload.get("amount")
        
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Amount must be a valid number."}), 400
        
        now = datetime.utcnow()
        expense_doc = {
            "category": category,
            "amount": amount,
            "is_loan": False,
            "created_at": now.isoformat(),
            "created_by": session.get("user_name", "guest"),
        }
        
        if is_mongo_available():
            try:
                daily_expenses_collection().insert_one(expense_doc)
            except PyMongoError:
                append_local_expense(expense_doc)
        else:
            append_local_expense(expense_doc)
        
        increment_current_spend(amount)
        
        return jsonify({"success": True, "message": "Expense saved successfully."})
    except Exception as e:
        print(f"Expense Submit Error: {e}")
        return jsonify({"success": False, "message": "Failed to submit expense."}), 500

@app.route('/api/spend_data')
def spend_data():
    dummy_data = {
        "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food", "Hostel Rent", "Travelling"],
        "amounts": [1200, 500, 800, 300, 5000, 450]
    }
    return jsonify(dummy_data)

@app.route("/api/db_status")
def db_status():
    connected = is_mongo_available()
    return jsonify({
        "atlas_connected": connected,
        "mongo_uri_configured": bool(os.environ.get("MONGO_URI")),
        "fallback_file_present": os.path.exists(LOCAL_USERS_FILE),
    })

@app.route("/api/my_profile")
def api_my_profile():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Not logged in."}), 401
    return jsonify({"success": True, "user": {"name": session.get("user_name"), "monthly_limit": 10000, "current_spend": 0}})

@app.route('/add_friend_loan', methods=['POST'])
def add_friend_loan():
    return redirect(url_for('my_expenses'))

@app.route('/add_interval_spend', methods=['POST'])
def add_interval_spend():
    return redirect(url_for('interval_spend'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
    
