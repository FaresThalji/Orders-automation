from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import asyncio
import aiohttp
import time
import os
import webbrowser
import threading

app = Flask(__name__)
app.secret_key = os.urandom(24)

async def authenticate_deposco(http_session, company, username, password):
    url = "https://dax.deposco.com/deposco/resources/nonsecure/authenticate"
    payload = {"company": company, "username": username, "password": password, "isMobile": False}
    headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json"}
    
    try:
        async with http_session.post(url, json=payload, headers=headers, timeout=10) as response:
            if response.status != 200:
                return None, f"Login Failed (HTTP {response.status})"
            data = await response.json(content_type=None)
            token = data.get('X-Auth-Token')
            return (token, "Success") if token else (None, "Token not found")
    except Exception as e:
        return None, f"Connection Error: {str(e)}"

async def fetch_order(http_session, order_number, token, semaphore):
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "authorization": f"Bearer {token}"
    }
    
    async with semaphore:
        try:
            # 1. Quick Search to find Order ID and Created Date
            search_url = f"https://dax.deposco.com/deposco/resources/secure/search/quick_search?term={order_number}&entity=&timestamp={int(time.time() * 1000)}"
            async with http_session.get(search_url, headers=headers, timeout=10) as res_search:
                if res_search.status != 200:
                    return {"order": order_number, "category": "NOT_FOUND", "reason": f"Search HTTP {res_search.status}"}
                
                search_data = await res_search.json(content_type=None)
                if not search_data:
                    return {"order": order_number, "category": "NOT_FOUND", "reason": "Order does not exist"}
                    
                order_id = None
                created_date = "N/A"
                
                for entity in search_data:
                    if entity.get("name") == "OrderHeader":
                        results = entity.get("results", [])
                        if results:
                            order_data = results[0]
                            order_id = order_data.get("id")
                            created_date = order_data.get("createdDate", "N/A")
                            break
                            
                if not order_id:
                    return {"order": order_number, "category": "NOT_FOUND", "reason": "No OrderHeader ID Found"}

            # 2. Extract Customer Order and Ship From via correct view_layout API
            customer_order = "N/A"
            ship_from = "N/A"
            
            header_url = f"https://dax.deposco.com/deposco/resources/secure/entity/view_layout/page_layout?entityName=OrderHeader&instanceId={order_id}&canEdit=true&showDefaultLayouts=true&timestamp={int(time.time() * 1000)}"
            
            async with http_session.get(header_url, headers=headers, timeout=10) as res_header:
                if res_header.status == 200:
                    header_data = await res_header.json(content_type=None)
                    
                    # Iterate exactly through the JSON structure you provided
                    for section in header_data.get("sections", []):
                        for attr in section.get("pageAttributes", []):
                            fname = attr.get("fieldName")
                            
                            # Grab the values if the keys exist
                            if fname == "customerOrderNumber" and "value" in attr:
                                customer_order = str(attr.get("value"))
                            elif fname == "shipFrom" and "value" in attr:
                                ship_from = str(attr.get("value"))

            # 3. Get Related Shipments
            related_url = "https://dax.deposco.com/deposco/resources/secure/entity/related_info"
            shipment_payload = {
                "instanceId": int(order_id), "relLayoutId": 198081, "page": 1, 
                "maxRows": 100, "maxRowsConsolidatedOHs": 5, "sortByColumns": [], 
                "useLayoutNumOfRows": False, "filterAttributes": [], "entityLabel": "Order Shipments"
            }
            async with http_session.post(related_url, headers=headers, json=shipment_payload, timeout=10) as res_ship:
                ship_data = await res_ship.json(content_type=None)
                shipments = ship_data.get("data", {}).get("response", [])
                
                if not shipments:
                    return {
                        "order": order_number, 
                        "customer_order": customer_order,
                        "ship_from": ship_from,
                        "created_date": created_date,
                        "category": "NO_TRACKING", 
                        "status": "Processing/Unshipped", 
                        "reason": "No shipments attached"
                    }
                    
                shipment_id = shipments[0].get("id")
                shipment_status = shipments[0].get("status", "Unknown")
                
                if not shipment_id:
                    return {
                        "order": order_number, 
                        "customer_order": customer_order,
                        "ship_from": ship_from,
                        "created_date": created_date,
                        "category": "NO_TRACKING", 
                        "status": shipment_status, 
                        "reason": "Shipment ID missing"
                    }

            # 4. Get Shipment Lines (Tracking)
            lines_payload = {
                "instanceId": int(shipment_id), "relLayoutId": 199376, "page": 1, 
                "maxRows": 100, "maxRowsConsolidatedOHs": 5, "sortByColumns": [], 
                "useLayoutNumOfRows": False, "filterAttributes": [], "entityLabel": "Shipment Line"
            }
            async with http_session.post(related_url, headers=headers, json=lines_payload, timeout=10) as res_lines:
                lines_data = await res_lines.json(content_type=None)
                rows = lines_data.get("data", {}).get("response", [])
                
                trackings = []
                for row in rows:
                    trk = row.get("trackingNumber", "")
                    if trk and trk not in trackings:
                        trackings.append(str(trk))
                        
                if not trackings:
                    return {
                        "order": order_number, 
                        "customer_order": customer_order,
                        "ship_from": ship_from,
                        "created_date": created_date,
                        "category": "NO_TRACKING", 
                        "status": shipment_status, 
                        "reason": "No tracking numbers on lines"
                    }
                    
                return {
                    "order": order_number, 
                    "customer_order": customer_order,
                    "ship_from": ship_from,
                    "created_date": created_date,
                    "category": "SHIPPED", 
                    "status": shipment_status, 
                    "tracking": " | ".join(trackings)
                }

        except Exception as e:
            return {"order": order_number, "category": "NOT_FOUND", "reason": f"Error: {type(e).__name__}"}

CONCURRENCY = 25

async def process_batch(company, username, password, order_numbers):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as http_session:
        token, auth_msg = await authenticate_deposco(http_session, company, username, password)
        if not token:
            return {"error": f"Authentication Failed: {auth_msg}"}
            
        tasks = [fetch_order(http_session, order, token, semaphore) for order in order_numbers]
        results = await asyncio.gather(*tasks)
        
        shipped = [r for r in results if r["category"] == "SHIPPED"]
        no_tracking = [r for r in results if r["category"] == "NO_TRACKING"]
        not_found = [r for r in results if r["category"] == "NOT_FOUND"]
        
        return {"shipped": shipped, "no_tracking": no_tracking, "not_found": not_found}

# --- Routing ---

@app.route("/")
def index():
    if "username" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/login", methods=["POST"])
async def login():
    data = request.json
    company = data.get("company", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    
    async with aiohttp.ClientSession() as http_session:
        token, auth_msg = await authenticate_deposco(http_session, company, username, password)
        if token:
            session["company"] = company
            session["username"] = username
            session["password"] = password
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": auth_msg}), 401

@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("index"))
    return render_template("dashboard.html", username=session["username"], company=session["company"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/api/extract", methods=["POST"])
async def extract():
    if "username" not in session:
        return jsonify({"error": "Not authenticated. Please log in again."}), 401
        
    data = request.json
    orders = [o.strip() for o in data.get("orders", "").replace(',', ' ').split() if o.strip()]
    
    if not orders:
        return jsonify({"error": "No valid orders provided"}), 400
        
    results = await process_batch(session["company"], session["username"], session["password"], orders)
    return jsonify(results)

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(debug=True, port=5000)
