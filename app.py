from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
from bson import ObjectId
import os
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import cloudinary
import cloudinary.uploader
from datetime import datetime

app = Flask(__name__, static_folder='Static', template_folder='Templates')
app.secret_key = "campuscoin_tracker_2026"

# --- CONFIGURATION ---

# 1. Cloudinary Setup (Participants will use this for receipt uploads)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_NAME", "your_cloud_name"),
    api_key=os.environ.get("CLOUDINARY_KEY", "your_api_key"),
    api_secret=os.environ.get("CLOUDINARY_SECRET", "your_api_secret"),
)

# 2. MongoDB & Mail Setup
# TODO for Participants: Insert your free MongoDB Atlas URI here
app.config["MONGO_URI"] = "mongodb+srv://dummyworld36_db_user:39zxLuQg1hIELacU@cluster0.6rfqi8c.mongodb.net/yourtreasurer"
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
    Task 1: Check if user is authenticated. If not, redirect to MyProfile.
    Allow access to profile page, static files, and authentication routes.
    """
    allowed_routes = ['my_profile', 'login', 'signup', 'about_us', 'analysis', 'static']
    
    if 'user' not in session and request.endpoint and request.endpoint not in allowed_routes:
        return redirect(url_for('my_profile')) 

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
    """Fetch and display user's expense history."""
    try:
        username = session.get('user', 'guest')
        expenses = list(mongo.db.daily_expenses.find({"username": username}).sort("date", -1))
        return render_template('expenses.html', expenses=expenses)
    except Exception as e:
        print(f"Error fetching expenses: {e}")
        return render_template('expenses.html', expenses=[])

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

# --- AUTHENTICATION ROUTES ---

@app.route('/login', methods=['POST'])
def login():
    """Handle user login with 30-Day budget cycle logic."""
    try:
        name = request.form.get('name')
        password = request.form.get('password')

        print(f"Login attempt - Name: {name}")

        if not name or not password:
            print("Error: Missing required fields")
            flash('All fields are required.', 'error')
            return redirect(url_for('my_profile'))

        # Check if user exists
        user = mongo.db.users.find_one({'name': name, 'password': password})
        if user:
            # 30-Day Logic: Check if 30 days have passed since start_date
            if 'start_date' in user:
                start_date = user['start_date']
                current_date = datetime.now()
                days_passed = (current_date - start_date).days
                
                if days_passed >= 30:
                    # Reset budget cycle
                    print(f"30-Day cycle expired for {name}. Resetting...")
                    mongo.db.users.update_one(
                        {'name': name},
                        {'$set': {'current_spend': 0, 'start_date': current_date}}
                    )
                    flash('Your 30-day budget cycle has reset. Please set your new monthly limit.', 'info')
                    session['user'] = name
                    return redirect(url_for('my_profile'))
            
            session['user'] = name
            flash('Login successful! Welcome back.', 'success')
            return redirect(url_for('home'))
        else:
            print(f"Error: Invalid credentials for user '{name}'")
            flash('Invalid credentials. Please try again.', 'error')
            return redirect(url_for('my_profile'))

    except Exception as e:
        print(f"Login Error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Login failed: {str(e)}', 'error')
        return redirect(url_for('my_profile'))

@app.route('/signup', methods=['POST'])
def signup():
    """Handle new user registration."""
    try:
        name = request.form.get('name')
        password = request.form.get('password')
        monthly_limit_str = request.form.get('monthly_limit')

        print(f"Signup attempt - Name: {name}, Password: {'*' * len(password) if password else 'None'}, Monthly Limit: {monthly_limit_str}")

        if not name or not password or not monthly_limit_str:
            print("Error: Missing required fields")
            flash('All fields are required.', 'error')
            return redirect(url_for('my_profile'))

        monthly_limit = float(monthly_limit_str)

        # Check if user already exists
        existing_user = mongo.db.users.find_one({'name': name})
        if existing_user:
            print(f"Error: User '{name}' already exists")
            flash('User already exists. Please login.', 'error')
            return redirect(url_for('my_profile'))

        # Create new user
        new_user = {
            'name': name,
            'password': password,
            'monthly_limit': monthly_limit,
            'current_spend': 0,
            'start_date': datetime.now()
        }

        print(f"Inserting new user: {name}")
        result = mongo.db.users.insert_one(new_user)
        print(f"User inserted with ID: {result.inserted_id}")

        # Set session
        session['user'] = name
        flash('Account created successfully! Welcome to YourTreasurer.', 'success')
        return redirect(url_for('home'))

    except Exception as e:
        print(f"Signup Error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Signup failed: {str(e)}', 'error')
        return redirect(url_for('my_profile'))

@app.route('/logout')
def logout():
    """Handle user logout."""
    session.pop('user', None)
    flash('You have been logged out.', 'success')
    return redirect(url_for('my_profile'))

@app.route('/update_monthly_limit', methods=['POST'])
def update_monthly_limit():
    """Update monthly limit after 30-day budget cycle reset."""
    try:
        username = session.get('user')
        if not username:
            flash('Please login first.', 'error')
            return redirect(url_for('my_profile'))

        monthly_limit_str = request.form.get('monthly_limit')
        if not monthly_limit_str:
            flash('Monthly limit is required.', 'error')
            return redirect(url_for('my_profile'))

        monthly_limit = float(monthly_limit_str)

        # Update user's monthly_limit
        mongo.db.users.update_one(
            {'name': username},
            {'$set': {'monthly_limit': monthly_limit}}
        )

        flash('Monthly limit updated successfully!', 'success')
        return redirect(url_for('home'))

    except Exception as e:
        print(f"Update Monthly Limit Error: {e}")
        flash(f'Failed to update monthly limit: {str(e)}', 'error')
        return redirect(url_for('my_profile'))

# --- DATA SUBMISSION ROUTES (THE LOGIC) ---

@app.route('/add_expense', methods=['POST'])
def add_expense():
    """Handles adding a new daily expense."""
    try:
        category = request.form.get('category')
        amount = float(request.form.get('amount'))
        description = request.form.get('description', '')
        date = request.form.get('date')

        print(f"Adding expense - Category: {category}, Amount: ₹{amount}, Date: {date}")

        if not category or not amount or not date:
            print("Error: Missing required fields")
            flash('All fields are required.', 'error')
            return redirect(url_for('my_expenses'))

        # Parse date and use current time for hourly tracking
        expense_date = datetime.strptime(date, '%Y-%m-%d')
        expense_date = expense_date.replace(
            hour=datetime.now().hour,
            minute=datetime.now().minute,
            second=datetime.now().second
        )

        # Insert expense into MongoDB
        expense = {
            'username': session.get('user', 'guest'),
            'category': category,
            'amount': amount,
            'description': description,
            'date': expense_date
        }

        result = mongo.db.daily_expenses.insert_one(expense)
        print(f"Expense inserted with ID: {result.inserted_id}")

        flash('Expense added successfully!', 'success')
        return redirect(url_for('my_expenses'))

    except Exception as e:
        print(f"Expense Submit Error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Failed to add expense: {str(e)}', 'error')
        return redirect(url_for('my_expenses'))

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

@app.route('/delete_expense/<expense_id>', methods=['POST'])
def delete_expense(expense_id):
    """Delete an expense using ObjectId."""
    try:
        mongo.db.daily_expenses.delete_one({"_id": ObjectId(expense_id)})
        flash('Expense deleted successfully!', 'success')
        return redirect(url_for('my_expenses'))
    except Exception as e:
        print(f"Delete Error: {e}")
        return redirect(url_for('my_expenses'))

# --- API ROUTES (FOR REAL-TIME CHARTS) ---

@app.route('/api/spend_data')
def spend_data():
    """API endpoint to feed the Doughnut and Line charts in the Analysis tab."""
    try:
        # Query MongoDB, group expenses by category
        pipeline = [
            {
                "$group": {
                    "_id": "$category",
                    "total": {"$sum": "$amount"}
                }
            }
        ]
        
        result = list(mongo.db.daily_expenses.aggregate(pipeline))
        
        if result:
            categories = [item["_id"] for item in result]
            amounts = [item["total"] for item in result]
            return jsonify({"categories": categories, "amounts": amounts})
        else:
            # Return empty data if no expenses found
            return jsonify({"categories": [], "amounts": []})
            
    except Exception as e:
        print(f"Error fetching spend data: {e}")
        # Return dummy data as fallback
        dummy_data = {
            "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food", "Hostel Rent", "Travelling"],
            "amounts": [1200, 500, 800, 300, 5000, 450]
        }
        return jsonify(dummy_data)

@app.route('/api/hourly_data')
def hourly_data():
    """API endpoint for hourly spending of today (Day view)."""
    try:
        username = session.get('user', 'guest')
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Query today's expenses
        pipeline = [
            {
                "$match": {
                    "username": username,
                    "date": {"$gte": today_start, "$lte": today_end}
                }
            },
            {
                "$group": {
                    "_id": {"$hour": "$date"},
                    "total": {"$sum": "$amount"}
                }
            },
            {
                "$sort": {"_id": 1}
            }
        ]
        
        result = list(mongo.db.daily_expenses.aggregate(pipeline))
        
        if result:
            hours = [f"{item['_id']}:00" for item in result]
            amounts = [item["total"] for item in result]
            return jsonify({"labels": hours, "amounts": amounts})
        else:
            # Return empty data if no expenses today
            return jsonify({"labels": [], "amounts": []})
            
    except Exception as e:
        print(f"Error fetching hourly data: {e}")
        import traceback
        traceback.print_exc()
        # Return dummy data as fallback
        return jsonify({
            "labels": ["0:00", "6:00", "12:00", "18:00"],
            "amounts": [0, 100, 300, 150]
        })

@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)
