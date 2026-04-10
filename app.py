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
from datetime import datetime, timedelta
from bson.objectid import ObjectId
import urllib.parse

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
# User's MongoDB Atlas connection with proper URL encoding
encoded_password = urllib.parse.quote_plus("Zahara@#$1")
app.config["MONGO_URI"] = f"mongodb+srv://Zahara:{encoded_password}@cluster0.dyxzgxe.mongodb.net/yourtreasurer"
mongo = PyMongo(app)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USER", 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASS", 'your_app_password') 
mail = Mail(app)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024 # 5MB limit for receipts

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
    Zero-Persistence: Check if user is authenticated and has set up budget.
    Redirect to profile if not authenticated or budget not set.
    """
    # Pages that don't require authentication
    allowed_routes = ['my_profile', 'authenticate', 'register', 'static', 'logout']
    
    if request.endpoint and request.endpoint in allowed_routes:
        return
    
    # Check if user is authenticated via session
    if 'user_name' not in session:
        return redirect(url_for('my_profile'))
    
    # Zero-Persistence: Verify user exists in MongoDB
    user = mongo.db.users.find_one({"name": session['user_name']})
    if not user:
        session.clear()
        return redirect(url_for('my_profile'))
    
    # Check if user has monthly_limit set
    if not user.get('monthly_limit'):
        flash("Please set your monthly budget limit to continue.", "info")
        return redirect(url_for('my_profile')) 

# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    # TODO: Fetch today's expenses to show a quick summary on the home dashboard
    return render_template('index.html', datetime=datetime)

@app.route('/my_profile')
def my_profile():
    # Check if user is already logged in
    if 'user_name' in session:
        # Zero-Persistence: Verify user exists in MongoDB
        user = mongo.db.users.find_one({"name": session['user_name']})
        if user:
            # User is logged in, show profile with user data
            return render_template('profile.html', user=user, datetime=datetime)
        else:
            # User not found in DB, clear session
            session.clear()
    
    # User not logged in, show login/registration form
    return render_template('profile.html', datetime=datetime)

@app.route('/my_expenses')
def my_expenses():
    # TODO: Fetch all expenses from MongoDB, sort by date, and pass to template
    return render_template('expenses.html', datetime=datetime)

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

@app.route('/authenticate', methods=['POST'])
def authenticate():
    """Handle user login with Zero-Persistence rule."""
    try:
        name = request.form.get('name')
        password = request.form.get('password')
        
        # Zero-Persistence: Verify against MongoDB for every request
        user = mongo.db.users.find_one({"name": name})
        
        if user and user['password'] == password:
            # Check for 30-day reset
            start_date = user.get('start_date', datetime.now())
            current_date = datetime.now()
            
            # If 30 days have passed, reset the budget cycle
            if current_date > start_date + timedelta(days=30):
                # Archive current expenses and reset
                mongo.db.users.update_one(
                    {"_id": user["_id"]},
                    {
                        "$set": {
                            "current_spend": 0,
                            "start_date": current_date,
                            "last_reset": current_date
                        }
                    }
                )
                flash("Your 30-day budget cycle has been reset! Please set a new monthly limit.", "info")
                return redirect(url_for('my_profile'))
            
            # Set session (minimal info only)
            session['user_name'] = name
            flash("Welcome back! Your treasury is unlocked.", "success")
            return redirect(url_for('home'))
        else:
            flash("Invalid credentials. Please try again.", "error")
            return redirect(url_for('my_profile'))
            
    except Exception as e:
        print(f"Authentication Error: {e}")
        flash("Login failed. Please try again.", "error")
        return redirect(url_for('my_profile'))

@app.route('/register', methods=['POST'])
def register():
    """Handle new user registration."""
    try:
        name = request.form.get('name')
        password = request.form.get('password')
        monthly_limit = float(request.form.get('monthly_limit'))
        
        # Check if user already exists
        existing_user = mongo.db.users.find_one({"name": name})
        if existing_user:
            flash("User already exists. Please login.", "error")
            return redirect(url_for('my_profile'))
        
        # Create new user with initial budget setup
        new_user = {
            "name": name,
            "password": password,  # In production, use proper hashing
            "monthly_limit": monthly_limit,
            "current_spend": 0,
            "start_date": datetime.now(),
            "created_at": datetime.now(),
            "email_sent_10": False,  # Flags for email alerts
            "email_sent_5": False,
            "email_sent_0": False
        }
        
        mongo.db.users.insert_one(new_user)
        session['user_name'] = name
        flash("Account created successfully! Your budget journey begins now.", "success")
        return redirect(url_for('home'))
        
    except Exception as e:
        print(f"Registration Error: {e}")
        flash("Registration failed. Please try again.", "error")
        return redirect(url_for('my_profile'))

@app.route('/update_profile', methods=['POST'])
def update_profile():
    """Handle user profile update."""
    try:
        # Check if user is logged in
        if 'user_name' not in session:
            flash("Please login to update your profile.", "error")
            return redirect(url_for('my_profile'))
        
        # Get form data
        name = request.form.get('name')
        monthly_limit = float(request.form.get('monthly_limit'))
        password = request.form.get('password')
        
        # Validate input
        if len(name) < 3:
            flash("Name must be at least 3 characters long.", "error")
            return redirect(url_for('my_profile'))
        
        if monthly_limit < 100:
            flash("Monthly budget must be at least Rs. 100.", "error")
            return redirect(url_for('my_profile'))
        
        # Prepare update data
        update_data = {
            "name": name,
            "monthly_limit": monthly_limit,
            "updated_at": datetime.now()
        }
        
        # Update password only if provided
        if password and len(password) >= 6:
            update_data["password"] = password
        
        # Update user in MongoDB
        result = mongo.db.users.update_one(
            {"name": session['user_name']},
            {"$set": update_data}
        )
        
        if result.modified_count > 0:
            # Update session if name changed
            if name != session['user_name']:
                session['user_name'] = name
            
            flash("Profile updated successfully!", "success")
        else:
            flash("No changes made to your profile.", "info")
        
        return redirect(url_for('my_profile'))
        
    except Exception as e:
        print(f"Profile Update Error: {e}")
        flash("Failed to update profile. Please try again.", "error")
        return redirect(url_for('my_profile'))

@app.route('/logout')
def logout():
    """Handle user logout."""
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for('my_profile'))

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