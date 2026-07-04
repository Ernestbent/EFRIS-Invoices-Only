import base64
import binascii
import gzip
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import frappe
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.serialization import pkcs12
from efris.efris.background_tasks.encryption import encrypt_dynamic_json


EAT_TIMEZONE = timezone(timedelta(hours=3))
T127_PAGE_SIZE = "10"


def log_integration_request(status, url, headers, data, response, service="T127", error="", aes_key=""):
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
            "error": error,
            "execution_time": datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S EAT"),
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass


def get_efris_settings():
    settings = frappe.get_single("EFRIS Settings")

    if not getattr(settings, "active", 0):
        frappe.throw("EFRIS Settings is disabled")

    required_fields = {
        "server_url": "Server URL",
        "device_number": "Device Number",
        "tin": "TIN",
        "aes_key": "AES Key",
        "private_key": "Private Key",
    }

    for fieldname, label in required_fields.items():
        if not getattr(settings, fieldname, None):
            frappe.throw(f"{label} is required in EFRIS Settings")

    return settings


def resolve_file_path(file_url):
    if not file_url:
        frappe.throw("No private key file was provided")

    file_name = file_url.split("/")[-1]

    private_path = os.path.join(frappe.get_site_path("private", "files"), file_name)
    if os.path.exists(private_path):
        return private_path

    public_path = os.path.join(frappe.get_site_path("public", "files"), file_name)
    if os.path.exists(public_path):
        return public_path

    frappe.throw(f"Private key file not found: {file_name}")


def get_private_key(pfx_path, password):
    with open(pfx_path, "rb") as handle:
        pfx_data = handle.read()

    password_bytes = (password or "").encode("utf-8")

    try:
        private_key, _, _ = pkcs12.load_key_and_certificates(
            pfx_data,
            password_bytes,
            default_backend(),
        )
    except Exception:
        private_key, _, _ = pkcs12.load_key_and_certificates(
            pfx_data,
            b"",
            default_backend(),
        )

    if not private_key:
        frappe.throw("No private key was found in the configured PFX file")

    return private_key


def get_pfx_password(settings):
    try:
        password = settings.get_password("password")
    except Exception:
        password = None

    if not password:
        password = getattr(settings, "password", None)

    return password or ""


def encrypt_payload(payload, aes_key_hex, private_key):
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    aes_key_bytes = binascii.unhexlify(aes_key_hex)
    padded_data = pad(json_bytes, AES.block_size)

    cipher = AES.new(aes_key_bytes, AES.MODE_ECB)
    encrypted_content = cipher.encrypt(padded_data)
    content_b64 = base64.b64encode(encrypted_content).decode("utf-8")

    signature = private_key.sign(
        content_b64.encode("utf-8"),
        asym_padding.PKCS1v15(),
        hashes.SHA1(),
    )

    return {
        "content": content_b64,
        "signature": base64.b64encode(signature).decode("utf-8"),
    }


def decrypt_response_content(encrypted_content, aes_key_hex):
    aes_key_bytes = bytes.fromhex(aes_key_hex)
    compressed_bytes = base64.b64decode(encrypted_content)

    try:
        encrypted_bytes = gzip.decompress(compressed_bytes)
    except Exception as gzip_error:
        for i in range(1, 5):
            try:
                encrypted_bytes = gzip.decompress(compressed_bytes[:-i])
                break
            except Exception:
                continue
        else:
            encrypted_bytes = compressed_bytes

    remainder = len(encrypted_bytes) % AES.block_size
    if remainder:
        encrypted_bytes = encrypted_bytes[:-remainder]

    cipher = AES.new(aes_key_bytes, AES.MODE_ECB)
    decrypted_padded = cipher.decrypt(encrypted_bytes)

    try:
        decrypted_bytes = unpad(decrypted_padded, AES.block_size)
    except ValueError:
        decrypted_bytes = decrypted_padded

    try:
        decoded_text = decrypted_bytes.decode("utf-8")
    except UnicodeDecodeError:
        decoded_text = decrypted_bytes.decode("latin-1")

    try:
        return json.loads(decoded_text)
    except json.JSONDecodeError as exc:
        raise frappe.ValidationError(
            f"Failed to parse decrypted T127 response as JSON using AES key {aes_key_hex}: {exc}"
        )


def get_record_value(record, fieldname):
    value = record.get(fieldname)
    if value not in (None, ""):
        return value

    for key, key_value in record.items():
        if str(key).strip().lower() == fieldname.lower():
            return key_value

    return ""


def normalize_t127_record(record):
    product_code = str(get_record_value(record, "goodsCode") or "").strip()
    goods_service_name = str(get_record_value(record, "goodsName") or "").strip()
    unit_price = get_record_value(record, "unitPrice")
    tax_rate = get_record_value(record, "taxRate")

    return {
        "product_code": product_code,
        "goods_service_name": goods_service_name,
        "unit_price": "" if unit_price in (None, "") else str(unit_price).strip(),
        "tax_rate": "" if tax_rate in (None, "") else str(tax_rate).strip(),
        "raw_record": record,
    }


def build_t127_request(settings, private_key, page_no="1"):
    payload = {
        "pageNo": str(page_no),
        "pageSize": T127_PAGE_SIZE,
    }

    encrypted_result = encrypt_dynamic_json(payload)
    if not encrypted_result.get("success"):
        frappe.throw(f"Encryption failed: {encrypted_result.get('error')}")

    request_time = datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    request_data = {
        "data": {
            "content": encrypted_result["encrypted_content"],
            "signature": encrypted_result["signature"],
            "dataDescription": {
                "codeType": "0",
                "encryptCode": "1",
                "zipCode": "0",
            },
        },
        "globalInfo": {
            "appId": "AP04",
            "version": "1.1.20191201",
            "dataExchangeId": uuid.uuid4().hex[:32],
            "interfaceCode": "T127",
            "requestCode": "TP",
            "requestTime": request_time,
            "responseCode": "TA",
            "userName": "admin",
            "deviceMAC": "B47720524158",
            "deviceNo": settings.device_number,
            "tin": settings.tin,
            "brn": (settings.brn or "").strip().lstrip("/"),
            "taxpayerID": "999000002030357",
            "longitude": "32.61665",
            "latitude": "0.36601",
            "agentType": "0",
            "extendField": {
                "responseDateFormat": "dd/MM/yyyy",
                "responseTimeFormat": "dd/MM/yyyy HH:mm:ss",
                "referenceNo": "",
                "operatorName": "",
            },
        },
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": "",
        },
    }
    return request_data, encrypted_result.get("aes_key", "")


def send_efris_request(settings, request_data, aes_key=""):
    headers = {"Content-Type": "application/json"}
    service = "T127"

    try:
        response = requests.post(
            settings.server_url,
            json=request_data,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        response_data = response.json()
    except requests.exceptions.RequestException as exc:
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            {},
            service=service,
            error=str(exc),
            aes_key=aes_key,
        )
        raise
    except ValueError as exc:
        raw_response = getattr(response, "text", "") if "response" in locals() else ""
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            {"raw_response": raw_response},
            service=service,
            error=f"Failed to decode JSON response: {exc}",
            aes_key=aes_key,
        )
        raise

    return_state = response_data.get("returnStateInfo", {})
    return_code = return_state.get("returnCode")
    return_message = return_state.get("returnMessage") or ""

    if return_code and return_code != "00":
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            service=service,
            error=f"EFRIS returned error {return_code}: {return_message}",
            aes_key=aes_key,
        )
        frappe.throw(f"EFRIS returned error {return_code}: {return_message}")

    if not return_code and return_message and return_message.upper() != "SUCCESS":
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            service=service,
            error=f"EFRIS returned error: {return_message}",
            aes_key=aes_key,
        )
        frappe.throw(f"EFRIS returned error: {return_message}")

    log_integration_request(
        "Completed",
        settings.server_url,
        headers,
        request_data,
        response_data,
        service=service,
        aes_key=aes_key,
    )
    return response_data


def get_all_efris_records(settings, private_key):
    page_no = 1
    page_count = 1
    records = []

    while page_no <= page_count:
        request_data, aes_key_used = build_t127_request(settings, private_key, page_no=page_no)
        response_data = send_efris_request(settings, request_data, aes_key=aes_key_used)

        encrypted_content = response_data.get("data", {}).get("content")
        if not encrypted_content:
            break

        decrypted_data = decrypt_response_content(
            encrypted_content,
            aes_key_used or settings.aes_key,
        )
        records.extend(decrypted_data.get("records") or [])

        page_info = decrypted_data.get("page") or {}
        try:
            page_count = int(page_info.get("pageCount") or 1)
        except (TypeError, ValueError):
            page_count = 1

        page_no += 1

    return records


def upsert_daily_price_record(record):
    normalized = normalize_t127_record(record)
    product_code = normalized["product_code"]

    if not product_code:
        frappe.throw(f"Missing goodsCode in T127 response record: {json.dumps(record)}")

    existing = frappe.get_all(
        "EFRIS Prices",
        filters={
            "product_code": product_code,
        },
        fields=["name", "unit_price"],
        limit_page_length=1,
    )

    values = {
        "product_code": product_code,
        "goods_service_name": normalized["goods_service_name"],
        "unit_price": normalized["unit_price"],
        "tax_rate": normalized["tax_rate"],
    }

    if existing:
        existing_unit_price = str(existing[0].unit_price or "").strip()
        incoming_unit_price = str(normalized["unit_price"] or "").strip()

        if existing_unit_price == incoming_unit_price:
            return "skipped", existing[0].name

        doc = frappe.get_doc("EFRIS Prices", existing[0].name)
        doc.update(values)
        doc.save(ignore_permissions=True)
        return "updated", doc.name

    doc = frappe.get_doc({
        "doctype": "EFRIS Prices",
        **values,
    })
    doc.insert(ignore_permissions=True)
    return "created", doc.name


@frappe.whitelist()
def sync_daily_efris_prices():
    try:
        settings = get_efris_settings()
        pfx_password = get_pfx_password(settings)
        private_key = get_private_key(resolve_file_path(settings.private_key), pfx_password)
        records = get_all_efris_records(settings, private_key)

        created = 0
        updated = 0
        skipped = 0
        item_results = []

        for record in records:
            normalized = normalize_t127_record(record)
            product_code = normalized["product_code"]
            try:
                action, _docname = upsert_daily_price_record(record)
                if action == "created":
                    created += 1
                elif action == "updated":
                    updated += 1
                else:
                    skipped += 1

                item_results.append({
                    "product_code": product_code,
                    "status": action,
                    "goods_service_name": normalized["goods_service_name"],
                    "unit_price": normalized["unit_price"],
                    "tax_rate": normalized["tax_rate"],
                })
            except Exception as exc:
                frappe.log_error(
                    title="EFRIS Daily Price Sync Item Error",
                    message=frappe.get_traceback(),
                )
                item_results.append({
                    "product_code": product_code,
                    "status": "error",
                    "message": str(exc),
                })

        frappe.db.commit()

        summary = {
            "success": True,
            "total_items": len(records),
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "failed": len([row for row in item_results if row.get("status") == "error"]),
            "item_results": item_results,
        }

        frappe.logger().info(f"EFRIS daily price sync summary: {summary}")
        return summary
    except Exception as exc:
        message = str(exc)

        if message == "EFRIS Settings is disabled":
            result = {
                "success": False,
                "skipped": True,
                "message": "EFRIS Settings is disabled. Enable the Active checkbox in EFRIS Settings first.",
            }
            frappe.logger().info(f"EFRIS daily price sync skipped: {result}")
            return result

        frappe.log_error(
            title="EFRIS Daily Price Sync Error",
            message=frappe.get_traceback(),
        )
        return {
            "success": False,
            "message": message,
        }
