# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

import json
import uuid
import base64
from datetime import datetime, timedelta, timezone

import frappe
import requests
from frappe.model.document import Document


EAT_TIMEZONE = timezone(timedelta(hours=3))


def log_integration_request(status, url, headers, data, response, error="", aes_key=""):
    frappe.get_doc({
        "doctype": "Integration Request",
        "integration_type": "Remote",
        "integration_request_service": "T101 Test Connection",
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


def get_efris_settings():
    settings = frappe.get_single("EFRIS Settings")

    required_fields = {
        "server_url": "Server URL",
        "device_number": "Device Number",
        "tin": "TIN",
    }

    for fieldname, label in required_fields.items():
        if not getattr(settings, fieldname, None):
            frappe.throw(f"{label} is required in EFRIS Settings")

    return settings


def build_t101_request(settings):
    request_time = datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "data": {
            "content": "",
            "signature": "",
            "dataDescription": {
                "codeType": "0",
                "encryptCode": "0",
                "zipCode": "0",
            },
        },
        "globalInfo": {
            "appId": "AP04",
            "version": "1.1.20191201",
            "dataExchangeId": uuid.uuid4().hex[:32],
            "interfaceCode": "T101",
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


def extract_t101_server_time(response_data):
    current_time = response_data.get("currentTime")
    if current_time:
        return current_time

    content = response_data.get("data", {}).get("content")
    if not content:
        return ""

    try:
        decoded_content = base64.b64decode(content).decode("utf-8")
        parsed_content = json.loads(decoded_content)
        return parsed_content.get("currentTime", "")
    except Exception:
        try:
            parsed_content = json.loads(content)
            return parsed_content.get("currentTime", "")
        except Exception:
            return ""


@frappe.whitelist()
def test_connection():
    settings = get_efris_settings()
    request_data = build_t101_request(settings)
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            settings.server_url,
            json=request_data,
            headers=headers,
            timeout=30,
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
            error=str(exc),
        )
        frappe.throw(f"T101 request failed: {exc}")
    except ValueError as exc:
        raw_response = getattr(response, "text", "") if "response" in locals() else ""
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            {"raw_response": raw_response},
            error=f"Failed to decode JSON response: {exc}",
        )
        frappe.throw(f"Failed to decode T101 response: {exc}")

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
            error=f"EFRIS returned error {return_code}: {return_message}",
        )
        frappe.throw(f"EFRIS returned error {return_code}: {return_message}")

    current_time = extract_t101_server_time(response_data)

    if not current_time:
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            error="T101 response did not include currentTime",
        )
        frappe.throw("T101 response did not include currentTime")

    log_integration_request(
        "Completed",
        settings.server_url,
        headers,
        request_data,
        response_data,
    )

    return {
        "success": True,
        "server_time": current_time,
        "message": f"EFRIS server time: {current_time}",
    }


class EFRISSettings(Document):
    pass
