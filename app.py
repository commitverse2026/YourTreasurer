from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import cloudinary
import threading

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# ------------------ CONFIGURATION ------------------

# ☁️ Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_NAME"),
    api_key=os.getenv("CLOUDINARY_KEY"),
    api_secret=os.getenv("CLOUDINARY_SECRET")
)

# 🧠 MongoDB Atlas
app.config["MONGO_URI"] = os.getenv("MONGO_URI")
mongo = PyMongo(app)

# 📩 Mail Config
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.getenv("MAIL_USERNAME")
app.config['MAIL_PASSWORD'] = os.getenv("MAIL_PASSWORD")
mail = Mail(app)

# 📦 File limit
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

# ------------------ EMAIL ASYNC ------------------

def send_async_email(app, msg):
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent!")
        except Exception as e:
            print(f"Mail Error: {e}")

# ------------------ GLOBAL CHECK ------------------

@app.before_request
@app.before_request
def check_budget_setup():
    # List of endpoints that DON'T need a login
    allowed_routes = ['my_profile', 'static']

    if request.endpoint not in allowed_routes:
        if "user" not in session:
            return redirect(url_for('my_profile'))
# ------------------ ROUTES ------------------

# 🏠 Home (Dashboard)
@app.route('/')
def home():
    if "user" not in session:
        return redirect(url_for('my_profile'))

    user = mongo.db.users.find_one({"name": session["user"]})
    return render_template('index.html', user=user)

# 👤 Profile (LOGIN + REGISTER)
@app.route('/my_profile', methods=["GET", "POST"])
# 👤 Profile (LOGIN + REGISTER)
@app.route('/my_profile', methods=["GET", "POST"])
def my_profile():
    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")
        monthly_limit = request.form.get("limit")

        # 1. Look for user in database
        user = mongo.db.users.find_one({"name": name})

        if user:
            # 🟢 EXISTING USER LOGIN (Task 1 Logic)
            if user["password"] == password:
                session["user"] = name
                
                # Check for 30-day reset (Optional but good for Task 1)
                start_date = datetime.strptime(user["start_date"], "%Y-%m-%d")
                if datetime.now() > start_date + timedelta(days=30):
                    mongo.db.users.update_one(
                        {"name": name},
                        {"$set": {"current_spend": 0, "start_date": datetime.now().strftime("%Y-%m-%d")}}
                    )
                
                return redirect(url_for('home'))
            else:
                return "❌ Invalid Password" # You can use flash() here later
        else:
            # 🆕 NEW USER REGISTER
            if not monthly_limit:
                return "⚠️ Please provide a monthly budget to register."

            mongo.db.users.insert_one({
                "name": name,
                "password": password,
                "monthly_limit": int(monthly_limit),
                "current_spend": 0,
                "start_date": datetime.now().strftime("%Y-%m-%d")
            })
            session["user"] = name
            return redirect(url_for('home'))

    return render_template('profile.html')
# 🔓 Logout
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('my_profile'))

# 📊 Expenses Page (placeholder for next feature)
@app.route('/my_expenses')
def my_expenses():
    return render_template('expenses.html')

# 📈 Analysis Page
@app.route('/analysis')
def analysis():
    return render_template('analysis.html')

# 🔁 Interval Spend Page
@app.route('/interval_spend')
def interval_spend():
    return render_template('interval_spend.html')

# ℹ️ About Page
@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

# ------------------ API (DUMMY FOR NOW) ------------------

@app.route('/api/spend_data')
def spend_data():
    dummy_data = {
        "categories": ["Educational", "Lifestyle", "Healthy Food", "Junk Food"],
        "amounts": [1200, 500, 800, 300]
    }
    return jsonify(dummy_data)

# ------------------ ERROR HANDLER ------------------

@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>File too large!</h1><a href='/my_expenses'>Try Again</a>", 413

# ------------------ RUN ------------------

if __name__ == '__main__':
    app.run(debug=True)