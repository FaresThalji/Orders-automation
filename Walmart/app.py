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

def get_view_payload(po_number):
    # This is the EXACT payload Deposco expects, with the filter injected into customerOrderNumber
    return {
        "view": {
            "id": 10644, "entityId": 5053, "companyId": 73, "userId": 2509, "groupId": 0,
            "text": "Rob Tracking numbers", "entityName": "OrderHeader", "entityClass": "com.deposco.domain.OrderHeader",
            "isOwn": True, "isShared": True, "bookmarkActive": False, "addActionLinks": True,
            "columns": [
                {"title": "Number", "name": "number", "fieldName": "number", "sortOrder": 0, "dataType": "Text", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 0, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104649, "subAttributeId": 0, "relatedToId": 0, "required": True, "readOnly": False, "custom": False, "searchable": True, "sortable": True, "businessKey": True, "isEntityTag": False, "allowNegative": False},
                {"title": "Updated Date", "name": "updatedDate", "fieldName": "updatedDate", "sortOrder": 0, "dataType": "DateTime", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 1, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104491, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": True, "custom": False, "searchable": False, "sortable": True, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                {"title": "Created Date", "name": "createdDate", "fieldName": "createdDate", "sortOrder": 0, "dataType": "DateTime", "returnDataType": "Text", "filtering": {}, "length": 25, "displayOrder": 2, "entityId": 5053, "entity": "com.deposco.domain.OrderHeader", "entityType": "Business", "entityName": "OrderHeader", "attributeId": 104561, "subAttributeId": 0, "relatedToId": 0, "required": False, "readOnly": True, "custom": False, "searchable": False, "sortable": True, "businessKey": False, "isEntityTag": False, "allowNegative": False},
                
                # INJECTED FILTER HERE: Search by PO Number (operator 5 = Starts With)
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
                        out_results.append({"order": po_number, "so_number": so_num, "ship_from": ship_from, "created_date": created_date, "category": "SHIPPED", "status": status, "tracking": final_trk})
                    else:
                        out_results.append({"order": po_number, "so_number": so_num, "ship_from": ship_from, "created_date": created_date, "category": "NO_TRACKING", "status": status, "reason": "No tracking info found"})
                        
                return out_results

        except Exception as e:
            return [{"order": po_number, "category": "NOT_FOUND", "reason": f"Error: {type(e).__name__}"}]

CONCURRENCY = 30

async def process_batch(company, username, password, order_numbers):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as http_session:
        token, auth_msg = await authenticate_deposco(http_session, company, username, password)
        if not token: return {"error": f"Authentication Failed: {auth_msg}"}
            
        tasks = [fetch_order(http_session, order, token, semaphore) for order in order_numbers]
        results = await asyncio.gather(*tasks)
        
        flat_results = []
        for sublist in results: flat_results.extend(sublist)
        
        shipped = [r for r in flat_results if r["category"] == "SHIPPED"]
        no_tracking = [r for r in flat_results if r["category"] == "NO_TRACKING"]
        not_found = [r for r in flat_results if r["category"] == "NOT_FOUND"]
        
        return {"shipped": shipped, "no_tracking": no_tracking, "not_found": not_found}

# --- Routing ---
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
