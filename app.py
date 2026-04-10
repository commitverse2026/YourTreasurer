from flask import Flask, render_template, request, flash, redirect, url_for, jsonify, session
from flask_pymongo import PyMongo
from flask_mail import Mail, Message
from werkzeug.utils import secure_filename
from bson import ObjectId
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
app.config["MONGO_URI"] = "mongodb+srv://dummyworld36_db_user:39zxLuQg1hIELacU@cluster0.6rfqi8c.mongodb.net/yourtreasurer"
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

@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)