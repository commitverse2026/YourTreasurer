import os
from pymongo import MongoClient
import certifi
from dotenv import load_dotenv
from datetime import datetime, timedelta
import random

load_dotenv()
uri = os.environ.get("MONGO_URI")
client = MongoClient(uri, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)
db = client.get_database() # Uses the one from the URI

# Get first user roughly
user = db.users.find_one()
if not user:
    print("No user found. Start app and log in first!")
    exit()

username = user['username']
categories = ["Junk Food", "Educational", "Travel", "Hostel Rent", "Lifestyle", "Healthy Food", "Other"]

print(f"Adding 15 extra records for {username}...")
new_records = []
for i in range(15):
    cat = random.choice(categories)
    amt = random.randint(50, 1500)
    is_loan = random.choice([True, False, False, False]) # 25% chance
    
    doc = {
        "username": username,
        "category": cat,
        "amount": float(amt),
        "is_loan": is_loan,
        "date": datetime.now() - timedelta(days=random.randint(1, 15)),
        "archived": False
    }
    if is_loan:
        doc["friend_email"] = f"friend{random.randint(10,99)}@example.com"
        doc["friend_relationship"] = "Colleague"
        doc["returned"] = random.choice([True, False])
        
    new_records.append(doc)

db.daily_expenses.insert_many(new_records)
print("Successfully inserted 15 more generic records into daily_expenses!")
