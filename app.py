from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
import os
import uuid 
import threading
import time
import cloudinary
import cloudinary.uploader
from datetime import datetime

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
# TODO for Participants: Insert your free MongoDB Atlas URI here
app.config["MONGO_URI"] = "mongodb+srv://priteepardeshi3011_db_user:o1UpyYozHv4zvlTn@cluster0.a5drjzn.mongodb.net/"
mongo = PyMongo(app)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", 'your_app_password') 
mail = Mail(app)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 # 5MB limit for receipts

# --- TEST MONGODB CONNECTION ---
try:
    mongo.db.list_collection_names()
    print("✓ MongoDB Atlas connection successful!")
except Exception as e:
    print(f"✗ MongoDB Atlas connection failed: {e}")
    print("Please check:")
    print("  1. Atlas credentials are correct")
    print("  2. Your IP is whitelisted in Atlas Network Access")
    print("  3. Cluster exists and is running")

# --- ASYNC BACKGROUND TASKS ---

def send_async_email(app, msg):
    """Function to send email in a background thread to prevent UI freezing."""
    with app.app_context():
        try:
            mail.send(msg)
            print("Email sent successfully!")
        except Exception as e:
            print(f"Background Mail Error: {e}")

# --- GLOBAL CHECKS ---

@app.before_request
def check_budget_setup():
    """
    Task 1: Check if user is authenticated. If not, redirect to MyProfile.
    Allow access to profile page, static files, and authentication routes.
    """
    allowed_routes = ['my_profile', 'login', 'signup', 'about_us', 'analysis']
    
    if 'user' not in session and request.endpoint and request.endpoint not in allowed_routes:
        return redirect(url_for('my_profile')) 

# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    # TODO: Fetch today's expenses to show a quick summary on the home dashboard
    return render_template('index.html')

@app.route('/my_profile')
def my_profile():
    # TODO: Fetch user's current budget threshold from MongoDB
    return render_template('profile.html')

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