
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
from flask import Flask, jsonify, redirect, render_template, request, session, url_for, flash
from flask_mail import Mail, Message
from flask_pymongo import PyMongo
from pymongo.errors import ConfigurationError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash
import certifi

app = Flask(__name__, template_folder="Templates", static_folder="Static", static_url_path="/static")
app.secret_key = "campuscoin_tracker_2026"
load_dotenv()

# --- CONFIGURATION ---

# 1. Cloudinary Setup
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_NAME", "your_cloud_name"),
    api_key=os.environ.get("CLOUDINARY_KEY", "your_api_key"),
    api_secret=os.environ.get("CLOUDINARY_SECRET", "your_api_secret"),
)

# 2. MongoDB & Mail Setup
def build_mongo_uri_with_timeouts(raw_uri):
    split_result = urlsplit(raw_uri)
    query = dict(parse_qsl(split_result.query, keep_blank_values=True))
    timeout_defaults = {
        "serverSelectionTimeoutMS": "2500",
        "connectTimeoutMS": "2500",
        "socketTimeoutMS": "2500",
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

raw_mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/yourtreasurer")
app.config["MONGO_URI"] = build_mongo_uri_with_timeouts(raw_mongo_uri)
app.config["MONGO_DBNAME"] = os.environ.get("MONGO_DBNAME", "yourtreasurer")
MONGO_INIT_ERROR = ""

# --- THE ATLAS SHIELD (CRITICAL FIX) ---
try:
    # We force SSL/TLS certificate verification to use certifi and allow invalid certs for local handshakes
    mongo = PyMongo(app, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)
except Exception as error:
    mongo = None
    MONGO_INIT_ERROR = str(error)

app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USER", "your_email@gmail.com")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASS", "your_app_password")
mail = Mail(app)

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5MB limit for receipts
LOCAL_USERS_FILE = os.path.join(app.root_path, "local_users.json")
MONGO_AVAILABLE = None
MONGO_LAST_CHECK = None
MONGO_LAST_ERROR = ""
PASSWORD_RULES_TEXT = (
    "Password must be at least 8 characters and include uppercase, lowercase, "
    "number, and special character."
)
NAME_RULES_TEXT = "Name must contain only letters and spaces."

# --- ASYNC BACKGROUND TASKS ---

def send_async_email(app, msg):
    """Function to send email in a background thread."""
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            print(f"Background Mail Error: {e}")

# --- DB HELPERS ---

def users_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    return mongo.db.users

def daily_expenses_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    return mongo.db.daily_expenses

def recurring_payments_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    return mongo.db.recurring_payments

# --- UTILS ---

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

def is_mongo_available():
    global MONGO_AVAILABLE, MONGO_LAST_CHECK, MONGO_LAST_ERROR
    if mongo is None:
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

def load_local_users():
    if not os.path.exists(LOCAL_USERS_FILE):
        return []
    try:
        with open(LOCAL_USERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except:
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

def parse_start_date(raw_start_date):
    if isinstance(raw_start_date, datetime):
        return raw_start_date
    if isinstance(raw_start_date, str):
        try:
            return datetime.fromisoformat(raw_start_date)
        except:
            return None
    return None

def maybe_reset_cycle(user_doc):
    start_date = parse_start_date(user_doc.get("start_date"))
    now = datetime.utcnow()
    if not start_date:
        users_collection().update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"start_date": now, "current_spend": user_doc.get("current_spend", 0)}},
        )
        user_doc["start_date"] = now
        return user_doc

    if now > start_date + timedelta(days=30):
        users_collection().update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"current_spend": 0, "start_date": now}},
        )
        user_doc["current_spend"] = 0
        user_doc["start_date"] = now
    return user_doc

def maybe_reset_cycle_local(user_doc):
    start_date = parse_start_date(user_doc.get("start_date"))
    now = datetime.utcnow()
    if not start_date:
        user_doc["start_date"] = now.isoformat()
        user_doc["current_spend"] = float(user_doc.get("current_spend", 0) or 0)
        upsert_local_user(user_doc)
        return user_doc

    if now > start_date + timedelta(days=30):
        user_doc["current_spend"] = 0.0
        user_doc["start_date"] = now.isoformat()
        upsert_local_user(user_doc)
    return user_doc

def build_user_payload(user_doc):
    return {
        "name": user_doc.get("name"),
        "monthly_limit": float(user_doc.get("monthly_limit", 0) or 0),
        "current_spend": float(user_doc.get("current_spend", 0) or 0),
    }

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/my_profile')
@app.route('/profile')
def my_profile():
    return render_template("profile.html")

@app.route("/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""

    if not name or not password:
        return jsonify({"success": False, "message": "Name and password are required."}), 400

    use_local_store = not is_mongo_available()
    if use_local_store:
        user_doc = get_local_user_by_name(name)
    else:
        try:
            user_doc = users_collection().find_one({"name": name})
        except:
            user_doc = get_local_user_by_name(name)
            use_local_store = True

    if not user_doc:
        return jsonify({"success": False, "needs_signup": True, "message": "User not found. Please sign up.", "prefill_name": name}), 404

    if not check_password_hash(user_doc.get("password", ""), password):
        return jsonify({"success": False, "message": "Invalid credentials."}), 401

    if use_local_store:
        user_doc = maybe_reset_cycle_local(user_doc)
        session["user_id"] = f"local:{user_doc['_id']}"
    else:
        user_doc = maybe_reset_cycle(user_doc)
        session["user_id"] = str(user_doc["_id"])
    session["user_name"] = user_doc["name"]

    return jsonify({"success": True, "message": "Login successful.", "user": build_user_payload(user_doc), "redirect_url": url_for("home")})

@app.route("/signup", methods=["POST"])
def signup():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""
    monthly_limit_input = payload.get("monthly_limit")

    if not is_name_valid(name):
        return jsonify({"success": False, "message": NAME_RULES_TEXT}), 400
    if not is_password_valid(password):
        return jsonify({"success": False, "message": PASSWORD_RULES_TEXT}), 400

    try:
        monthly_limit = float(monthly_limit_input) if monthly_limit_input else 0.0
    except:
        return jsonify({"success": False, "message": "Invalid limit."}), 400

    use_local_store = not is_mongo_available()
    if use_local_store:
        existing = get_local_user_by_name(name)
    else:
        existing = users_collection().find_one({"name": name})
    
    if existing:
        return jsonify({"success": False, "message": "User already exists."}), 409

    now = datetime.utcnow()
    hashed = generate_password_hash(password)
    user_data = {"name": name, "password": hashed, "monthly_limit": monthly_limit, "current_spend": 0.0, "start_date": now}

    if use_local_store:
        user_data["_id"] = str(uuid4())
        user_data["start_date"] = now.isoformat()
        upsert_local_user(user_data)
        session["user_id"] = f"local:{user_data['_id']}"
    else:
        res = users_collection().insert_one(user_data)
        session["user_id"] = str(res.inserted_id)
    
    session["user_name"] = name
    return jsonify({"success": True, "message": "Signup successful.", "user": build_user_payload(user_data), "redirect_url": url_for("home")})

from flask import Flask, render_template, request, redirect, session, jsonify
from flask_pymongo import PyMongo
from datetime import datetime
from bson.objectid import ObjectId

app = Flask(__name__)
app.secret_key = "secret123"

# MongoDB
app.config["MONGO_URI"] = "mongodb+srv://onkarghadage1107_db_user:cPfYrgcDoaCdkOlz@cluster0.gs7ggdy.mongodb.net/yourtreasurer"
mongo = PyMongo(app)

users = mongo.db.users
expenses = mongo.db.expenses
loans = mongo.db.loans


# ---------------- LOGIN ---------------- #
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        name = request.form['name']
        password = request.form['password']
        limit = request.form.get('monthly_limit', 0)

        user = users.find_one({"name": name})

        if not user:
            users.insert_one({
                "name": name,
                "password": password,
                "monthly_limit": int(limit),
                "current_spend": 0
            })
            session['user'] = name
            return redirect('/')

        if user['password'] == password:
            session['user'] = name
            return redirect('/')


    return render_template("profile.html")


# ---------------- HOME ---------------- #
@app.route('/')
def home():
    if "user" not in session:
        return redirect('/login')
    return render_template("index.html", user=session.get("user"))


# ---------------- PROGRESS API ---------------- #
@app.route('/api/progress')
def progress():
    user = users.find_one({"name": session["user"]})

    spent = user.get("current_spend",0)
    limit = user.get("monthly_limit",1)

    percent = (spent/limit)*100

    return jsonify({
        "progress": percent,
        "spent": spent,
        "limit": limit
    })


# ---------------- EXPENSE PAGE ---------------- #
@app.route('/my_expenses')
def my_expenses():

    if not session.get("user_id"):
        return redirect(url_for('my_profile'))

    user_name = session.get("user_name")
    expenses = []

    if is_mongo_available():
        try:
            expenses = list(daily_expenses_collection().find({"created_by": user_name}).sort("created_at", -1))
            if not expenses:
                # Seed dummy (Task 3: Minimum 8 dummy expenses across categories)
                dummy = [
                    {"category": "Junk Food", "amount": 150.0, "spent_at": "Local Cafe", "is_loan": False, "created_at": datetime.utcnow() - timedelta(hours=2), "created_by": user_name},
                    {"category": "Educational", "amount": 800.0, "spent_at": "Bookstore", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=1), "created_by": user_name},
                    {"category": "Travel", "amount": 250.0, "spent_at": "Metro Station", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=1, hours=5), "created_by": user_name},
                    {"category": "Hostel Rent", "amount": 5000.0, "spent_at": "Hostel Office", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=2), "created_by": user_name},
                    {"category": "Lifestyle", "amount": 1200.0, "spent_at": "Shopping Mall", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=3), "created_by": user_name},
                    {"category": "Healthy Food", "amount": 300.0, "spent_at": "Fruit Stall", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=4), "created_by": user_name},
                    {"category": "Other", "amount": 100.0, "spent_at": "Stationery", "is_loan": True, "friend_email": "friend1@example.com", "friend_relationship": "Classmate", "created_at": datetime.utcnow() - timedelta(days=5), "created_by": user_name},
                    {"category": "Junk Food", "amount": 200.0, "spent_at": "Snack Bar", "is_loan": True, "friend_email": "friend2@example.com", "friend_relationship": "Roommate", "created_at": datetime.utcnow() - timedelta(days=6), "created_by": user_name}
                ]
                daily_expenses_collection().insert_many(dummy)
                expenses = list(daily_expenses_collection().find({"created_by": user_name}).sort("created_at", -1))
            for exp in expenses:
                exp["_id"] = str(exp["_id"])
        except:
            expenses = []
    
    return render_template('expenses.html', expenses=expenses)

@app.route('/analysis')
def analysis():
    return render_template('analysis.html')

@app.route('/interval_spend')
def interval_spend():
    if not session.get("user_id"):
        return redirect(url_for('my_profile'))
    
    user_name = session.get("user_name")
    recurrings = []
    
    if is_mongo_available():
        try:
            recurrings = list(recurring_payments_collection().find({"created_by": user_name}).sort("due_day", 1))
            for item in recurrings:
                item["_id"] = str(item["_id"])
        except:
            pass
            
    return render_template('interval_spend.html', recurrings=recurrings)

@app.route('/add_recurring', methods=['POST'])
def add_recurring():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    
    user_name = session.get("user_name")
    payload = request.form
    name = payload.get("item_name")
    amount = payload.get("amount")
    due_day = payload.get("due_day")
    reminder_days = payload.get("reminder_days")
    
    try:
        data = {
            "item_name": name,
            "amount": float(amount),
            "due_day": int(due_day),
            "reminder_days": int(reminder_days),
            "created_by": user_name,
            "created_at": datetime.utcnow()
        }
        
        if is_mongo_available():
            recurring_payments_collection().insert_one(data)
        
        flash("Recurring expense added successfully!", "success")
        return redirect(url_for('interval_spend'))
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
        return redirect(url_for('interval_spend'))

@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

@app.route('/api/spend_data')
def get_spend_data():
    if not session.get("user_name"):
        return jsonify({"success": False}), 401
    
    # Return dummy data or fetch from DB
    return jsonify({
        "success": True,
        "categories": ["Food", "Transport", "Rent"],
        "amounts": [1200, 800, 5000]
    })

    exp = list(expenses.find({"user": session["user"]}))
    loan_data = list(loans.find({"user": session["user"]}))

    return render_template("expenses.html", expenses=exp, loans=loan_data)


# ---------------- ADD EXPENSE ---------------- #
@app.route('/add_expense', methods=['POST'])
def add_expense():
    amount = int(request.form['amount'])

    expenses.insert_one({
        "user": session["user"],
        "amount": amount,
        "category": request.form['category'],
        "date": datetime.now()
    })

    users.update_one(
        {"name": session["user"]},
        {"$inc": {"current_spend": amount}}
    )

    return redirect('/my_expenses')


# ---------------- ADD LOAN ---------------- #
@app.route('/add_loan', methods=['POST'])
def add_loan():
    loans.insert_one({
        "user": session["user"],
        "friend_name": request.form['friend_name'],
        "amount": int(request.form['amount']),
        "status": "pending"
    })
    return redirect('/my_expenses')


# ---------------- MARK RETURN ---------------- #
@app.route('/mark_returned/<loan_id>', methods=['POST'])
def mark_returned(loan_id):

    loan = loans.find_one({"_id": ObjectId(loan_id)})

    loans.update_one(
        {"_id": ObjectId(loan_id)},
        {"$set": {"status":"returned"}}
    )

    amount = loan['amount']

    expenses.insert_one({
        "user": session["user"],
        "amount": -amount,
        "category": "Loan Return",
        "date": datetime.now()
    })

    users.update_one(
        {"name": session["user"]},
        {"$inc": {"current_spend": -amount}}
    )

    return redirect('/my_expenses')


# ---------------- LOGOUT ---------------- #
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')



if __name__ == "__main__":
    app.run(debug=True)