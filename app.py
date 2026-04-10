from flask import Flask, render_template, request, flash, redirect, url_for, jsonify
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from pymongo.errors import PyMongoError
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import os
import uuid 
import threading
import time
import cloudinary
import cloudinary.uploader
from datetime import datetime
from calendar import monthrange

load_dotenv()

app = Flask(__name__, template_folder="Templates", static_folder="Static", static_url_path="/static")
app.secret_key = "campuscoin_tracker_2026"

# --- CONFIGURATION ---

# 1. Cloudinary Setup (Participants will use this for receipt uploads)
cloudinary.config( 
    cloud_name = os.environ.get("CLOUDINARY_NAME", "your_cloud_name"), 
    api_key = os.environ.get("CLOUDINARY_KEY", "your_api_key"), 
    api_secret = os.environ.get("CLOUDINARY_SECRET", "your_api_secret") 
)

# 2. MongoDB & Mail Setup
# Prefer .env configuration, with a safe local fallback database name in the URI.
app.config["MONGO_URI"] = os.environ.get(
    "MONGO_URI",
    "mongodb://127.0.0.1:27017/yourtreasurer"
)
mongo = PyMongo(app)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME") or os.environ.get("MAIL_USER", 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD") or os.environ.get("MAIL_PASS", 'your_app_password') 
mail = Mail(app)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 # 5MB limit for receipts

DEFAULT_USER_KEY = "primary"
DEFAULT_DB_NAME = "yourtreasurer"
OFFLINE_BUDGET_DOC = {
    "user_key": DEFAULT_USER_KEY,
    "email": "",
    "monthly_budget": 10000.0,
    "guardian_alerts": {},
    "offline_mode": True
}

# --- ASYNC BACKGROUND TASKS ---

def send_async_email(app, msg):
    """Function to send email in a background thread to prevent UI freezing."""
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent successfully!")
        except Exception as e:
            print(f"Background Mail Error: {e}")


def get_db():
    """Return a usable Mongo database even when the URI does not specify a default DB name."""
    if mongo.db is not None:
        return mongo.db
    return mongo.cx[DEFAULT_DB_NAME]


def get_offline_budget_document():
    return dict(OFFLINE_BUDGET_DOC)


def parse_amount(value, default=0.0):
    """Convert form input like '1,250' or 'Rs. 400' into a numeric amount."""
    if value is None:
        return default

    cleaned = str(value).strip().replace(",", "")
    cleaned = cleaned.replace("Rs.", "").replace("Rs", "").replace("INR", "").strip()

    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return default


def get_budget_owner():
    """Current repo has no auth/session yet, so treat the app as a single-user budget owner."""
    return DEFAULT_USER_KEY


def get_current_month_bounds(now=None):
    now = now or datetime.now()
    month_start = datetime(now.year, now.month, 1)
    last_day = monthrange(now.year, now.month)[1]
    month_end = datetime(now.year, now.month, last_day, 23, 59, 59, 999999)
    return month_start, month_end


def normalize_expense_document(form_data, now=None):
    now = now or datetime.now()
    amount = parse_amount(
        form_data.get("amount")
        or form_data.get("expense_amount")
        or form_data.get("price")
        or form_data.get("cost")
    )

    return {
        "user_key": get_budget_owner(),
        "title": form_data.get("title") or form_data.get("item_name") or form_data.get("expense_name") or "Expense",
        "category": form_data.get("category") or "General",
        "amount": amount,
        "notes": form_data.get("notes") or form_data.get("description") or "",
        "created_at": now,
        "month_key": now.strftime("%Y-%m")
    }


def get_user_budget_document():
    """
    Resolve the active budget record from a few likely collections so the guardian
    logic can start working even before the profile flow is fully built.
    """
    user_key = get_budget_owner()
    db = get_db()
    collections = [
        db.users,
        db.user_profile,
        db.budget_profiles,
        db.budgets
    ]

    candidates = [
        {"user_key": user_key},
        {"username": user_key},
        {"is_default": True}
    ]

    try:
        for collection in collections:
            for query in candidates:
                doc = collection.find_one(query)
                if doc:
                    return doc, collection

        fallback = {
            "user_key": user_key,
            "email": app.config.get("MAIL_USERNAME"),
            "monthly_budget": 10000.0,
            "guardian_alerts": {}
        }
        db.users.update_one(
            {"user_key": user_key},
            {"$setOnInsert": fallback},
            upsert=True
        )
        created = db.users.find_one({"user_key": user_key})
        return created or fallback, db.users
    except PyMongoError as e:
        print(f"Mongo budget lookup error: {e}")
        return get_offline_budget_document(), None


def get_budget_amount(budget_doc):
    for key in ("monthly_budget", "budget", "threshold", "budget_threshold", "monthly_limit"):
        amount = parse_amount(budget_doc.get(key), default=None)
        if amount is not None and amount > 0:
            return amount
    return 0.0


def get_alert_recipient(budget_doc):
    return (
        budget_doc.get("email")
        or budget_doc.get("guardian_email")
        or budget_doc.get("mail")
        or app.config.get("MAIL_USERNAME")
    )


def get_current_month_spend(user_key, now=None):
    now = now or datetime.now()
    month_start, month_end = get_current_month_bounds(now)
    db = get_db()
    pipeline = [
        {
            "$match": {
                "user_key": user_key,
                "created_at": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$group": {
                "_id": None,
                "total": {"$sum": "$amount"}
            }
        }
    ]
    try:
        result = list(db.daily_expenses.aggregate(pipeline))
        return float(result[0]["total"]) if result else 0.0
    except PyMongoError as e:
        print(f"Mongo spend aggregate error: {e}")
        return 0.0


def get_recent_expenses(user_key, limit=12):
    db = get_db()
    try:
        return list(
            db.daily_expenses
            .find({"user_key": user_key})
            .sort("created_at", -1)
            .limit(limit)
        )
    except PyMongoError as e:
        print(f"Mongo recent expenses error: {e}")
        return []


def get_budget_snapshot():
    budget_doc, _ = get_user_budget_document()
    user_key = budget_doc.get("user_key") or get_budget_owner()
    budget_amount = get_budget_amount(budget_doc)
    total_spent = get_current_month_spend(user_key)
    remaining_amount = budget_amount - total_spent

    usage_percent = 0.0
    if budget_amount > 0:
        usage_percent = (total_spent / budget_amount) * 100

    return {
        "budget": round(budget_amount, 2),
        "spent": round(total_spent, 2),
        "remaining": round(remaining_amount, 2),
        "usage_percent": round(usage_percent, 2),
        "capped_percent": max(0, min(round(usage_percent, 2), 100)),
        "offline_mode": bool(budget_doc.get("offline_mode")),
        "recipient_email": get_alert_recipient(budget_doc) or "",
        "status": (
            "Offline demo mode"
            if budget_doc.get("offline_mode")
            else "Live budget data"
        )
    }


def send_guardian_alert(level, recipient, budget_amount, total_spent, remaining_amount):
    subjects = {
        "10_percent": "Guardian Alert: Only 10% budget remaining",
        "5_percent": "Guardian Alert: Critical 5% budget remaining",
        "over_budget": "Guardian Alert: Stop spending now"
    }
    headlines = {
        "10_percent": "Caution: your monthly budget is almost used up.",
        "5_percent": "Critical warning: your budget is nearly exhausted.",
        "over_budget": "Stop alert: you are already over budget."
    }

    msg = Message(
        subject=subjects[level],
        recipients=[recipient]
    )
    msg.body = (
        f"{headlines[level]}\n\n"
        f"Monthly budget: Rs. {budget_amount:.2f}\n"
        f"Spent this month: Rs. {total_spent:.2f}\n"
        f"Remaining budget: Rs. {remaining_amount:.2f}\n\n"
        "Please review your recent expenses and pause non-essential spending."
    )
    threading.Thread(target=send_async_email, args=(app, msg), daemon=True).start()


def maybe_send_guardian_alerts(total_spent, expense_amount, now=None):
    now = now or datetime.now()
    budget_doc, budget_collection = get_user_budget_document()
    budget_amount = get_budget_amount(budget_doc)
    recipient = get_alert_recipient(budget_doc)
    user_key = budget_doc.get("user_key") or budget_doc.get("username") or get_budget_owner()

    if budget_doc.get("offline_mode"):
        return ["Atlas is offline, so guardian emails are paused right now."]

    if not recipient or budget_amount <= 0:
        return []

    remaining_amount = budget_amount - total_spent
    month_key = now.strftime("%Y-%m")
    alerts = budget_doc.get("guardian_alerts") or {}
    month_alerts = alerts.get(month_key, {})
    updates = {}
    triggered_alerts = []

    if remaining_amount <= 0:
        send_guardian_alert("over_budget", recipient, budget_amount, total_spent, remaining_amount)
        updates[f"guardian_alerts.{month_key}.over_budget_last_sent_at"] = now
        triggered_alerts.append("Stop alert sent: you are over budget.")
    else:
        remaining_ratio = remaining_amount / budget_amount

        if remaining_ratio <= 0.05 and not month_alerts.get("five_percent_sent"):
            send_guardian_alert("5_percent", recipient, budget_amount, total_spent, remaining_amount)
            updates[f"guardian_alerts.{month_key}.five_percent_sent"] = True
            updates[f"guardian_alerts.{month_key}.five_percent_sent_at"] = now
            triggered_alerts.append("Critical 5% remaining alert sent.")
        elif remaining_ratio <= 0.10 and not month_alerts.get("ten_percent_sent"):
            send_guardian_alert("10_percent", recipient, budget_amount, total_spent, remaining_amount)
            updates[f"guardian_alerts.{month_key}.ten_percent_sent"] = True
            updates[f"guardian_alerts.{month_key}.ten_percent_sent_at"] = now
            triggered_alerts.append("Caution alert sent: only 10% budget remains.")

    if updates and budget_collection is not None and budget_doc.get("_id") is not None:
        budget_collection.update_one(
            {"_id": budget_doc["_id"]},
            {"$set": updates, "$setOnInsert": {"user_key": user_key}},
            upsert=True
        )

    return triggered_alerts


def save_budget_profile(form_data):
    user_key = get_budget_owner()
    db = get_db()
    monthly_budget = parse_amount(form_data.get("monthly_budget") or form_data.get("budget"))
    email = (form_data.get("email") or "").strip() or app.config.get("MAIL_USERNAME")
    profile_update = {
        "user_key": user_key,
        "email": email,
        "monthly_budget": monthly_budget,
        "updated_at": datetime.now()
    }
    try:
        db.users.update_one(
            {"user_key": user_key},
            {
                "$set": profile_update,
                "$setOnInsert": {
                    "guardian_alerts": {}
                }
            },
            upsert=True
        )
        return db.users.find_one({"user_key": user_key})
    except PyMongoError as e:
        print(f"Mongo profile save error: {e}")
        offline_doc = get_offline_budget_document()
        offline_doc["email"] = email
        offline_doc["monthly_budget"] = monthly_budget
        return offline_doc

# --- GLOBAL CHECKS ---

@app.before_request
def check_budget_setup():
    """
    TODO Task 1: Check if the user has set up their initial monthly budget.
    If they haven't (and they aren't on static/profile pages), redirect them to MyProfile.
    """
    pass 

# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    budget_snapshot = get_budget_snapshot()
    return render_template('index.html', budget_snapshot=budget_snapshot)

@app.route('/my_profile')
def my_profile():
    budget_doc, _ = get_user_budget_document()
    if budget_doc.get("offline_mode"):
        flash("MongoDB Atlas is not reachable right now. You are seeing offline demo values.", "warning")
    return render_template(
        'profile.html',
        budget_doc=budget_doc,
        budget_amount=get_budget_amount(budget_doc)
    )


@app.route('/save_profile', methods=['POST'])
def save_profile():
    try:
        budget_doc = save_budget_profile(request.form.to_dict())
        flash("Profile saved. Guardian alerts will use the updated budget and email.", "success")
        return redirect(url_for('my_profile'))
    except Exception as e:
        print(f"Profile Save Error: {e}")
        flash("Could not save your profile right now.", "error")
        return redirect(url_for('my_profile'))

@app.route('/my_expenses')
def my_expenses():
    budget_doc, _ = get_user_budget_document()
    if budget_doc.get("offline_mode"):
        flash("MongoDB Atlas is not reachable right now. Expense saves and guardian emails are paused.", "warning")
    user_key = budget_doc.get("user_key") or get_budget_owner()
    total_spent = get_current_month_spend(user_key)
    budget_amount = get_budget_amount(budget_doc)
    remaining_amount = budget_amount - total_spent
    expenses = get_recent_expenses(user_key)
    return render_template(
        'expenses.html',
        expenses=expenses,
        budget_doc=budget_doc,
        budget_amount=budget_amount,
        total_spent=total_spent,
        remaining_amount=remaining_amount
    )

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
        db = get_db()

        # 1. TODO: Handle Cloudinary receipt upload if 'receipt_image' exists in request.files
        expense_doc = normalize_expense_document(form_data)
        if expense_doc.get("user_key") and not get_user_budget_document()[0].get("offline_mode"):
            db.daily_expenses.insert_one(expense_doc)
        else:
            flash("MongoDB Atlas is not reachable, so this expense was not saved.", "warning")
            return redirect(url_for('my_expenses'))
        total_spent = get_current_month_spend(expense_doc["user_key"], expense_doc["created_at"])
        triggered_alerts = maybe_send_guardian_alerts(total_spent, expense_doc["amount"], expense_doc["created_at"])

        flash(f"Expense added: {expense_doc['title']} for Rs. {expense_doc['amount']:.0f}.", "success")
        for message in triggered_alerts:
            flash(message, "warning")

        return redirect(url_for('my_expenses'))
    except Exception as e:
        print(f"Expense Submit Error: {e}")
        flash("Expense submission failed. Please try again.", "error")
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


@app.route('/api/budget_status')
def budget_status():
    return jsonify(get_budget_snapshot())


@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)
