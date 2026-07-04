import frappe
import json
import requests
import gzip
import base64
from frappe import _
from datetime import datetime, timezone, timedelta
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from autozoneura.autozoneura.background_tasks.encryption import encrypt_dynamic_json

eat_timezone = timezone(timedelta(hours=3))


def get_efris_settings():
    """Get EFRIS settings"""
    efris_settings = frappe.get_single("EFRIS Settings")
    if not efris_settings.is_active:
        frappe.throw(_("EFRIS Settings disabled"))
    if not efris_settings.device_number or not efris_settings.tin or not efris_settings.server_url:
        frappe.throw(_("EFRIS Settings are incomplete"))
    if not efris_settings.aes_key:
        frappe.throw(_("AES key not found"))
    return {
        "url": efris_settings.server_url,
        "tin": efris_settings.tin,
        "device_no": efris_settings.device_number,
        "brn": efris_settings.brn or "",
        "aes_key": efris_settings.aes_key
    }


def log_integration_request(status, url, headers, data, response, service="", aes_key=""):
    """Log integration request"""
    try:
        frappe.get_doc({
            "doctype": "Integration Request",
            "integration_type": "Remote",
            "integration_request_service": service,
            "is_remote_request": True,
            "method": "POST",
            "status": status,
            "custom_aes_key": aes_key or "",
            "url": url,
            "request_headers": json.dumps(headers, indent=4),
            "data": json.dumps(data, indent=4),
            "output": json.dumps(response, indent=4),
            "execution_time": datetime.now(eat_timezone).strftime("%Y-%m-%d %H:%M:%S EAT")
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass


def decrypt_aes_content(encrypted_content, aes_key):
    """
    Decrypt AES encrypted content from EFRIS
    Order: Base64 → GZIP → AES → JSON
    """
    try:
        import gzip
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        
        aes_key_bytes = bytes.fromhex(aes_key)
        
        # Step 1: Base64 decode
        compressed_bytes = base64.b64decode(encrypted_content)
        
        # Step 2: GZIP decompress with fallback
        try:
            encrypted_bytes = gzip.decompress(compressed_bytes)
        except Exception as gzip_error:
            # Try removing trailing bytes if gzip fails
            for i in range(1, 5):
                try:
                    encrypted_bytes = gzip.decompress(compressed_bytes[:-i])
                    break
                except:
                    continue
            else:
                frappe.log_error(f"GZIP decompress failed: {str(gzip_error)}", "EFRIS GZIP Error")
                raise Exception(f"GZIP decompress failed: {str(gzip_error)}")
        
        # Step 3: Handle AES block alignment
        remainder = len(encrypted_bytes) % 16
        if remainder != 0:
            encrypted_bytes = encrypted_bytes[:-remainder]
        
        # Step 4: AES decrypt (ECB mode)
        cipher = AES.new(aes_key_bytes, AES.MODE_ECB)
        decrypted_padded = cipher.decrypt(encrypted_bytes)
        
        # Step 5: Remove PKCS7 padding
        try:
            decrypted_data = unpad(decrypted_padded, AES.block_size)
        except ValueError:
            decrypted_data = decrypted_padded
        
        # Step 6: Parse JSON
        try:
            result = json.loads(decrypted_data.decode('utf-8'))
        except UnicodeDecodeError:
            result = json.loads(decrypted_data.decode('latin-1'))
        
        return result
        
    except Exception as e:
        frappe.log_error(f"Decryption error: {str(e)}", "EFRIS Decryption")
        frappe.throw(_("Failed to decrypt EFRIS response: {0}").format(str(e)))


def build_t127_request(payload, settings):
    """Build T127 request with encryption - T127 returns all configured items"""
    # Encrypt payload
    encrypted_result = encrypt_dynamic_json(payload)
    if not encrypted_result.get("success"):
        frappe.throw(_(f"Encryption failed: {encrypted_result.get('error')}"))
    
    # Generate unique IDs
    data_exchange_id = frappe.generate_hash(length=32)
    current_time = datetime.now(eat_timezone).strftime("%Y-%m-%d %H:%M:%S")
    
    # Build request
    request_data = {
        "data": {
            "content": encrypted_result["encrypted_content"],
            "signature": encrypted_result["signature"],
            "dataDescription": {
                "codeType": "0",
                "encryptCode": "1",
                "zipCode": "0"
            }
        },
        "globalInfo": {
            "appId": "AP04",
            "version": "1.1.20191201",
            "dataExchangeId": data_exchange_id,
            "interfaceCode": "T127",
            "requestCode": "TP",
            "requestTime": current_time,
            "responseCode": "TA",
            "userName": "admin",
            "deviceMAC": "B47720524158",
            "deviceNo": settings["device_no"],
            "tin": settings["tin"],
            "brn": settings["brn"].strip().lstrip("/") if settings["brn"] else "",
            "taxpayerID": "999000002030357",
            "longitude": "32.61665",
            "latitude": "0.36601",
            "agentType": "0",
            "extendField": {
                "responseDateFormat": "dd/MM/yyyy",
                "responseTimeFormat": "dd/MM/yyyy HH:mm:ss",
                "referenceNo": frappe.generate_hash(length=14),
                "operatorName": frappe.session.user
            }
        },
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": ""
        }
    }
    return request_data, encrypted_result.get("aes_key", "")


def send_efris_request(server_url, request_data, aes_key=""):
    """Send request to EFRIS server"""
    headers = {"Content-Type": "application/json"}
    
    try:
        response = requests.post(server_url, json=request_data, headers=headers, timeout=60)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.Timeout:
        log_integration_request("Failed", server_url, headers, request_data, {}, "T127 Stock Query", aes_key=aes_key)
        frappe.throw(_("EFRIS request timed out. Please try again."))
    except requests.exceptions.RequestException as e:
        log_integration_request("Failed", server_url, headers, request_data, {}, "T127 Stock Query", aes_key=aes_key)
        frappe.throw(_(f"EFRIS request failed: {str(e)}"))


def get_efris_stock_all(settings):
    """Query ALL configured items from EFRIS using T127 interface"""
    payload = {
        "pageNo": "",
        "pageSize": ""
    }
    
    request_data, aes_key_used = build_t127_request(payload, settings)
    
    try:
        response_data = send_efris_request(settings['url'], request_data, aes_key=aes_key_used)
    except Exception as e:
        frappe.throw(_(f"EFRIS request failed: {str(e)}"))
    
    log_integration_request('Completed', settings['url'], {}, request_data, response_data, "T127 Stock Query", aes_key=aes_key_used)
    
    return_message = response_data.get("returnStateInfo", {}).get("returnMessage", "")
    
    if return_message != "SUCCESS":
        frappe.throw(_(f"EFRIS returned error: {return_message}"))
    
    data = response_data.get("data", {})
    encrypted_content = data.get("content", "")
    
    if not encrypted_content:
        frappe.throw(_("No data returned from EFRIS"))
    
    decrypted_data = decrypt_aes_content(
        encrypted_content,
        aes_key_used or settings["aes_key"],
    )
    
    return decrypted_data


@frappe.whitelist()
def validate_invoice_stock_before_efris(invoice_name):
    """
    Validate Sales Invoice items against EFRIS stock and update unit prices.
    Uses item_code from Sales Invoice Item as goodsCode for EFRIS lookup.
    """
    try:
        # Get Sales Invoice
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        
        if not invoice.items:
            frappe.throw(_("No items found in invoice"))
        
        # Get EFRIS settings
        settings = get_efris_settings()
        
        # Query ALL stock from EFRIS
        frappe.msgprint("Querying EFRIS stock...", alert=True)
        stock_response = get_efris_stock_all(settings)
        
        # Build stock lookup from EFRIS response
        stock_lookup = {}
        records = stock_response.get('records', [])
        
        for record in records:
            goods_code = record.get('goodsCode', '')
            stock_qty = float(record.get('stock', 0))
            unit_price = record.get('unitPrice', 0)
            stock_lookup[goods_code] = {
                'stock': stock_qty,
                'goods_name': record.get('goodsName', ''),
                'unit_price': float(unit_price) if unit_price else 0
            }
        
        # Build items map from invoice
        items_map = {}
        items_to_update = []
        
        for item in invoice.items:
            goods_code = item.custom_efris_product_code
            item_name = item.custom_efris_item_name
            
            if not goods_code:
                frappe.throw(_(f"Item row {item.idx} has no EFRIS product code"))
            
            if not item_name:
                frappe.throw(_(f"Item row {item.idx} has no EFRIS item name"))
            
            # Store item info
            if goods_code in items_map:
                items_map[goods_code]['qty'] += item.qty
                items_map[goods_code]['rows'].append(item.idx)
                items_map[goods_code]['item_names'].append(item.item_name)
            else:
                items_map[goods_code] = {
                    'item_name': item_name,
                    'qty': item.qty,
                    'rows': [item.idx],
                    'item_names': [item.item_name],
                    'current_rate': item.rate
                }
            
            # Check if item exists in EFRIS and update unit price
            if goods_code in stock_lookup:
                efris_unit_price = stock_lookup[goods_code]['unit_price']
                if efris_unit_price and efris_unit_price != item.custom_efris_unit_price:
                    items_to_update.append({
                        'idx': item.idx,
                        'goods_code': goods_code,
                        'current_price': item.custom_efris_unit_price,
                        'efris_price': efris_unit_price
                    })
        
        # Update unit prices in invoice items
        if items_to_update:
            frappe.msgprint(f"Updating {len(items_to_update)} item(s) with EFRIS unit prices...", alert=True)
            
            for update_info in items_to_update:
                for item in invoice.items:
                    if item.idx == update_info['idx']:
                        item.custom_efris_unit_price = update_info['efris_price']
                        # Optional: Also update the rate field
                        # item.rate = update_info['efris_price']
                        # item.amount = item.qty * update_info['efris_price']
                        break
            
            # Save the invoice with updated prices
            invoice.save(ignore_permissions=True)
            frappe.db.commit()
            frappe.msgprint("Unit prices updated from EFRIS!", indicator='green')
        
        # Validate stock
        out_of_stock = []
        sufficient_stock = []
        missing_items = []
        
        for goods_code, item_info in items_map.items():
            item_name = item_info['item_name']
            required_qty = item_info['qty']
            
            if goods_code not in stock_lookup:
                missing_items.append({
                    'goods_code': goods_code,
                    'item_name': item_name,
                    'required': required_qty
                })
                continue
            
            available_stock = stock_lookup[goods_code]['stock']
            
            if available_stock < required_qty:
                shortage = required_qty - available_stock
                out_of_stock.append({
                    'goods_code': goods_code,
                    'item_name': item_name,
                    'required': required_qty,
                    'available': available_stock,
                    'shortage': shortage
                })
            else:
                sufficient_stock.append({
                    'goods_code': goods_code,
                    'item_name': item_name,
                    'required': required_qty,
                    'available': available_stock
                })
        
        # Display success messages
        if sufficient_stock:
            success_items = "<br>".join([
                f" {item['goods_code']}: <span style='color: green; font-weight: bold;'>{item['available']}</span> available"
                for item in sufficient_stock
            ])
            frappe.msgprint(success_items, title="Validation Passed", indicator='green')
        
        # If any items missing or out of stock, throw error
        if missing_items or out_of_stock:
            error_details = []
            
            if missing_items:
                error_details.append("<h5>Items Not Found in EFRIS:</h5>")
                error_details.append("<table class='table table-bordered'><tr><th>Item Code</th><th>Item Name</th><th>Required Qty</th></tr>")
                for item in missing_items:
                    error_details.append(f"<tr><td>{item['goods_code']}</td><td>{item['item_name']}</td><td style='color: green; font-weight: bold;'>{item['required']}</td></tr>")
                error_details.append("</table><br>")
            
            if out_of_stock:
                error_details.append("<h5>Insufficient Stock:</h5>")
                error_details.append("<table class='table table-bordered'><tr><th>Item Code</th><th>Item Name</th><th>Required</th><th>Available</th><th>Short</th></tr>")
                for item in out_of_stock:
                    error_details.append(f"<tr><td>{item['goods_code']}</td><td>{item['item_name']}</td><td style='color: green; font-weight: bold;'>{item['required']}</td><td style='color: orange; font-weight: bold;'>{item['available']}</td><td style='color:red; font-weight: bold;'>{item['shortage']}</td></tr>")
                error_details.append("</table><br>")

            error_details.append("<p><b>Action Required:</b> Update stock in EFRIS before submitting.</p>")

            detailed_message = "".join(error_details)
            frappe.log_error(
                message=detailed_message,
                title="Stock Validation Failed"
            )
            return {
                "success": False,
                "invoice_name": invoice_name,
                "message": "Stock validation failed. Review the validation details below.",
                "details": detailed_message,
                "total_items": len(items_map),
                "sufficient_stock": len(sufficient_stock),
                "out_of_stock": len(out_of_stock),
                "missing_items": len(missing_items),
                "updated_prices": len(items_to_update)
            }
        
        # All items have sufficient stock
        return {
            "success": True,
            "invoice_name": invoice_name,
            "total_items": len(items_map),
            "sufficient_stock": len(sufficient_stock),
            "out_of_stock": len(out_of_stock),
            "missing_items": len(missing_items),
            "updated_prices": len(items_to_update)
        }
        
    except Exception as e:
        frappe.log_error(
            message=frappe.get_traceback(),
            title="Stock Validation Error"
        )
        return {
            "success": False,
            "invoice_name": invoice_name,
            "message": str(e)
        }


@frappe.whitelist()
def update_unit_price_from_efris(invoice_name):
    """
    Update only the unit prices from EFRIS without stock validation.
    Useful when you just want to fetch prices.
    """
    try:
        invoice = frappe.get_doc("Sales Invoice", invoice_name)
        
        if not invoice.items:
            frappe.throw(_("No items found in invoice"))
        
        settings = get_efris_settings()
        stock_response = get_efris_stock_all(settings)
        
        records = stock_response.get('records', [])
        price_lookup = {}
        
        for record in records:
            goods_code = record.get('goodsCode', '')
            unit_price = record.get('unitPrice', 0)
            if goods_code:
                price_lookup[goods_code] = float(unit_price) if unit_price else 0
        
        updated = 0
        for item in invoice.items:
            goods_code = item.custom_efris_product_code
            if goods_code and goods_code in price_lookup:
                item.custom_efris_unit_price = price_lookup[goods_code]
                updated += 1
        
        if updated > 0:
            invoice.save(ignore_permissions=True)
            frappe.db.commit()
            frappe.msgprint(f"Updated {updated} item(s) with EFRIS unit prices!", indicator='green')
        else:
            frappe.msgprint("No items were updated. Check that EFRIS product codes are set.", indicator='orange')
        
        return {
            "success": True,
            "invoice_name": invoice_name,
            "updated": updated,
            "total_items": len(invoice.items)
        }
        
    except Exception as e:
        frappe.log_error(
            message=frappe.get_traceback(),
            title="EFRIS Price Update Error"
        )
        return {
            "success": False,
            "invoice_name": invoice_name,
            "message": str(e)
        }
