import requests
import time
from datetime import datetime

def test_yourtreasurer_end_to_end():
    """Comprehensive end-to-end testing of YourTreasurer application"""
    base_url = "http://127.0.0.1:5000"
    
    print("=== YourTreasurer End-to-End Testing ===")
    print(f"Testing on: {base_url}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Test 1: Application Health Check
    print("1. Testing Application Health...")
    try:
        response = requests.get(base_url, timeout=5)
        if response.status_code == 200:
            print("   [PASS] Application is running and responding")
        else:
            print(f"   [FAIL] Application returned status: {response.status_code}")
            return False
    except Exception as e:
        print(f"   [FAIL] Cannot connect to application: {e}")
        return False
    
    # Test 2: Profile Page Access (Not Logged In)
    print("\n2. Testing Profile Page (Not Logged In)...")
    try:
        response = requests.get(f"{base_url}/my_profile", timeout=5)
        if response.status_code == 200:
            print("   [PASS] Profile page accessible for non-logged-in users")
            if "Create Account" in response.text and "Existing User Login" in response.text:
                print("   [PASS] Registration and login forms are present")
            else:
                print("   [FAIL] Registration/login forms not found")
                return False
        else:
            print(f"   [FAIL] Profile page returned status: {response.status_code}")
            return False
    except Exception as e:
        print(f"   [FAIL] Profile page test failed: {e}")
        return False
    
    # Test 3: User Registration
    print("\n3. Testing User Registration...")
    test_user = {
        'name': f'testuser{int(time.time())}',
        'password': 'testpass123',
        'monthly_limit': '5000'
    }
    
    try:
        response = requests.post(f"{base_url}/register", data=test_user, timeout=10)
        if response.status_code == 302:
            print("   [PASS] User registration successful (redirecting)")
            
            # Test 4: User Login
            print("\n4. Testing User Login...")
            login_data = {
                'name': test_user['name'],
                'password': test_user['password']
            }
            
            # Create a session to maintain login state
            session = requests.Session()
            response = session.post(f"{base_url}/authenticate", data=login_data, timeout=10)
            
            if response.status_code == 302:
                print("   [PASS] User login successful (redirecting)")
                
                # Test 5: Profile Page (Logged In)
                print("\n5. Testing Profile Page (Logged In)...")
                response = session.get(f"{base_url}/my_profile", timeout=10)
                
                if response.status_code == 200:
                    print("   [PASS] Profile page accessible for logged-in users")
                    if test_user['name'] in response.text:
                        print("   [PASS] User name displayed correctly")
                    else:
                        print("   [FAIL] User name not found in profile")
                        return False
                    
                    if "Rs." in response.text or "Rs" in response.text or "rupees" in response.text.lower():
                        print("   [PASS] Rupee currency displayed in profile")
                    else:
                        print("   [FAIL] Rupee currency not found in profile")
                        return False
                    
                    if "Edit Profile" in response.text:
                        print("   [PASS] Edit Profile button present")
                    else:
                        print("   [FAIL] Edit Profile button not found")
                        return False
                else:
                    print(f"   [FAIL] Profile page returned status: {response.status_code}")
                    return False
                
                # Test 6: Profile Update
                print("\n6. Testing Profile Update...")
                update_data = {
                    'name': test_user['name'],
                    'monthly_limit': '6000',
                    'password': 'newpass123'
                }
                
                response = session.post(f"{base_url}/update_profile", data=update_data, timeout=10)
                
                if response.status_code == 302:
                    print("   [PASS] Profile update successful (redirecting)")
                    
                    # Verify the update
                    response = session.get(f"{base_url}/my_profile", timeout=10)
                    if "6000" in response.text or "6,000" in response.text:
                        print("   [PASS] Monthly budget updated correctly")
                    else:
                        print("   [FAIL] Monthly budget not updated")
                        return False
                else:
                    print(f"   [FAIL] Profile update returned status: {response.status_code}")
                    return False
                
                # Test 7: Expenses Page
                print("\n7. Testing Expenses Page...")
                response = session.get(f"{base_url}/my_expenses", timeout=10)
                
                if response.status_code == 200:
                    print("   [PASS] Expenses page accessible")
                    if "Rs." in response.text or "Rs" in response.text or "rupees" in response.text.lower():
                        print("   [PASS] Rupee currency displayed in expenses")
                    else:
                        print("   [FAIL] Rupee currency not found in expenses")
                        return False
                    
                    if "minlength=\"3\"" in response.text:
                        print("   [PASS] Description minimum 3 character validation present")
                    else:
                        print("   [FAIL] Description validation not found")
                        return False
                else:
                    print(f"   [FAIL] Expenses page returned status: {response.status_code}")
                    return False
                
                # Test 8: Navigation
                print("\n8. Testing Navigation...")
                pages_to_test = [
                    ('/', 'Home'),
                    ('/analysis', 'Analysis'),
                    ('/interval_spend', 'Interval Spend'),
                    ('/about_us', 'About Us')
                ]
                
                for page_path, page_name in pages_to_test:
                    response = session.get(f"{base_url}{page_path}", timeout=10)
                    if response.status_code == 200:
                        print(f"   [PASS] {page_name} page accessible")
                    else:
                        print(f"   [FAIL] {page_name} page returned status: {response.status_code}")
                        return False
                
                # Test 9: Logout
                print("\n9. Testing Logout...")
                response = session.get(f"{base_url}/logout", timeout=10)
                if response.status_code == 302:
                    print("   [PASS] Logout successful (redirecting)")
                else:
                    print(f"   [FAIL] Logout returned status: {response.status_code}")
                    return False
                
            else:
                print(f"   [FAIL] Login returned status: {response.status_code}")
                return False
        else:
            print(f"   [FAIL] Registration returned status: {response.status_code}")
            return False
    except Exception as e:
        print(f"   [FAIL] Registration/Login test failed: {e}")
        return False
    
    print("\n=== All Tests Passed Successfully! ===")
    print("YourTreasurer is running flawlessly end-to-end!")
    return True

if __name__ == "__main__":
    success = test_yourtreasurer_end_to_end()
    if success:
        print("\n[SUCCESS] YourTreasurer is ready for production!")
    else:
        print("\n[FAILURE] Some tests failed. Please check the issues above.")
