import os
import json
from datetime import datetime, timedelta
from uuid import uuid4

import cloudinary
import cloudinary.uploader
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_mail import Mail
from flask_pymongo import PyMongo
from pymongo.errors import PyMongoError
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
app.config["MONGO_URI"] = os.environ.get(
    "MONGO_URI",
    "mongodb://localhost:27017/yourtreasurer?serverSelectionTimeoutMS=1200&connectTimeoutMS=1200&socketTimeoutMS=1200",
)
mongo = PyMongo(app)

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

# --- ASYNC BACKGROUND TASKS ---

def send_async_email(app, msg):
    """Function to send email in a background thread to prevent UI freezing."""
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent successfully!")
        except Exception as e:
            print(f"Background Mail Error: {e}")


def users_collection():
    return mongo.db.users


def is_mongo_available():
    global MONGO_AVAILABLE, MONGO_LAST_CHECK
    now = datetime.utcnow()
    if MONGO_LAST_CHECK and (now - MONGO_LAST_CHECK).total_seconds() < 30:
        return bool(MONGO_AVAILABLE)
    try:
        mongo.cx.admin.command("ping")
        MONGO_AVAILABLE = True
    except PyMongoError:
        MONGO_AVAILABLE = False
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
def my_profile():
    user_data = None
    if session.get("user_id"):
        if session["user_id"].startswith("local:"):
            local_user_id = session["user_id"].replace("local:", "", 1)
            user_doc = get_local_user_by_id(local_user_id)
            if user_doc:
                user_doc = maybe_reset_cycle_local(user_doc)
                user_data = build_user_payload(user_doc)
        elif is_mongo_available():
            try:
                user_doc = users_collection().find_one({"_id": ObjectId(session["user_id"])})
                if user_doc:
                    user_doc = maybe_reset_cycle(user_doc)
                    user_data = build_user_payload(user_doc)
            except (InvalidId, PyMongoError):
                user_data = None
    return render_template("profile.html", user=user_data)


@app.route("/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""
    monthly_limit_input = payload.get("monthly_limit")

    if not name or not password:
        return jsonify({"success": False, "message": "Name and password are required."}), 400

    use_local_store = not is_mongo_available()
    if use_local_store:
        user_doc = get_local_user_by_name(name)
    else:
        try:
            user_doc = users_collection().find_one({"name": name})
        except PyMongoError:
            user_doc = get_local_user_by_name(name)
            use_local_store = True
    now = datetime.utcnow()

    if user_doc:
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
                "redirect_url": url_for("home"),
            }
        )

    try:
        monthly_limit = float(monthly_limit_input) if monthly_limit_input not in (None, "") else 0.0
    except ValueError:
        return jsonify({"success": False, "message": "Monthly limit must be a number."}), 400

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
        try:
            insert_result = users_collection().insert_one(new_user)
        except PyMongoError:
            return jsonify({"success": False, "message": "Database unavailable. Please try again shortly."}), 503
        session["user_id"] = str(insert_result.inserted_id)
    session["user_name"] = name

    return jsonify(
        {
            "success": True,
            "message": "Profile created and login successful.",
            "user": {
                "name": name,
                "monthly_limit": monthly_limit,
                "current_spend": 0.0,
            },
            "redirect_url": url_for("home"),
        }
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True, "message": "Logged out."})

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
    """API endpoint to feed the Doughnut and Line charts in the Analysis tab."""
    # TODO Task 4: Query MongoDB, group expenses by Category (Hostel, Junk Food, etc.)
    # Return as JSON so JavaScript can draw the charts without reloading the page
    
    dummy_data = {
        "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food", "Hostel Rent", "Travelling"],
        "amounts": [1200, 500, 800, 300, 5000, 450]
    }
    return jsonify(dummy_data)


@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)
