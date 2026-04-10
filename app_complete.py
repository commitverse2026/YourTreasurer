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
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = "campuscoin_tracker_2026"

# Session configuration
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# --- CONFIGURATION ---

# 1. Cloudinary Setup (Participants will use this for receipt uploads)
cloudinary.config( 
    cloud_name = os.environ.get("CLOUDINARY_NAME", "your_cloud_name"), 
    api_key = os.environ.get("CLOUDINARY_KEY", "your_api_key"), 
    api_secret = os.environ.get("CLOUDINARY_SECRET", "your_api_secret") 
)

# 2. MongoDB & Mail Setup
# TODO for Participants: Insert your free MongoDB Atlas URI here
app.config["MONGO_URI"] = "mongodb+srv://dummyworld36_db_user:39zxLuQg1hIELacU@cluster0.6rfqi8c.mongodb.net/"
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

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('my_profile'))
        return f(*args, **kwargs)
    return decorated_function

@app.before_request
def check_budget_setup():
    """
    Task 1: Check if the user has set up their initial monthly budget.
    If they haven't (and they aren't on static/profile pages), redirect them to MyProfile.
    """
    # Allow access to profile page and static files
    if request.endpoint in ['my_profile', 'setup_profile', 'static']:
        return None
    
    # Check if user is logged in
    if 'user_id' not in session:
        return redirect(url_for('my_profile'))
    
    # Check 30-day reset logic
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
    if user:
        start_date = user.get('start_date')
        if start_date:
            # Check if 30 days have passed
            if datetime.now() > start_date + timedelta(days=30):
                # Archive current expenses and reset
                mongo.db.archived_expenses.insert_many(
                    mongo.db.daily_expenses.find({'username': user['name']})
                )
                mongo.db.daily_expenses.delete_many({'username': user['name']})
                
                # Reset user data
                mongo.db.users.update_one(
                    {'_id': ObjectId(session['user_id'])},
                    {'$set': {'start_date': datetime.now(), 'current_spend': 0}}
                )
                flash('Your 30-day budget cycle has been reset! Please set your new monthly budget.', 'info')
                return redirect(url_for('my_profile'))
    
    return None 

# --- CORE NAVIGATION ROUTES ---

@app.route('/')
def home():
    """Home page with real-time progress bar"""
    if 'user_id' not in session:
        return redirect(url_for('my_profile'))
    
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
    if not user:
        return redirect(url_for('my_profile'))
    
    # Calculate total spent
    total_spent = 0
    expenses = mongo.db.daily_expenses.find({'username': user['name']})
    for expense in expenses:
        total_spent += expense['amount']
    
    # Calculate progress percentage
    progress_percentage = (total_spent / user['monthly_limit']) * 100 if user['monthly_limit'] > 0 else 0
    
    # Get today's expenses
    today = datetime.now().strftime('%Y-%m-%d')
    today_expenses = list(mongo.db.daily_expenses.find({
        'username': user['name'],
        'date': {'$gte': datetime.strptime(today, '%Y-%m-%d')}
    }))
    
    return render_template('index.html', 
                         user=user, 
                         total_spent=total_spent,
                         progress_percentage=progress_percentage,
                         today_expenses=today_expenses)

@app.route('/my_profile')
def my_profile():
    """Profile page with user authentication"""
    user = None
    if 'user_id' in session:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
    
    return render_template('profile.html', user=user)

@app.route('/my_expenses')
@login_required
def my_expenses():
    """Expenses page with real-time spend history"""
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
    expenses = list(mongo.db.daily_expenses.find({'username': user['name']}).sort('date', -1))
    
    return render_template('expenses.html', expenses=expenses)

@app.route('/analysis')
@login_required
def analysis():
    """Analysis page with charts"""
    return render_template('analysis.html')

@app.route('/interval_spend')
@login_required
def interval_spend():
    """Interval spend page with recurring payments"""
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
    payments = list(mongo.db.recurring_payments.find({'username': user['name'], 'active': True}))
    
    # Calculate days until due for each payment
    for payment in payments:
        payment['days_until_due'] = (payment['due_date'] - datetime.now()).days
    
    return render_template('interval_spend.html', payments=payments)

@app.route('/about_us')
def about_us():
    return render_template('about_us.html')

# --- USER AUTHENTICATION ROUTES ---

@app.route('/setup_profile', methods=['POST'])
def setup_profile():
    """Handle user profile setup and login"""
    try:
        name = request.form['name']
        password = request.form['password']
        monthly_limit = float(request.form['monthly_limit'])
        
        # Check if user exists
        existing_user = mongo.db.users.find_one({'name': name})
        
        if existing_user:
            # Verify password
            if existing_user['password'] == password:
                # Login successful
                session['user_id'] = str(existing_user['_id'])
                session.permanent = True
                
                # Check if 30-day reset is needed
                if datetime.now() > existing_user['start_date'] + timedelta(days=30):
                    # Reset cycle
                    mongo.db.archived_expenses.insert_many(
                        mongo.db.daily_expenses.find({'username': name})
                    )
                    mongo.db.daily_expenses.delete_many({'username': name})
                    
                    mongo.db.users.update_one(
                        {'_id': existing_user['_id']},
                        {'$set': {
                            'start_date': datetime.now(),
                            'monthly_limit': monthly_limit,
                            'current_spend': 0
                        }}
                    )
                    flash('Budget cycle reset successfully!', 'success')
                else:
                    # Update monthly limit
                    mongo.db.users.update_one(
                        {'_id': existing_user['_id']},
                        {'$set': {'monthly_limit': monthly_limit}}
                    )
                    flash('Profile updated successfully!', 'success')
                
                return redirect(url_for('home'))
            else:
                flash('Invalid password!', 'danger')
                return redirect(url_for('my_profile'))
        else:
            # Create new user
            user_data = {
                'name': name,
                'password': password,
                'monthly_limit': monthly_limit,
                'start_date': datetime.now(),
                'current_spend': 0
            }
            
            result = mongo.db.users.insert_one(user_data)
            session['user_id'] = str(result.inserted_id)
            session.permanent = True
            
            flash('Account created successfully!', 'success')
            return redirect(url_for('home'))
            
    except Exception as e:
        print(f"Profile Setup Error: {e}")
        flash('Failed to setup profile', 'danger')
        return redirect(url_for('my_profile'))

@app.route('/logout')
def logout():
    """Logout user"""
    session.clear()
    return redirect(url_for('my_profile'))

# --- DATA SUBMISSION ROUTES (THE LOGIC) ---

@app.route('/add_expense', methods=['POST'])
@login_required
def add_expense():
    """Handles adding a new daily expense."""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        form_data = request.form.to_dict()
        
        # Handle Cloudinary receipt upload
        receipt_url = None
        if 'receipt_image' in request.files and request.files['receipt_image'].filename:
            file = request.files['receipt_image']
            if file and file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                upload_result = cloudinary.uploader.upload(file)
                receipt_url = upload_result['secure_url']
        
        # Prepare expense data
        expense_data = {
            'username': user['name'],
            'category': form_data['category'],
            'amount': float(form_data['amount']),
            'description': form_data['description'],
            'date': datetime.now(),
            'is_loan': 'is_loan' in form_data,
            'receipt_url': receipt_url
        }
        
        # Handle loan-specific fields
        if expense_data['is_loan']:
            expense_data.update({
                'friend_name': form_data.get('friend_name', ''),
                'friend_email': form_data.get('friend_email', ''),
                'relationship': form_data.get('relationship', 'Friend'),
                'returned': False
            })
            
            # Send loan handshake email
            send_loan_handshake_email(expense_data)
        
        # Insert expense into MongoDB
        mongo.db.daily_expenses.insert_one(expense_data)
        
        # Check budget thresholds and send alerts
        check_budget_alerts(user)
        
        flash('Expense added successfully!', 'success')
        return redirect(url_for('my_expenses'))
        
    except Exception as e:
        print(f"Expense Submit Error: {e}")
        flash(f'Submission failed: {e}', 'danger')
        return redirect(url_for('my_expenses'))

@app.route('/add_friend_loan', methods=['POST'])
@login_required
def add_friend_loan():
    """Handles logging money given to a friend and sending initial email."""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        form_data = request.form.to_dict()
        
        loan_data = {
            'username': user['name'],
            'friend_name': form_data['friend_name'],
            'friend_email': form_data['friend_email'],
            'amount': float(form_data['amount']),
            'description': form_data['description'],
            'relationship': form_data.get('relationship', 'Friend'),
            'date': datetime.now(),
            'returned': False
        }
        
        # Save loan to database
        mongo.db.friend_loans.insert_one(loan_data)
        
        # Send email to friend
        send_loan_handshake_email(loan_data)
        
        flash('Loan recorded and notification sent to friend!', 'success')
        return redirect(url_for('my_expenses'))
        
    except Exception as e:
        print(f"Friend Loan Error: {e}")
        flash('Failed to record loan', 'danger')
        return redirect(url_for('my_expenses'))

@app.route('/add_interval_spend', methods=['POST'])
@login_required
def add_interval_spend():
    """Handles adding EMIs, Hostel Fees, Subscriptions."""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        form_data = request.form.to_dict()
        
        payment_data = {
            'username': user['name'],
            'payment_type': form_data['payment_type'],
            'amount': float(form_data['amount']),
            'description': form_data['description'],
            'due_date': datetime.strptime(form_data['due_date'], '%Y-%m-%d'),
            'reminder_days': int(form_data['reminder_days']),
            'created_date': datetime.now(),
            'active': True
        }
        
        # Calculate days until due
        payment_data['days_until_due'] = (payment_data['due_date'] - datetime.now()).days
        
        # Save interval spend to MongoDB
        mongo.db.recurring_payments.insert_one(payment_data)
        
        flash('Interval payment added successfully!', 'success')
        return redirect(url_for('interval_spend'))
        
    except Exception as e:
        print(f"Interval Spend Error: {e}")
        flash('Failed to add interval payment', 'danger')
        return redirect(url_for('interval_spend'))

# --- EXPENSE MANAGEMENT ROUTES ---

@app.route('/delete_expense/<expense_id>', methods=['DELETE'])
@login_required
def delete_expense(expense_id):
    """Delete an expense"""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        
        # Delete expense
        result = mongo.db.daily_expenses.delete_one({
            '_id': ObjectId(expense_id),
            'username': user['name']
        })
        
        if result.deleted_count > 0:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Expense not found'})
            
    except Exception as e:
        print(f"Delete Expense Error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/mark_returned/<expense_id>', methods=['POST'])
@login_required
def mark_returned(expense_id):
    """Mark a loan as returned"""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        
        # Find the expense
        expense = mongo.db.daily_expenses.find_one({
            '_id': ObjectId(expense_id),
            'username': user['name'],
            'is_loan': True
        })
        
        if expense:
            # Mark as returned
            mongo.db.daily_expenses.update_one(
                {'_id': ObjectId(expense_id)},
                {'$set': {'returned': True}}
            )
            
            # Add negative entry to daily_expenses
            return_data = {
                'username': user['name'],
                'category': 'Loan Return',
                'amount': -expense['amount'],  # Negative amount
                'description': f"Loan returned from {expense['friend_name']}",
                'date': datetime.now(),
                'is_loan': False,
                'returned': True
            }
            
            mongo.db.daily_expenses.insert_one(return_data)
            
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Loan not found'})
            
    except Exception as e:
        print(f"Mark Returned Error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/remind_friend/<expense_id>', methods=['POST'])
@login_required
def remind_friend(expense_id):
    """Send reminder to friend about loan"""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        
        # Find the expense
        expense = mongo.db.daily_expenses.find_one({
            '_id': ObjectId(expense_id),
            'username': user['name'],
            'is_loan': True
        })
        
        if expense and not expense['returned']:
            # Send reminder email
            send_reminder_email(expense)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Loan not found or already returned'})
            
    except Exception as e:
        print(f"Remind Friend Error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete_interval_payment/<payment_id>', methods=['DELETE'])
@login_required
def delete_interval_payment(payment_id):
    """Delete an interval payment"""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        
        # Delete payment
        result = mongo.db.recurring_payments.delete_one({
            '_id': ObjectId(payment_id),
            'username': user['name']
        })
        
        if result.deleted_count > 0:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Payment not found'})
            
    except Exception as e:
        print(f"Delete Payment Error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# --- API ROUTES (FOR REAL-TIME CHARTS) ---

@app.route('/api/spend_data')
@login_required
def spend_data():
    """API endpoint to feed the Doughnut and Line charts in the Analysis tab."""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        
        # Aggregate expenses by category
        pipeline = [
            {'$match': {'username': user['name']}},
            {'$group': {'_id': '$category', 'total': {'$sum': '$amount'}}},
            {'$sort': {'total': -1}}
        ]
        
        category_data = list(mongo.db.daily_expenses.aggregate(pipeline))
        
        categories = [item['_id'] for item in category_data]
        amounts = [item['total'] for item in category_data]
        
        return jsonify({
            'categories': categories,
            'amounts': amounts
        })
        
    except Exception as e:
        print(f"API Error: {e}")
        return jsonify({'categories': [], 'amounts': []})

@app.route('/api/trend_data')
@login_required
def trend_data():
    """API endpoint for trend data (day/month view)"""
    try:
        user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})
        view = request.args.get('view', 'day')
        
        if view == 'day':
            # Today's hourly spending
            today = datetime.now().strftime('%Y-%m-%d')
            start_date = datetime.strptime(today, '%Y-%m-%d')
            end_date = start_date + timedelta(days=1)
            
            pipeline = [
                {'$match': {
                    'username': user['name'],
                    'date': {'$gte': start_date, '$lt': end_date}
                }},
                {'$group': {
                    '_id': {'$hour': '$date'},
                    'total': {'$sum': '$amount'}
                }},
                {'$sort': {'_id': 1}}
            ]
            
            labels = [f"{hour}:00" for hour in range(24)]
            amounts = [0] * 24
            
            hourly_data = list(mongo.db.daily_expenses.aggregate(pipeline))
            for item in hourly_data:
                if 0 <= item['_id'] < 24:
                    amounts[item['_id']] = item['total']
                    
        else:  # month view
            # Last 30 days daily spending
            start_date = datetime.now() - timedelta(days=30)
            
            pipeline = [
                {'$match': {
                    'username': user['name'],
                    'date': {'$gte': start_date}
                }},
                {'$group': {
                    '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$date'}},
                    'total': {'$sum': '$amount'}
                }},
                {'$sort': {'_id': 1}}
            ]
            
            daily_data = list(mongo.db.daily_expenses.aggregate(pipeline))
            labels = [item['_id'] for item in daily_data]
            amounts = [item['total'] for item in daily_data]
        
        return jsonify({
            'labels': labels,
            'amounts': amounts
        })
        
    except Exception as e:
        print(f"Trend API Error: {e}")
        return jsonify({'labels': [], 'amounts': []})

# --- HELPER FUNCTIONS ---

def check_budget_alerts(user):
    """Check budget thresholds and send alerts"""
    try:
        # Calculate total spent
        total_spent = 0
        expenses = mongo.db.daily_expenses.find({'username': user['name']})
        for expense in expenses:
            total_spent += expense['amount']
        
        # Calculate percentage spent
        percentage_spent = (total_spent / user['monthly_limit']) * 100 if user['monthly_limit'] > 0 else 0
        
        # Check thresholds and send alerts
        if percentage_spent >= 100:
            # Over budget - send alert for every new expense
            send_budget_alert(user, 'over_budget', percentage_spent)
        elif percentage_spent >= 95 and not user.get('alert_95_sent', False):
            # 5% remaining - send once per cycle
            send_budget_alert(user, 'critical', percentage_spent)
            mongo.db.users.update_one(
                {'_id': user['_id']},
                {'$set': {'alert_95_sent': True}}
            )
        elif percentage_spent >= 90 and not user.get('alert_90_sent', False):
            # 10% remaining - send once per cycle
            send_budget_alert(user, 'warning', percentage_spent)
            mongo.db.users.update_one(
                {'_id': user['_id']},
                {'$set': {'alert_90_sent': True}}
            )
            
    except Exception as e:
        print(f"Budget Alert Error: {e}")

def send_budget_alert(user, alert_type, percentage):
    """Send budget alert email"""
    try:
        subject = f"YourTreasurer Budget Alert - {alert_type.title()}"
        
        if alert_type == 'over_budget':
            body = f"""
            Dear {user['name']},
            
            ALERT: You have exceeded your monthly budget!
            
            Budget Limit: Rs{user['monthly_limit']}
            Current Spending: Rs{user['monthly_limit'] * (percentage / 100)}
            Overspend: {percentage - 100:.1f}%
            
            Please review your spending immediately.
            
            Best regards,
            YourTreasurer
            """
        elif alert_type == 'critical':
            body = f"""
            Dear {user['name']},
            
            CRITICAL: You have only 5% of your budget remaining!
            
            Budget Limit: Rs{user['monthly_limit']}
            Current Spending: {percentage:.1f}% used
            Remaining: Rs{user['monthly_limit'] * 0.05}
            
            Please be very careful with your spending.
            
            Best regards,
            YourTreasurer
            """
        else:  # warning
            body = f"""
            Dear {user['name']},
            
            WARNING: You have used 90% of your monthly budget!
            
            Budget Limit: Rs{user['monthly_limit']}
            Current Spending: {percentage:.1f}% used
            Remaining: Rs{user['monthly_limit'] * 0.1}
            
            Please monitor your spending carefully.
            
            Best regards,
            YourTreasurer
            """
        
        msg = Message(
            subject,
            sender=app.config['MAIL_USERNAME'],
            recipients=[user.get('email', app.config['MAIL_USERNAME'])]
        )
        msg.body = body
        
        # Send in background
        threading.Thread(target=send_async_email, args=(app, msg)).start()
        
    except Exception as e:
        print(f"Send Budget Alert Error: {e}")

def send_loan_handshake_email(loan_data):
    """Send loan notification to friend"""
    try:
        subject = f"Loan Notification from {loan_data['username']}"
        body = f"""
        Dear {loan_data['friend_name']},
        
        This is to inform you that {loan_data['username']} has recorded a loan to you:
        
        Amount: Rs{loan_data['amount']}
        Description: {loan_data['description']}
        Date: {loan_data['date'].strftime('%d %b %Y')}
        
        Please acknowledge this loan and make arrangements for repayment.
        
        Best regards,
        YourTreasurer (Automated System)
        """
        
        msg = Message(
            subject,
            sender=app.config['MAIL_USERNAME'],
            recipients=[loan_data['friend_email']]
        )
        msg.body = body
        
        # Send in background
        threading.Thread(target=send_async_email, args=(app, msg)).start()
        
    except Exception as e:
        print(f"Send Loan Handshake Error: {e}")

def send_reminder_email(expense):
    """Send reminder email to friend"""
    try:
        subject = f"Gentle Reminder: Loan from {expense['username']}"
        body = f"""
        Dear {expense['friend_name']},
        
        This is a gentle reminder about your loan from {expense['username']}:
        
        Amount: Rs{expense['amount']}
        Description: {expense['description']}
        Date: {expense['date'].strftime('%d %b %Y')}
        
        Please make arrangements for repayment at your earliest convenience.
        
        Best regards,
        YourTreasurer (Automated System)
        """
        
        msg = Message(
            subject,
            sender=app.config['MAIL_USERNAME'],
            recipients=[expense['friend_email']]
        )
        msg.body = body
        
        # Send in background
        threading.Thread(target=send_async_email, args=(app, msg)).start()
        
    except Exception as e:
        print(f"Send Reminder Error: {e}")

# --- SMART REMINDER AUTOMATION ---

def check_recurring_payments():
    """Check for due recurring payments and send reminders"""
    try:
        # Get all active recurring payments
        payments = list(mongo.db.recurring_payments.find({'active': True}))
        
        for payment in payments:
            days_until_due = (payment['due_date'] - datetime.now()).days
            
            # Check if reminder should be sent
            if days_until_due <= payment['reminder_days'] and days_until_due >= 0:
                # Send reminder to user
                send_payment_reminder(payment)
                
                # Mark reminder as sent
                mongo.db.recurring_payments.update_one(
                    {'_id': payment['_id']},
                    {'$set': {'reminder_sent': True}}
                )
                
    except Exception as e:
        print(f"Check Recurring Payments Error: {e}")

def send_payment_reminder(payment):
    """Send payment reminder to user"""
    try:
        user = mongo.db.users.find_one({'name': payment['username']})
        
        subject = f"Payment Reminder: {payment['payment_type']}"
        body = f"""
        Dear {user['name']},
        
        This is a reminder about your upcoming payment:
        
        Type: {payment['payment_type']}
        Amount: Rs{payment['amount']}
        Description: {payment['description']}
        Due Date: {payment['due_date'].strftime('%d %b %Y')}
        Days Until Due: {(payment['due_date'] - datetime.now()).days}
        
        Please ensure timely payment.
        
        Best regards,
        YourTreasurer
        """
        
        msg = Message(
            subject,
            sender=app.config['MAIL_USERNAME'],
            recipients=[user.get('email', app.config['MAIL_USERNAME'])]
        )
        msg.body = body
        
        # Send in background
        threading.Thread(target=send_async_email, args=(app, msg)).start()
        
    except Exception as e:
        print(f"Send Payment Reminder Error: {e}")

@app.errorhandler(413)
def request_entity_too_large(error):
    return "<h1>Receipt file is too large!</h1><p>Please keep your screenshot under 5MB.</p><a href='/my_expenses'>Try Again</a>", 413

if __name__ == '__main__':
    app.run(debug=True, port=5000)
