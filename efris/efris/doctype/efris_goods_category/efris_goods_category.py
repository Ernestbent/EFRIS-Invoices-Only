# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

import base64
import gzip
import json
import uuid

import frappe
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from frappe.model.document import Document
from frappe.utils import now_datetime

from efris.efris.background_tasks.efris_price_sync import (
    get_pfx_password,
    get_private_key,
    resolve_file_path,
)
from efris.efris.custom_scripts.upload_invoice import (
    clean_brn,
    get_efris_request_time,
    log_integration_request,
)


DOCTYPE = "EFRIS Goods Category"
INTERFACE_CODE = "T124"
SERVICE_NAME = "T124 Goods Category Sync"
PAGE_SIZE = 100


class EFRISGoodsCategory(Document):
    pass


def get_category_settings():
    settings = frappe.get_single("EFRIS Settings")
    if not getattr(settings, "active", 0):
        frappe.throw("EFRIS Settings is disabled.")

    required_fields = {
        "server_url": "Server URL",
        "device_number": "Device Number",
        "tin": "TIN",
        "private_key": "Private Key",
    }
    for fieldname, label in required_fields.items():
        if not getattr(settings, fieldname, None):
            frappe.throw(f"{label} is required in EFRIS Settings.")

    return settings


def encode_plain_content(payload, private_key):
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    content = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    signature = private_key.sign(
        content.encode("ascii"),
        asym_padding.PKCS1v15(),
        hashes.SHA1(),
    )
    return content, base64.b64encode(signature).decode("ascii")


def build_t124_request(settings, private_key, page_no):
    content, signature = encode_plain_content(
        {"pageNo": str(page_no), "pageSize": str(PAGE_SIZE)},
        private_key,
    )
    return {
        "data": {
            "content": content,
            "signature": signature,
            "dataDescription": {
                "codeType": "0",
                "encryptCode": "0",
                "zipCode": "0",
            },
        },
        "globalInfo": {
            "appId": "AP04",
            "version": "1.1.20191201",
            "dataExchangeId": uuid.uuid4().hex,
            "interfaceCode": INTERFACE_CODE,
            "requestCode": "TP",
            "requestTime": get_efris_request_time(),
            "responseCode": "TA",
            "userName": "admin",
            "deviceMAC": "B47720524158",
            "deviceNo": settings.device_number,
            "tin": settings.tin,
            "brn": clean_brn(settings.brn),
            "taxpayerID": "999000002030357",
            "longitude": "32.61665",
            "latitude": "0.36601",
            "agentType": "0",
            "extendField": {
                "responseDateFormat": "dd/MM/yyyy",
                "responseTimeFormat": "dd/MM/yyyy HH:mm:ss",
                "referenceNo": "goods-category-sync",
                "operatorName": frappe.session.user or "Administrator",
            },
        },
        "returnStateInfo": {"returnCode": "", "returnMessage": ""},
    }


def parse_t124_content(response_data):
    content = (response_data.get("data") or {}).get("content")
    if isinstance(content, dict):
        return content
    if not content:
        if "records" in response_data:
            return response_data
        return {}

    if isinstance(content, bytes):
        content = content.decode("utf-8")

    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        pass

    try:
        decoded = base64.b64decode(content)
        if decoded.startswith(b"\x1f\x8b"):
            decoded = gzip.decompress(decoded)
        return json.loads(decoded.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise frappe.ValidationError(f"Unable to decode the T124 response content: {exc}")


def validate_t124_response(response_data):
    return_state = response_data.get("returnStateInfo") or {}
    return_code = str(return_state.get("returnCode") or "").strip()
    return_message = str(return_state.get("returnMessage") or "").strip()
    if return_code not in {"", "00"}:
        frappe.throw(f"T124 error {return_code}: {return_message or 'Unknown EFRIS error'}")
    if return_message and return_message.upper() != "SUCCESS":
        frappe.throw(f"T124 failed: {return_message}")


def send_t124_request(settings, request_data):
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(
            settings.server_url,
            json=request_data,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        response_data = response.json()
        validate_t124_response(response_data)
    except (requests.exceptions.RequestException, ValueError) as exc:
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            {},
            error=str(exc),
            service=SERVICE_NAME,
            reference_doctype=DOCTYPE,
        )
        raise

    log_integration_request(
        "Completed",
        settings.server_url,
        headers,
        request_data,
        response_data,
        service=SERVICE_NAME,
        reference_doctype=DOCTYPE,
    )
    return response_data


def is_ura_yes(value):
    return 1 if str(value or "").strip() in {"1", "101"} else 0


def normalize_category(record, synced_on=None):
    return {
        "category_code": str(record.get("commodityCategoryCode") or "").strip(),
        "category_name": str(record.get("commodityCategoryName") or "").strip(),
        "parent_code": str(record.get("parentCode") or "").strip(),
        "category_level": int(record.get("commodityCategoryLevel") or 0),
        "tax_rate": str(record.get("rate") or "").strip(),
        "exclusion_code": str(record.get("exclusion") or "").strip(),
        "is_leaf_node": is_ura_yes(record.get("isLeafNode")),
        "is_service": is_ura_yes(record.get("serviceMark")),
        "enabled": 1 if str(record.get("enableStatusCode") or "").strip() == "1" else 0,
        "is_zero_rate": is_ura_yes(record.get("isZeroRate")),
        "is_exempt": is_ura_yes(record.get("isExempt")),
        "excisable": is_ura_yes(record.get("excisable")),
        "vat_out_of_scope": is_ura_yes(record.get("vatOutScopeCode")),
        "last_synced_on": synced_on or now_datetime(),
    }


def upsert_category(record, synced_on):
    values = normalize_category(record, synced_on=synced_on)
    category_code = values["category_code"]
    if not category_code or not values["category_name"]:
        return "skipped"

    if frappe.db.exists(DOCTYPE, category_code):
        frappe.db.set_value(DOCTYPE, category_code, values, update_modified=False)
        return "updated"

    frappe.get_doc({"doctype": DOCTYPE, **values}).insert(ignore_permissions=True)
    return "created"


def sync_goods_categories(requested_by=None, start_page=1):
    settings = get_category_settings()
    private_key_path = resolve_file_path(settings.private_key)
    private_key = get_private_key(private_key_path, get_pfx_password(settings))
    synced_on = now_datetime()
    totals = {"created": 0, "updated": 0, "skipped": 0}
    try:
        page_no = max(1, int(start_page))
    except (TypeError, ValueError):
        page_no = 1
    page_count = page_no

    while page_no <= page_count:
        request_data = build_t124_request(settings, private_key, page_no)
        response_data = send_t124_request(settings, request_data)
        content = parse_t124_content(response_data)
        records = content.get("records") or []

        for record in records:
            result = upsert_category(record, synced_on)
            totals[result] += 1

        page = content.get("page") or {}
        try:
            page_count = max(1, int(page.get("pageCount") or 1))
        except (TypeError, ValueError):
            page_count = 1

        frappe.db.commit()
        page_no += 1

    result = {
        "success": True,
        "pages": page_count,
        "total": totals["created"] + totals["updated"],
        **totals,
    }
    if requested_by:
        frappe.publish_realtime("efris_goods_category_sync_complete", result, user=requested_by)
    return result


@frappe.whitelist()
def enqueue_goods_category_sync():
    frappe.only_for("System Manager")
    frappe.enqueue(
        sync_goods_categories,
        queue="long",
        timeout=3600,
        job_name="EFRIS Goods Category Sync",
        requested_by=frappe.session.user,
        enqueue_after_commit=True,
    )
    return {
        "success": True,
        "message": "EFRIS goods category synchronization has been queued.",
    }
