from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
import os
import threading
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "campuscoin_tracker_2026")

# --- CONFIGURATION ---

# Cloudinary
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_NAME", "your_cloud_name"),
    api_key=os.environ.get("CLOUDINARY_KEY", "your_api_key"),
    api_secret=os.environ.get("CLOUDINARY_SECRET", "your_api_secret")
)

# MongoDB — reads from .env, falls back to local
app.config["MONGO_URI"] = os.environ.get(
    "MONGO_URI",
    "mongodb://localhost:27017/expense_tracker"
)
mongo = PyMongo(app)

# Mail
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", 'your_app_password')
mail = Mail(app)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024


# --- ASYNC EMAIL ---

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
        except Exception as e:
            print(f"Mail Error: {e}")


# ============================================================
# TASK 1: THE SECURE BUDGET GATEWAY
# ============================================================
# Rule: Every route except profile setup must require a logged-in session.
# If no session exists, redirect to /my_profile so the user can log in
# or register. On login, the 30-day reset logic is applied.

@app.before_request
def check_budget_setup():
    """
    Guard: If the user has no active session, redirect them to the
    profile/login page for every route except the profile routes themselves
    and static asset requests.
    """
    # Routes that are always accessible (no login required)
    open_routes = {'my_profile', 'save_profile', 'static'}

    if request.endpoint not in open_routes and 'username' not in session:
        return redirect(url_for('my_profile'))


# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/my_profile')
def my_profile():
    """
    Shows the login / registration form.
    If a session already exists, pass the current user's data to pre-fill.
    """
    user = None
    if 'username' in session:
        user = mongo.db.users.find_one({"name": session['username']})
    return render_template('profile.html', user=user)


# ============================================================
# TASK 1 – CORE SAVE / LOGIN LOGIC
# ============================================================
@app.route('/save_profile', methods=['POST'])
def save_profile():
    """
    Handles both NEW USER registration and EXISTING USER login.

    New user  → insert document into `users` collection.
    Existing  → verify password, then check 30-day budget cycle.
    30-day reset → wipe current_spend, update start_date.
    """
    try:
        name          = request.form.get('name', '').strip()
        password      = request.form.get('password', '').strip()
        monthly_limit = request.form.get('monthly_limit', 0)

        if not name or not password:
            flash("Name and password are required.", "error")
            return redirect(url_for('my_profile'))

        monthly_limit = int(monthly_limit)

        user = mongo.db.users.find_one({"name": name})

        # ---------- EXISTING USER ----------
        if user:
            if user['password'] != password:
                flash("Incorrect password. Please try again.", "error")
                return redirect(url_for('my_profile'))

            # 30-DAY CYCLE CHECK
            start_date = user.get('start_date')
            if start_date:
                if isinstance(start_date, str):
                    start_date = datetime.strptime(start_date, "%Y-%m-%d")

                if datetime.now() > start_date + timedelta(days=30):
                    # Archive + reset
                    mongo.db.users.update_one(
                        {"name": name},
                        {
                            "$set": {
                                "current_spend": 0,
                                "start_date": datetime.now().strftime("%Y-%m-%d"),
                                "monthly_limit": monthly_limit if monthly_limit else user.get('monthly_limit', 0)
                            }
                        }
                    )
                    flash("Your 30-day budget cycle has reset. Welcome back!", "info")

            flash(f"Welcome back, {name}! 🎉", "success")

        # ---------- NEW USER ----------
        else:
            if monthly_limit <= 0:
                flash("Please enter a valid monthly budget.", "error")
                return redirect(url_for('my_profile'))

            mongo.db.users.insert_one({
                "name":          name,
                "password":      password,
                "monthly_limit": monthly_limit,
                "current_spend": 0,
                "start_date":    datetime.now().strftime("%Y-%m-%d")
            })
            flash(f"Account created! Welcome, {name}! 🏛️", "success")

        session['username'] = name
        return redirect(url_for('home'))

    except Exception as e:
        return f"Error: {e}", 500


@app.route('/logout')
def logout():
    session.pop('username', None)
    flash("You've been logged out.", "info")
    return redirect(url_for('my_profile'))


@app.route('/analysis')
def analysis():
    return render_template('analysis.html')


@app.route('/interval_spend')
def interval_spend():
    return render_template('interval_spend.html')


@app.route('/about_us')
def about_us():
    return render_template('about_us.html')


# --- DATA ROUTES (stubs – implemented in later tasks) ---

@app.route('/add_expense', methods=['POST'])
def add_expense():
    try:
        return redirect(url_for('home'))
    except Exception as e:
        return f"Error: {e}", 500


@app.route('/add_friend_loan', methods=['POST'])
def add_friend_loan():
    try:
        return redirect(url_for('home'))
    except Exception:
        return "Error", 500


@app.route('/add_interval_spend', methods=['POST'])
def add_interval_spend():
    try:
        return redirect(url_for('interval_spend'))
    except Exception:
        return "Error", 500


# --- SPEND DATA API (dummy – replaced in Task 4) ---

@app.route('/api/spend_data')
def spend_data():
    dummy_data = {
        "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food", "Hostel Rent", "Travelling"],
        "amounts":    [1200, 500, 800, 300, 5000, 450]
    }
    return jsonify(dummy_data)


# --- ERROR HANDLERS ---

@app.errorhandler(413)
def request_entity_too_large(error):
    return "File too large!", 413


# --- RUN ---

if __name__ == '__main__':
    app.run(debug=True, port=5000)
