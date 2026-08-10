from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import asyncio
import aiohttp
import time
import os
import webbrowser
import threading
import json
import re
import uuid

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- FEDEX CONFIGURATION ---
FEDEX_CLIENT_ID = os.environ.get("FEDEX_CLIENT_ID", "YOUR_FEDEX_CLIENT_ID_HERE")
FEDEX_CLIENT_SECRET = os.environ.get("FEDEX_CLIENT_SECRET", "YOUR_FEDEX_CLIENT_SECRET_HERE")

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

# --- FEDEX LIVE TRACKING ENGINE ---
async def get_fedex_token(http_session, client_id, client_secret):
    if not client_id or "YOUR_FEDEX" in client_id:
        return None

    url = "https://apis.fedex.com/oauth/token"
    payload = f"grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        async with http_session.post(url, data=payload, headers=headers, timeout=10) as res:
            if res.status == 200:
                data = await res.json()
                return data.get("access_token")
    except Exception:
        pass
    return None

async def fetch_fedex_statuses(http_session, tracking_numbers, fedex_token):
    if not tracking_numbers or not fedex_token:
        return {}

    url = "https://apis.fedex.com/track/v1/trackingnumbers"
    headers = {
        "Authorization": f"Bearer {fedex_token}",
        "Content-Type": "application/json"
    }

    status_map = {}
    chunk_size = 30 

    for i in range(0, len(tracking_numbers), chunk_size):
        chunk = tracking_numbers[i:i + chunk_size]
        payload = {
            "includeDetailedScans": False,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": trk}} for trk in chunk]
        }

        try:
            async with http_session.post(url, json=payload, headers=headers, timeout=10) as res:
                if res.status == 200:
                    data = await res.json()
                    results = data.get("output", {}).get("completeTrackResults", [])
                    for item in results:
                        tracks = item.get("trackResults", [])
                        if tracks:
                            t_info = tracks[0].get("trackingNumberInfo", {})
                            t_num = t_info.get("trackingNumber")
                            desc = tracks[0].get("latestStatusDetail", {}).get("description")
                            if t_num and desc:
                                status_map[t_num] = desc.upper()
        except Exception:
            pass

    return status_map

# --- WALMART AUTOMATION ENGINE ---
def parse_cookie_input(cookie_input):
    try:
        cookies = json.loads(cookie_input)
        if isinstance(cookies, list):
            return "; ".join([f"{c['name']}={c['value']}" for c in cookies if 'name' in c and 'value' in c])
    except Exception:
        pass
    return cookie_input

async def async_update_walmart_tracking(po_number, tracking_number_str, session_cookie):
    url = "https://seller.walmart.com/aurora/v2/auroraOrderService/gql"
    
    # Intelligently split tracking numbers (handles commas, tabs, spaces)
    trackings = [t.strip() for t in re.split(r'[,\s\t]+', str(tracking_number_str)) if t.strip()]
    if not trackings:
        return {"success": False, "error": "No valid tracking numbers parsed"}

    xsrf_match = re.search(r'XSRF-TOKEN=([^;]+)', session_cookie)
    xsrf_token = xsrf_match.group(1) if xsrf_match else ""

    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "cookie": session_cookie,
        "origin": "https://seller.walmart.com",
        "referer": f"https://seller.walmart.com/orders/manage-orders?orderGroups=Unshipped&limit=200&poNumber={po_number}",
        "sec-ch-ua": "\"Not;A=Brand\";v=\"8\", \"Chromium\";v=\"150\", \"Google Chrome\";v=\"150\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "wm_aurora.locale": "en-US",
        "wm_aurora.market": "US",
        "wm_svc.name": "API"
    }

    if xsrf_token:
        headers["x-xsrf-token"] = xsrf_token

    async with aiohttp.ClientSession() as session:
        # STEP 1: Fetch PO details to count lines
        headers["pxqueryname"] = "get_orders_getAllOrders,get_orders_getAllOrders"
        headers["wm_qos.correlation_id"] = str(uuid.uuid4())
        
        fetch_query = """query get_orders_getAllOrders($params: SearchParams) {
          get_orders_getAllOrders(searchParams: $params) {
             orderInfo { purchaseOrders { poLines { lineId primeLineNo quantity } } }
          }
        }"""
        fetch_payload = {"query": fetch_query, "variables": {"params": {"orderGroups": "All", "isDetailPage": True, "poNumber": str(po_number).strip()}}}
        
        po_lines = []
        try:
            async with session.post(url, json=fetch_payload, headers=headers, timeout=15) as res:
                if res.status == 200:
                    data = await res.json()
                    po_lines = data.get("data", {}).get("get_orders_getAllOrders", {}).get("orderInfo", {}).get("purchaseOrders", [{}])[0].get("poLines", [])
        except Exception:
            pass

        # Fallback if fetch fails
        if not po_lines:
            po_lines = [{"lineId": ["1"], "primeLineNo": [1], "quantity": 1}]

        # STEP 2: Intelligent Routing Matrix
        poLineRequestDTOList = []
        unused_trackings = []
        num_lines = len(po_lines)
        num_tracks = len(trackings)

        if num_tracks == 1:
            # 1 Tracking -> Apply to all SKUs
            for line in po_lines:
                poLineRequestDTOList.append({
                    "lineIds": [str(line.get("lineId", ["1"])[0])],
                    "primeLineNo": [int(line.get("primeLineNo", [1])[0])],
                    "updatedQuantity": str(line.get("quantity", "1")),
                    "updatedStatus": "Shipped",
                    "intentToCancelOverride": False,
                    "shipmentInfo": {
                        "carrierServiceCode": "FDX-ST",
                        "trackingNo": trackings[0]
                    }
                })
        else:
            # Multi Tracking -> Map 1:1, collect leftovers
            for i, line in enumerate(po_lines):
                trk = trackings[i] if i < num_tracks else trackings[-1]
                poLineRequestDTOList.append({
                    "lineIds": [str(line.get("lineId", ["1"])[0])],
                    "primeLineNo": [int(line.get("primeLineNo", [1])[0])],
                    "updatedQuantity": str(line.get("quantity", "1")),
                    "updatedStatus": "Shipped",
                    "intentToCancelOverride": False,
                    "shipmentInfo": {
                        "carrierServiceCode": "FDX-ST",
                        "trackingNo": trk
                    }
                })
            if num_tracks > num_lines:
                unused_trackings = trackings[num_lines:]

        # STEP 3: Execute Update
        headers["pxqueryname"] = "update_orders_updateOrder,update_orders_updateOrder"
        headers["wm_qos.correlation_id"] = str(uuid.uuid4())
        
        update_query = """mutation update_orders_updateOrder($input: [PoUpdateRequest]) {
          update_orders_updateOrder(poUpdateRequest: $input) {
             poUpdateResponseStatus {
              poNumber
              updateResponsePoLineList { status statusDescription lineIds error }
              errorList
            }
          }
        }"""
        update_payload = {"query": update_query, "variables": {"input": [{"poNumber": str(po_number).strip(), "isWCPOrder": False, "poLineRequestDTOList": poLineRequestDTOList}]}}

        try:
            async with session.post(url, json=update_payload, headers=headers, timeout=15) as res:
                if res.status != 200:
                    return {"success": False, "error": f"HTTP {res.status}"}
                
                update_data = await res.json()
                if "errors" in update_data:
                    return {"success": False, "error": str(update_data["errors"])}
                    
                try:
                    resp_status = update_data["data"]["update_orders_updateOrder"]["poUpdateResponseStatus"]
                    if resp_status.get("errorList"): return {"success": False, "error": str(resp_status["errorList"])}
                    for l in resp_status.get("updateResponsePoLineList", []):
                        if l.get("error"): return {"success": False, "error": l.get("statusDescription")}
                except Exception:
                    pass
                    
                return {"success": True, "po_number": po_number, "tracking": trackings, "unused_trackings": unused_trackings}
        except Exception as e:
            return {"success": False, "error": str(e)}

@app.route('/api/update-walmart', methods=['POST'])
async def update_walmart_order():
    if "username" not in session: 
        return jsonify({"success": False, "error": "Not authenticated."}), 401

    data = request.get_json()
    po_number = data.get('po_number')
    tracking_number = data.get('tracking_number')
    raw_cookie = data.get('session_cookie')
    
    if not all([po_number, tracking_number, raw_cookie]):
        return jsonify({"success": False, "error": "Missing required data"}), 400
        
    session_cookie = parse_cookie_input(raw_cookie)
    result = await async_update_walmart_tracking(po_number, tracking_number, session_cookie)
    
    return jsonify(result), 200

# --- DEPOSCO VIEW PAYLOAD ---
def get_view_payload(po_number):
    return {
        "view": {
            "id": 10644, "entityId": 5053, "companyId": 73, "userId": 2509, "groupId": 0,
            "text": "Rob Tracking numbers", "entityName": "OrderHeader", "entityClass": "com.deposco.domain.OrderHeader",
            "isOwn": True, "isShared": True, "bookmarkActive": False, "addActionLinks": True,
            "columns": [
                {"title": "Number", "name": "number", "fieldName": "number", "sortOrder": 0, "dataType": "Text", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 0, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104649, "subAttributeId": 0, "relatedToId": 0, "required": True, "readOnly": False, "custom": False, "searchable": True, "sortable": True, "businessKey": True, "isEntityTag": False, "allowNegative": False},
                {"title": "Updated Date", "name": "updatedDate", "fieldName": "updatedDate", "sortOrder": 0, "dataType": "DateTime", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 1, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104491, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": True, "custom": False, "searchable": False, "sortable": True, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Created Date", "name": "createdDate", "fieldName": "createdDate", "sortOrder": 0, "dataType": "DateTime", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 2, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104561, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": True, "custom": False, "searchable": False, "sortable": True, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Customer Order Number", "name": "customerOrderNumber", "fieldName": "customerOrderNumber", "sortOrder": 0, "dataType": "Text", "returnDataType": "Text", "filtering": {"filterString": po_number, "operator": 5, "filterStrings": []}, "length": 50, "displayOrder": 3, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 114035, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": False, "custom": False, "searchable": True, "sortable": False, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Current Status", "name": "currentStatus", "fieldName": "currentStatus", "sortOrder": 0, "dataType": "Enumeration", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 5, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104555, "subAttributeId": 0, "relatedToId": 0, "required": True, "readOnly": False, "custom": False, "searchable": False, "sortable": True, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Ship From Facility - Number", "name": "shipFrom.number", "fieldName": "shipFrom", "sortOrder": 0, "dataType": "RelatedEntity", "returnDataType": "Text", "filtering": {}, "relatedToEntity": "com.deposco.domain.Facility", "relatedToEntityName": "Facility", "relatedToEntityType": "Business", "relatedToFieldName": "number", "relatedToDataType": "Text", "length": 0, "displayOrder": 15, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 113601, "subAttributeId": 102369, "relatedToId": 4955, "required": True, "readOnly": False, "custom": False, "searchable": False, "sortable": True, "businessKey": True, "isEntityTag": False, "allowNegative": False},
                {"title": "Tracking Link(s)", "name": "baseTrackingLink", "fieldName": "baseTrackingLink", "sortOrder": 0, "dataType": "API", "returnDataType": "Text", "apiSql": "SELECT group_concat(concat( CASE WHEN c_.TRACKING_NUMBER IS NOT NULL THEN COALESCE(concat(ss_.SHIP_VENDOR, '=', ss_.FREIGHT_TYPE, '=', c_.TRACKING_NUMBER, ','),'') ELSE '' END , '', COALESCE(concat(ss_.SHIP_VENDOR, '=', ss_.FREIGHT_TYPE, '=', ch_.TRACKING_NUMBER), '') )) from SHIPMENT_ORDER_HEADER soh_ inner join SHIPMENT s_ on soh_.SHIPMENT_ID = s_.SHIPMENT_ID INNER JOIN SHIPPING_SERVICE ss_ ON ss_.ship_via = s_.SHIP_VIA LEFT JOIN CONTAINER c_ ON c_.shipment_id = s_.shipment_id LEFT JOIN CONTAINER_HIST ch_ ON ch_.shipment_id = s_.shipment_id WHERE soh_.ORDER_HEADER_ID = :id", "filtering": {}, "length": 400, "displayOrder": 11, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 113843, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": True, "custom": False, "searchable": False, "sortable": False, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Tracking Number", "name": "billToPhone2", "fieldName": "billToPhone2", "sortOrder": 0, "dataType": "Text", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 70, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104523, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": False, "custom": False, "searchable": False, "sortable": False, "businessKey": False, "isEntityTag": False, "allowNegative": False}
            ],
            "filterAttributes": [], "numberOfRows": 100
        },
        "page": 1, "rowsPerPage": 100, "uiRowsPerPage": -1, "useLabel": False, "isExport": False, "translateEnums": False
    }

async def fetch_order(http_session, po_number, token, semaphore):
    headers = {"accept": "application/json, text/plain, */*", "content-type": "application/json", "authorization": f"Bearer {token}"}
    
    async with semaphore:
        try:
            view_api_url = "https://dax.deposco.com/deposco/resources/secure/entity"
            payload = get_view_payload(po_number)
            
            async with http_session.post(view_api_url, headers=headers, json=payload, timeout=15) as res_view:
                if res_view.status != 200:
                    return [{"order": po_number, "category": "NOT_FOUND", "reason": f"View HTTP {res_view.status}"}]
                    
                view_data = await res_view.json(content_type=None)
                records = view_data.get("response", [])
                
                if not records:
                    return [{"order": po_number, "category": "NOT_FOUND", "reason": "Not Found in Deposco"}]
                    
                out_results = []
                for rec in records:
                    so_num = rec.get("number", "N/A")
                    cust_order = rec.get("customerOrderNumber") or "N/A"
                    created_date = rec.get("createdDate", "N/A")
                    ship_from = rec.get("shipFrom.number", "N/A")
                    status = rec.get("currentStatus", "Unknown")
                    
                    base_track = rec.get("baseTrackingLink") or ""
                    fallback_track = rec.get("billToPhone2") or ""
                    
                    t_nums = []
                    if base_track:
                        for link in base_track.split(','):
                            if '=' in link: t_nums.append(link.split('=')[-1].strip())
                                
                    final_trk = " | ".join([t for t in t_nums if t])
                    if not final_trk and fallback_track: final_trk = fallback_track.strip()
                        
                    if final_trk:
                        out_results.append({
                            "order": po_number, 
                            "so_number": so_num, 
                            "customer_order": cust_order, 
                            "ship_from": ship_from, 
                            "created_date": created_date, 
                            "category": "SHIPPED", 
                            "status": status, 
                            "tracking": final_trk,
                            "raw_trackings": t_nums or ([fallback_track.strip()] if fallback_track else [])
                        })
                    else:
                        out_results.append({
                            "order": po_number, 
                            "so_number": so_num, 
                            "customer_order": cust_order, 
                            "ship_from": ship_from, 
                            "created_date": created_date, 
                            "category": "NO_TRACKING", 
                            "status": status, 
                            "reason": "No tracking info found",
                            "raw_trackings": []
                        })
                        
                return out_results

        except Exception as e:
            return [{"order": po_number, "category": "NOT_FOUND", "reason": f"Error: {type(e).__name__}"}]

CONCURRENCY = 30

async def process_batch(company, username, password, order_numbers):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as http_session:
        deposco_task = authenticate_deposco(http_session, company, username, password)
        fedex_task = get_fedex_token(http_session, FEDEX_CLIENT_ID, FEDEX_CLIENT_SECRET)
        
        token, fedex_token = await asyncio.gather(deposco_task, fedex_task)
        token_str, auth_msg = token
        
        if not token_str:
            return {"error": f"Authentication Failed: {auth_msg}"}
            
        tasks = [fetch_order(http_session, order, token_str, semaphore) for order in order_numbers]
        results = await asyncio.gather(*tasks)
        
        flat_results = []
        for sublist in results: flat_results.extend(sublist)

        shipped = [r for r in flat_results if r["category"] == "SHIPPED"]
        no_tracking = [r for r in flat_results if r["category"] == "NO_TRACKING"]
        not_found = [r for r in flat_results if r["category"] == "NOT_FOUND"]

        if fedex_token and shipped:
            all_tracking_numbers = []
            for item in shipped:
                all_tracking_numbers.extend(item.get("raw_trackings", []))

            unique_trackings = list(set(all_tracking_numbers))
            
            if unique_trackings:
                live_statuses = await fetch_fedex_statuses(http_session, unique_trackings, fedex_token)
                for item in shipped:
                    item_trackings = item.get("raw_trackings", [])
                    matched_statuses = [live_statuses.get(trk) for trk in item_trackings if live_statuses.get(trk)]
                    if matched_statuses:
                        item["status"] = " | ".join(set(matched_statuses))

        return {"shipped": shipped, "no_tracking": no_tracking, "not_found": not_found}

# --- ROUTING ---
@app.route("/")
def index():
    if "username" in session: return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/login", methods=["POST"])
async def login():
    data = request.json
    async with aiohttp.ClientSession() as http_session:
        token, auth_msg = await authenticate_deposco(http_session, data.get("company", "").strip(), data.get("username", "").strip(), data.get("password", "").strip())
        if token:
            session.update({"company": data.get("company", "").strip(), "username": data.get("username", "").strip(), "password": data.get("password", "").strip()})
            return jsonify({"success": True})
        return jsonify({"success": False, "error": auth_msg}), 401

@app.route("/dashboard")
def dashboard():
    if "username" not in session: return redirect(url_for("index"))
    return render_template("dashboard.html", username=session["username"], company=session["company"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/api/extract", methods=["POST"])
async def extract():
    if "username" not in session: return jsonify({"error": "Not authenticated. Please log in again."}), 401
    orders = [o.strip() for o in request.json.get("orders", "").replace(',', ' ').split() if o.strip()]
    if not orders: return jsonify({"error": "No valid orders provided"}), 400
    results = await process_batch(session["company"], session["username"], session["password"], orders)
    return jsonify(results)

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(debug=True, port=5000)
