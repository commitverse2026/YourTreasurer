
from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
import os
from dotenv import load_dotenv
import uuid 
import threading
import time
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta
from bson.objectid import ObjectId
import certifi

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


app = Flask(__name__, template_folder="Templates", static_folder="Static", static_url_path="/static")
app.secret_key = "campuscoin_tracker_2026"
load_dotenv()

# --- CONFIGURATION ---

# 1. Cloudinary Setup (Participants will use this for receipt uploads)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_NAME", "your_cloud_name"),
    api_key=os.environ.get("CLOUDINARY_KEY", "your_api_key"),
    api_secret=os.environ.get("CLOUDINARY_SECRET", "your_api_secret"),
)

# 2. MongoDB & Mail Setup

app.config["MONGO_URI"] = os.environ.get("MONGO_URI", "mongodb+srv://priteepardeshi3011_db_user:o1UpyYozHv4zvlTn@cluster0.a5drjzn.mongodb.net/yourtreasurer?retryWrites=true&w=majority")
mongo = PyMongo(app, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", 'your_app_password') 

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


def users_collection():
    if mongo is None:
        raise ConfigurationError(MONGO_INIT_ERROR or "MongoDB client is not initialized.")
    db_name = app.config["MONGO_DBNAME"]
    return mongo.cx[db_name]["users"]


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

    """
    Task 1: Check if the user has set up their initial monthly budget.
    If they haven't (and they aren't on static/profile pages), redirect them to MyProfile.
    """
    allowed_endpoints = ['my_profile', 'static']
    if request.endpoint and request.endpoint not in allowed_endpoints:
        if 'username' not in session or 'password' not in session:
            return redirect(url_for('my_profile'))
            
        user = mongo.db.users.find_one({
            "username": session['username'],
            "password": session['password']
        })
        
        if not user:
            session.clear()
            return redirect(url_for('my_profile'))
            
        # 30-Day Temporal Reset Logic
        start_date = user.get('start_date')
        if start_date:
            days_passed = (datetime.now() - start_date).days
            if days_passed >= 30:
                # Archive expenses
                mongo.db.daily_expenses.update_many(
                    {"username": user['username'], "archived": {"$ne": True}},
                    {"$set": {"archived": True}}
                )
                # Reset budget start date and unset monthly limit to force a new one
                mongo.db.users.update_one(
                    {"_id": user['_id']},
                    {"$set": {"start_date": datetime.now()}, "$unset": {"monthly_limit": ""}}
                )
                flash("Your 30-day budget cycle has reset! Please set a new Monthly Limit.", "error")
                return redirect(url_for('my_profile'))
                
        if not user.get('monthly_limit'):
            flash("Please set your Monthly Limit for this 30-Day cycle.", "error")
            return redirect(url_for('my_profile'))

    return None

# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    # TODO: Fetch today's expenses to show a quick summary on the home dashboard
    return render_template('index.html')


@app.route('/my_profile', methods=['GET', 'POST'])
def my_profile():
    if request.method == 'POST':
        # Handle 30-day Temporal Reset limit update
        if 'update_limit' in request.form and 'username' in session:
            new_limit = request.form.get('update_limit')
            if new_limit:
                mongo.db.users.update_one(
                    {"username": session['username']}, 
                    {"$set": {"monthly_limit": float(new_limit)}}
                )
                flash("New Monthly Limit locked in! Your next 30-day cycle has begun.", "success")
                return redirect(url_for('home'))
                
        username = request.form.get('username')
        password = request.form.get('password')
        monthly_limit = request.form.get('monthly_limit')
        
        if not username or not password:
            flash("Name and Password are required!")
            return redirect(url_for('my_profile'))
            
        user = mongo.db.users.find_one({"username": username})
        
        if user:
            # Login
            if user['password'] == password:
                session['username'] = username
                session['password'] = password
                flash("Login successful! Welcome back.", "success")
                return redirect(url_for('home'))
            else:
                flash("Invalid credentials.", "error")
                return redirect(url_for('my_profile'))
        else:
            # New user setup
            if not monthly_limit:
                flash("Please set a monthly limit for a new account.", "error")
                return redirect(url_for('my_profile'))
                
            mongo.db.users.insert_one({
                "username": username,
                "password": password,
                "monthly_limit": float(monthly_limit),
                "start_date": datetime.now(),
                "created_at": datetime.now()
            })
            session['username'] = username
            session['password'] = password
            flash("Account created! Let's manage your budget.", "success")
            return redirect(url_for('home'))
            
    # GET request
    user_data = None
    if 'username' in session and 'password' in session:
        user_data = mongo.db.users.find_one({"username": session['username'], "password": session['password']})

    return render_template('profile.html', user=user_data)

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for('my_profile'))

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

    if 'username' not in session:
        return redirect(url_for('my_profile'))
        
    username = session['username']
    expenses_cursor = mongo.db.daily_expenses.find({"username": username, "archived": {"$ne": True}}).sort("date", -1)
    expenses = list(expenses_cursor)
    
    if len(expenses) == 0:
        # Insert 8 dummy expenses to prove functionality
        dummy_data = [
            {"username": username, "category": "Junk Food", "amount": 150, "is_loan": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Educational", "amount": 800, "is_loan": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Travel", "amount": 250, "is_loan": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Hostel Rent", "amount": 5000, "is_loan": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Lifestyle", "amount": 1500, "is_loan": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Healthy Food", "amount": 300, "is_loan": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Other", "amount": 100, "is_loan": True, "friend_email": "friend1@example.com", "friend_relationship": "classmate", "returned": False, "date": datetime.now(), "archived": False},
            {"username": username, "category": "Junk Food", "amount": 200, "is_loan": True, "friend_email": "friend2@example.com", "friend_relationship": "roommate", "returned": False, "date": datetime.now(), "archived": False}
        ]
        mongo.db.daily_expenses.insert_many(dummy_data)
        # Fetch again to get the inserted ObjectIds
        expenses_cursor = mongo.db.daily_expenses.find({"username": username, "archived": {"$ne": True}}).sort("date", -1)
        expenses = list(expenses_cursor)
        
    return render_template('expenses.html', expenses=expenses)

@app.route('/delete_expense/<id>', methods=['POST'])
def delete_expense(id):
    try:
        mongo.db.daily_expenses.delete_one({"_id": ObjectId(id), "username": session.get('username')})
        flash('Expense deleted.', 'success_delete')
    except Exception as e:
        flash(f'Error deleting expense: {e}', 'error')
    return redirect(url_for('my_expenses'))

@app.route('/remind_friend/<id>', methods=['POST'])
def remind_friend(id):
    expense = mongo.db.daily_expenses.find_one({"_id": ObjectId(id)})
    if expense and expense.get('is_loan'):
        friend_email = expense.get('friend_email')
        subject = f"Gentle Reminder from {session['username']}"
        msg_body = f"Hello. This is a gentle reminder that you still owe ₹{expense.get('amount')} for '{expense.get('category')}'. Please process the return soon."
        msg = Message(subject, sender=app.config['MAIL_USERNAME'], recipients=[friend_email])
        msg.body = msg_body
        threading.Thread(target=send_async_email, args=(app._get_current_object(), msg)).start()
        flash('Reminder sent to friend!', 'success_remind')
    return redirect(url_for('my_expenses'))

@app.route('/return_loan/<id>', methods=['POST'])
def return_loan(id):
    expense = mongo.db.daily_expenses.find_one({"_id": ObjectId(id)})
    if expense and expense.get('is_loan') and not expense.get('returned'):
        # Update loan status
        mongo.db.daily_expenses.update_one({"_id": ObjectId(id)}, {"$set": {"returned": True}})
        
        # Add negative entry
        mongo.db.daily_expenses.insert_one({
            "username": session['username'],
            "category": "Loan Return",
            "amount": -expense.get('amount'),
            "is_loan": False,
            "date": datetime.now(),
            "archived": False
        })
        
        flash('Loan marked as returned!', 'success_returned')
    return redirect(url_for('my_expenses'))

    if not session.get("user_id"):
        return redirect(url_for('home'))

    user_name = session.get("user_name")
    expenses = []

    if is_mongo_available():
        try:
            expenses = list(daily_expenses_collection().find({"created_by": user_name}).sort("created_at", -1))
            if not expenses:
                # Seed dummy expenses
                dummy_expenses = [
                    {"category": "Junk Food", "amount": 150.0, "spent_at": "Local Cafe", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=1), "created_by": user_name},
                    {"category": "Educational", "amount": 500.0, "spent_at": "Bookstore", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=2), "created_by": user_name},
                    {"category": "Healthy Food", "amount": 200.0, "spent_at": "Grocery Store", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=3), "created_by": user_name},
                    {"category": "Travelling", "amount": 300.0, "spent_at": "Bus Station", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=4), "created_by": user_name},
                    {"category": "Lifestyle", "amount": 250.0, "spent_at": "Clothing Shop", "is_loan": True, "friend_email": "friend@example.com", "relationship": "Classmate", "created_at": datetime.utcnow() - timedelta(days=5), "created_by": user_name},
                    {"category": "Hostel Rent", "amount": 2000.0, "spent_at": "Hostel Office", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=6), "created_by": user_name},
                    {"category": "Junk Food", "amount": 100.0, "spent_at": "Fast Food Joint", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=7), "created_by": user_name},
                    {"category": "Other", "amount": 50.0, "spent_at": "Miscellaneous", "is_loan": False, "created_at": datetime.utcnow() - timedelta(days=8), "created_by": user_name},
                ]
                daily_expenses_collection().insert_many(dummy_expenses)
                expenses = list(daily_expenses_collection().find({"created_by": user_name}).sort("created_at", -1))
            for exp in expenses:
                exp["_id"] = str(exp["_id"])
                exp["created_at"] = exp["created_at"].isoformat() if isinstance(exp["created_at"], datetime) else exp["created_at"]
        except PyMongoError:
            expenses = []
    else:
        # Fallback to local
        local_expenses = load_local_expenses()
        expenses = [exp for exp in local_expenses if exp.get("created_by") == user_name]
        if not expenses:
            # Seed dummy for local
            dummy_expenses = [
                {"_id": str(uuid4()), "category": "Junk Food", "amount": 150.0, "spent_at": "Local Cafe", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=1)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Educational", "amount": 500.0, "spent_at": "Bookstore", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=2)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Healthy Food", "amount": 200.0, "spent_at": "Grocery Store", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=3)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Travelling", "amount": 300.0, "spent_at": "Bus Station", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=4)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Lifestyle", "amount": 250.0, "spent_at": "Clothing Shop", "is_loan": True, "friend_email": "friend@example.com", "relationship": "Classmate", "created_at": (datetime.utcnow() - timedelta(days=5)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Hostel Rent", "amount": 2000.0, "spent_at": "Hostel Office", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=6)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Junk Food", "amount": 100.0, "spent_at": "Fast Food Joint", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=7)).isoformat(), "created_by": user_name},
                {"_id": str(uuid4()), "category": "Other", "amount": 50.0, "spent_at": "Miscellaneous", "is_loan": False, "created_at": (datetime.utcnow() - timedelta(days=8)).isoformat(), "created_by": user_name},
            ]
            local_expenses.extend(dummy_expenses)
            save_local_expenses(local_expenses)
            expenses = [exp for exp in local_expenses if exp.get("created_by") == user_name]
        expenses.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return render_template('expenses.html', expenses=expenses)


@app.route('/analysis')
def analysis():
    return render_template('analysis.html')

@app.route('/interval_spend')
def interval_spend():
    if 'username' not in session:
        return redirect(url_for('my_profile'))
        
    dues = list(mongo.db.recurring_payments.find({"username": session['username']}).sort("due_date", 1))
    
    # Task 12: Smart Reminder Automation
    for due in dues:
        days_left = (due['due_date'] - datetime.now()).days
        if 0 <= days_left <= due.get('remind_days', 3):
            if not due.get('reminder_sent'):
                subject = "Reminder: Upcoming Payment"
                msg_body = f"Your payment of ₹{due['amount']} for {due['category']} is due in {days_left} days!"
                msg = Message(subject, sender=app.config['MAIL_USERNAME'], recipients=[app.config['MAIL_USERNAME']])
                msg.body = msg_body
                threading.Thread(target=send_async_email, args=(app._get_current_object(), msg)).start()
                mongo.db.recurring_payments.update_one({"_id": due['_id']}, {"$set": {"reminder_sent": True}})
        
        due['days_left'] = days_left

    return render_template('interval_spend.html', dues=dues)

@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

# --- DATA SUBMISSION ROUTES (THE LOGIC) ---

@app.route('/get_progress')
def get_progress():
    if 'username' not in session:
        return jsonify({"success": False})
    
    user = mongo.db.users.find_one({"username": session['username']})
    if not user:
        return jsonify({"success": False})
        
    pipeline = [
        {"$match": {"username": session['username'], "archived": {"$ne": True}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result = list(mongo.db.daily_expenses.aggregate(pipeline))
    total_spent = result[0]['total'] if result else 0
    
    return jsonify({
        "success": True, 
        "total_spent": total_spent, 
        "monthly_limit": user.get('monthly_limit', 0)
    })

@app.route('/add_expense', methods=['POST'])
def add_expense():
    """Handles adding a new daily expense."""
    try:
        category = request.form.get('category')
        amount = float(request.form.get('amount', 0))
        is_loan = request.form.get('is_loan') == 'yes'
        friend_email = request.form.get('friend_email')
        friend_relationship = request.form.get('friend_relationship')
        receipt = request.files.get('receipt_image')
        
        secure_url = None
        if receipt and receipt.filename != '':
            upload_result = cloudinary.uploader.upload(receipt)
            secure_url = upload_result.get('secure_url')
            
        expense_doc = {
            "username": session['username'],
            "category": category,
            "amount": amount,
            "is_loan": is_loan,
            "date": datetime.now(),
            "archived": False,
            "secure_url": secure_url
        }
        
        if is_loan:
            expense_doc.update({
                "friend_email": friend_email,
                "friend_relationship": friend_relationship,
                "returned": False
            })
            
            # Send email to friend
            subject = f"Loan Notification from {session['username']}"
            msg_body = f"Hello! This is a secure ledger record that {session['username']} lent you ₹{amount} for '{category}'. Please ensure you pay them back on time."
            msg = Message(subject, sender=app.config['MAIL_USERNAME'], recipients=[friend_email])
            msg.body = msg_body
            threading.Thread(target=send_async_email, args=(app._get_current_object(), msg)).start()

        mongo.db.daily_expenses.insert_one(expense_doc)
        
        # Calculate new total to trigger guardian emails
        pipeline = [
            {"$match": {"username": session['username'], "archived": {"$ne": True}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]
        result = list(mongo.db.daily_expenses.aggregate(pipeline))
        total_spent = result[0]['total'] if result else 0
        
        user = mongo.db.users.find_one({"username": session['username']})
        limit = user.get('monthly_limit', 0)
        
        if limit > 0:
            usage = total_spent / limit
            msg = None
            if usage >= 1.0:
                 # Over budget mail for EVERY expense
                 msg = Message("Over Budget Alert!", sender=app.config['MAIL_USERNAME'], recipients=[app.config['MAIL_USERNAME']])
                 msg.body = f"Stop! You are over budget. Total spent: ₹{total_spent} out of your ₹{limit} limit."
            elif usage >= 0.95 and not user.get('warning_95_sent'):
                 msg = Message("Critical Warning: 5% left!", sender=app.config['MAIL_USERNAME'], recipients=[app.config['MAIL_USERNAME']])
                 msg.body = f"You only have 5% of your budget left (Total spent: ₹{total_spent}/₹{limit})."
                 mongo.db.users.update_one({"_id": user["_id"]}, {"$set": {"warning_95_sent": True}})
            elif usage >= 0.90 and not user.get('warning_90_sent'):
                 msg = Message("Caution: 10% left!", sender=app.config['MAIL_USERNAME'], recipients=[app.config['MAIL_USERNAME']])
                 msg.body = f"You only have 10% of your budget left (Total spent: ₹{total_spent}/₹{limit})."
                 mongo.db.users.update_one({"_id": user["_id"]}, {"$set": {"warning_90_sent": True}})
                 
            if msg:
                threading.Thread(target=send_async_email, args=(app._get_current_object(), msg)).start()
                
        flash('Expense tracked successfully!', 'success')
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
        category = request.form.get('category')
        amount = float(request.form.get('amount', 0))
        due_date_str = request.form.get('due_date')
        remind_days = int(request.form.get('remind_days', 3))
        
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
        except:
            due_date = datetime.now()
            
        mongo.db.recurring_payments.insert_one({
            "username": session['username'],
            "category": category,
            "amount": amount,
            "due_date": due_date,
            "remind_days": remind_days,
            "reminder_sent": False
        })
        flash('Recurring payment added!', 'success')
        return redirect(url_for('interval_spend'))
    except Exception as e:
        return "Internal Error", 500

# --- API ROUTES (FOR REAL-TIME CHARTS) ---

@app.route('/api/spend_data')
def spend_data():
    """API endpoint to feed the Doughnut and Line charts in the Analysis tab."""
    view_type = request.args.get('view', 'month')
    username = session.get('username')
    if not username:
        return jsonify({"categories": [], "amounts": []})

    if view_type == 'day':
        start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_date = datetime.now() - timedelta(days=30)
        
    pipeline = [
        {"$match": {
            "username": username,
            "archived": {"$ne": True},
            "date": {"$gte": start_date}
        }},
        {"$group": {
            "_id": "$category",
            "total": {"$sum": "$amount"}
        }}
    ]
    
    result = list(mongo.db.daily_expenses.aggregate(pipeline))
    categories = []
    amounts = []
    for r in result:
        categories.append(r['_id'])
        amounts.append(r['total'])
        
    return jsonify({"categories": categories, "amounts": amounts})


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
