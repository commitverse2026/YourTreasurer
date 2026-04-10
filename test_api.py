import requests
import json

# First login
login_data = {
    "name": "testuser",
    "password": "Test@1234"
}

session = requests.Session()
login_response = session.post("http://localhost:5000/login", json=login_data)
print("Login response:", login_response.json())

# Add recurring payment
payment_data = {
    "name": "Test EMI",
    "amount": 5000,
    "due_date": "2025-01-15",
    "remind_days_before": 3,
    "payment_type": "emi"
}

payment_response = session.post("http://localhost:5000/api/add_recurring_payment", json=payment_data)
print("Add payment response:", payment_response.json())

# Get payments
get_response = session.get("http://localhost:5000/api/get_recurring_payments")
print("Get payments response:", get_response.json())
