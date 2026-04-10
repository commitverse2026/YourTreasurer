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
from flask_mail import Mail
from flask_pymongo import PyMongo
from pymongo.errors import ConfigurationError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__, template_folder="Templates", static_folder="Static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "campuscoin_tracker_2026")

# --- CONFIGURATION ---

# 1. Cloudinary Setup (Participants will use this for receipt uploads)
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
try:
    mongo = PyMongo(app)
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
LOCAL_EXPENSES_FILE = os.path.join(app.root_path, "local_daily_expenses.json")
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
    """Function to send email in a background thread to prevent UI freezing."""
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent successfully!")
        except Exception as e:
            print(f"Background Mail Error: {e}")


def send_cycle_reset_email(user_name, user_email=None):
    """Send notification when 30-day cycle resets."""
    if not user_email:
        # If no email, perhaps skip or use a default
        return
    msg = Message(
        "YourTreasurer: Monthly Budget Cycle Reset",
        sender=app.config["MAIL_USERNAME"],
        recipients=[user_email],
    )
    msg.body = f"Hi {user_name},\n\nYour 30-day budget cycle has reset. Your current spend has been reset to 0, and you can set a new monthly limit.\n\nBest,\nYourTreasurer Team"
    send_async_email(app, msg)


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


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def increment_current_spend(amount):
    user_id = session.get("user_id")
    if not user_id:
        return

    if user_id.startswith("local:"):
        local_user_id = user_id.replace("local:", "", 1)
        user_doc = get_local_user_by_id(local_user_id)
        if not user_doc:
            return
        current = float(user_doc.get("current_spend", 0) or 0)
        user_doc["current_spend"] = current + amount
        upsert_local_user(user_doc)
        return

    if is_mongo_available():
        try:
            users_collection().update_one({"_id": ObjectId(user_id)}, {"$inc": {"current_spend": amount}})
        except (InvalidId, PyMongoError):
            return


def normalize_expense_doc(expense_doc):
    normalized = dict(expense_doc)
    if "_id" in normalized:
        normalized["_id"] = str(normalized["_id"])
    created_at = normalized.get("created_at")
    if isinstance(created_at, datetime):
        normalized["created_at"] = created_at.isoformat()
    elif created_at is not None:
        normalized["created_at"] = str(created_at)
    return normalized


def get_expense_history_for_user(user_name, limit=100):
    if not user_name:
        return [], "local"

    if is_mongo_available():
        try:
            cursor = (
                daily_expenses_collection()
                .find({"$or": [{"created_by": user_name}, {"username": user_name}]})
                .sort("created_at", -1)
                .limit(limit)
            )
            mongo_expenses = [normalize_expense_doc(exp) for exp in cursor]
            return mongo_expenses, "atlas"
        except PyMongoError:
            pass

    local_expenses = load_local_expenses()
    filtered = [
        exp for exp in local_expenses
        if exp.get("username") == user_name or exp.get("created_by") == user_name
    ]
    filtered.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return [normalize_expense_doc(exp) for exp in filtered[:limit]], "local"


def parse_start_date(raw_start_date):
    if isinstance(raw_start_date, datetime):
        return raw_start_date
    if isinstance(raw_start_date, str):
        try:
            return datetime.fromisoformat(raw_start_date)
        except ValueError:
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
        # Archive expenses before resetting
        try:
            expenses = list(daily_expenses_collection().find({"created_by": user_doc["name"], "created_at": {"$gte": start_date}}))
            if expenses:
                archived_collection = mongo.cx[app.config["MONGO_DBNAME"]]["archived_expenses"]
                for exp in expenses:
                    exp["archived_at"] = now
                    archived_collection.insert_one(exp)
                daily_expenses_collection().delete_many({"created_by": user_doc["name"], "created_at": {"$gte": start_date}})
        except PyMongoError:
            pass  # Ignore archiving errors

        users_collection().update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"current_spend": 0, "start_date": now, "monthly_limit": 0}},  # Reset monthly_limit to prompt user
        )
        user_doc["current_spend"] = 0
        user_doc["start_date"] = now
        user_doc["monthly_limit"] = 0
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

# --- GLOBAL CHECKS ---

@app.before_request
def check_budget_setup():
    return None

# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    # TODO: Fetch today's expenses to show a quick summary on the home dashboard
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
        return jsonify(
            {
                "success": False,
                "needs_signup": True,
                "message": "User not found. Please set your monthly budget to create profile.",
                "prefill_name": name,
            }
        ), 404

    if not check_password_hash(user_doc.get("password", ""), password):
        return jsonify({"success": False, "message": "Invalid credentials."}), 401

    if use_local_store:
        user_doc = maybe_reset_cycle_local(user_doc)
        session["user_id"] = f"local:{user_doc['_id']}"
    else:
        try:
            user_doc = maybe_reset_cycle(user_doc)
        except PyMongoError:
            return jsonify({"success": False, "message": "Database unavailable. Please try again shortly."}), 503
        session["user_id"] = str(user_doc["_id"])
    session["user_name"] = user_doc["name"]

    return jsonify(
        {
            "success": True,
            "message": "Login successful.",
            "user": build_user_payload(user_doc),
            "storage": "local" if use_local_store else "atlas",
            "redirect_url": url_for("home"),
        }
    )


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
        return jsonify({"success": False, "message": "User already exists. Please login."}), 409

    now = datetime.utcnow()
    try:
        new_user = {
            "name": name,
            "password": generate_password_hash(password),
            "monthly_limit": monthly_limit,
            "current_spend": 0.0,
            "start_date": now,
        }
        if use_local_store:
            local_user = {
                "_id": str(uuid4()),
                "name": name,
                "password": new_user["password"],
                "monthly_limit": monthly_limit,
                "current_spend": 0.0,
                "start_date": now.isoformat(),
            }
            upsert_local_user(local_user)
            session["user_id"] = f"local:{local_user['_id']}"
        else:
            insert_result = users_collection().insert_one(new_user)
            session["user_id"] = str(insert_result.inserted_id)
        session["user_name"] = name
    except PyMongoError:
        return jsonify({"success": False, "message": "Database unavailable. Please try again shortly."}), 503

    return jsonify(
        {
            "success": True,
            "message": "Profile created and login successful.",
            "user": {
                "name": name,
                "monthly_limit": monthly_limit,
                "current_spend": 0.0,
            },
            "storage": "local" if use_local_store else "atlas",
            "redirect_url": url_for("home"),
        }
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True, "message": "Logged out."})

@app.route('/my_expenses')
def my_expenses():
    return render_template('expenses.html')


@app.route('/expense_history')
def expense_history_page():
    return render_template('expense_history.html')

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
    """Handles adding a new daily expense / loan entry."""
    try:
        payload = request.get_json(silent=True) or request.form.to_dict()

        category = (payload.get("category") or "").strip()
        amount_raw = payload.get("amount")
        spent_at = (payload.get("spent_at") or "").strip()
        is_loan = parse_bool(payload.get("is_loan"))
        friend_email = (payload.get("friend_email") or "").strip()
        relationship = (payload.get("relationship") or "").strip()

        if not category:
            return jsonify({"success": False, "message": "Category is required."}), 400

        if not spent_at:
            return jsonify({"success": False, "message": "Please specify where the money was spent."}), 400

        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Amount must be a valid number."}), 400
        if amount <= 0:
            return jsonify({"success": False, "message": "Amount must be greater than 0."}), 400

        if is_loan and (not friend_email or not relationship):
            return jsonify(
                {
                    "success": False,
                    "message": "Friend email and relationship are required when 'Is it a Loan?' is selected.",
                }
            ), 400

        now = datetime.utcnow()
        expense_doc = {
            "category": category,
            "amount": amount,
            "spent_at": spent_at,
            "is_loan": is_loan,
            "friend_email": friend_email if is_loan else None,
            "relationship": relationship if is_loan else None,
            "created_at": now,
            "created_by": session.get("user_name", "guest"),
            "username": session.get("user_name", "guest"),
        }

        storage = "local"
        if is_mongo_available():
            try:
                insert_result = daily_expenses_collection().insert_one(expense_doc)
                expense_doc["_id"] = str(insert_result.inserted_id)
                storage = "atlas"
            except PyMongoError:
                local_doc = expense_doc.copy()
                local_doc["_id"] = str(uuid4())
                local_doc["created_at"] = now.isoformat()
                append_local_expense(local_doc)
        else:
            local_doc = expense_doc.copy()
            local_doc["_id"] = str(uuid4())
            local_doc["created_at"] = now.isoformat()
            append_local_expense(local_doc)

        increment_current_spend(amount)

        return jsonify(
            {
                "success": True,
                "message": "Expense saved successfully.",
                "storage": storage,
            }
        )
    except Exception as e:
        print(f"Expense Submit Error: {e}")
        return jsonify({"success": False, "message": "Failed to submit expense."}), 500

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
    """API endpoint to feed the Doughnut and Line charts in the Analysis tab."""
    # TODO Task 4: Query MongoDB, group expenses by Category (Hostel, Junk Food, etc.)
    # Return as JSON so JavaScript can draw the charts without reloading the page
    
    dummy_data = {
        "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food", "Hostel Rent", "Travelling"],
        "amounts": [1200, 500, 800, 300, 5000, 450]
    }
    return jsonify(dummy_data)


@app.route("/api/expense_history")
def expense_history():
    user_name = session.get("user_name") or "guest"
    expenses, storage = get_expense_history_for_user(user_name, limit=100)
    return jsonify(
        {
            "success": True,
            "user_name": user_name,
            "storage": storage,
            "count": len(expenses),
            "expenses": expenses,
        }
    )


@app.route("/api/db_status")
def db_status():
    connected = is_mongo_available()
    return jsonify(
        {
            "atlas_connected": connected,
            "mongo_uri_configured": bool(os.environ.get("MONGO_URI")),
            "fallback_file_present": os.path.exists(LOCAL_USERS_FILE),
            "last_check_utc": MONGO_LAST_CHECK.isoformat() if MONGO_LAST_CHECK else None,
            "last_error": None if connected else MONGO_LAST_ERROR,
        }
    )


@app.route("/api/my_profile")
@app.route("/api/profile")
def api_my_profile():
    if not session.get("user_id"):
        return jsonify({"success": False, "message": "Not logged in."}), 401

    user_doc = None
    if session["user_id"].startswith("local:"):
        local_user_id = session["user_id"].replace("local:", "", 1)
        user_doc = get_local_user_by_id(local_user_id)
        if user_doc:
            user_doc = maybe_reset_cycle_local(user_doc)
    else:
        if not is_mongo_available():
            return jsonify({"success": False, "message": "Database unavailable."}), 503
        try:
            user_doc = users_collection().find_one({"_id": ObjectId(session["user_id"])})
            if user_doc:
                user_doc = maybe_reset_cycle(user_doc)
        except (InvalidId, PyMongoError):
            return jsonify({"success": False, "message": "Profile fetch failed."}), 500

    if not user_doc:
        return jsonify({"success": False, "message": "User not found."}), 404

    return jsonify(
        {
            "success": True,
            "user": build_user_payload(user_doc),
        }
    )


@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)
