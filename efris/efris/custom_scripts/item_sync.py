import uuid
from decimal import Decimal, InvalidOperation

import frappe
import requests
from frappe.utils.data import strip_html

from efris.efris.background_tasks.encryption import encrypt_dynamic_json
from efris.efris.custom_scripts.upload_invoice import (
    EFRISIntegrationError,
    EFRIS_OPERATOR_NAME,
    clean_brn,
    decrypt_efris_response_content,
    get_efris_request_time,
    get_efris_settings,
    log_integration_request,
)

T130_INTERFACE_CODE = "T130"
T130_SERVICE_NAME = "T130 Goods Upload"
ADD_OPERATION = "101"
MODIFY_OPERATION = "102"
NO_FLAG = "102"
UGX_CURRENCY_CODE = "101"

EFRIS_UOM_MAPPING = {
    "pieces": "PP",
    "piece": "PP",
    "pp-piece": "PP",
    "pair": "111",
    "litre": "102",
    "liter": "102",
}


def normalize_unit_price(value):
    cleaned_value = str(value or "").replace(",", "").strip()
    if not cleaned_value:
        raise EFRISIntegrationError("EFRIS Unit Price is required.")

    try:
        unit_price = Decimal(cleaned_value)
    except InvalidOperation:
        raise EFRISIntegrationError("EFRIS Unit Price must be a valid number.")

    if unit_price < 0:
        raise EFRISIntegrationError("EFRIS Unit Price cannot be negative.")

    return format(unit_price, "f")


def get_efris_uom_code(uom):
    normalized_uom = str(uom or "").strip()
    if not normalized_uom:
        raise EFRISIntegrationError("EFRIS Unit of Measure is required.")

    return EFRIS_UOM_MAPPING.get(normalized_uom.lower(), normalized_uom)


def validate_vat_value(vat):
    normalized_vat = str(vat or "").strip()
    if normalized_vat not in {"0.18", "0", "-"}:
        raise EFRISIntegrationError("VAT must be 0.18, 0, or - for exempt items.")
    return normalized_vat


def build_t130_payload(item, goods_name, category_id, efris_uom, unit_price):
    goods_code = (item.custom_efris_product_code or item.item_code or "").strip()
    operation_type = MODIFY_OPERATION if item.custom_efris_product_code else ADD_OPERATION

    if not goods_code:
        raise EFRISIntegrationError("Item Code is required before syncing with EFRIS.")
    if len(goods_code) > 50:
        raise EFRISIntegrationError("Item Code cannot exceed 50 characters for EFRIS.")

    goods_name = str(goods_name or "").strip()
    if not goods_name:
        raise EFRISIntegrationError("EFRIS Goods Name is required.")

    category_id = str(category_id or "").strip()
    if not category_id:
        raise EFRISIntegrationError("EFRIS Goods Category ID is required.")

    return [
        {
            "operationType": operation_type,
            "goodsName": goods_name,
            "goodsCode": goods_code,
            "measureUnit": get_efris_uom_code(efris_uom),
            "unitPrice": normalize_unit_price(unit_price),
            "currency": UGX_CURRENCY_CODE,
            "commodityCategoryId": category_id,
            "haveExciseTax": NO_FLAG,
            "description": strip_html(item.description or "")[:1024],
            "stockPrewarning": "0",
            "havePieceUnit": NO_FLAG,
            "pieceMeasureUnit": "",
            "pieceUnitPrice": "",
            "packageScaledValue": "",
            "pieceScaledValue": "",
            "exciseDutyCode": "",
            "haveOtherUnit": NO_FLAG,
            "goodsTypeCode": "101",
            "haveCustomsUnit": NO_FLAG,
            "commodityGoodsExtendEntity": {
                "customsMeasureUnit": "",
                "customsUnitPrice": "",
                "packageScaledValueCustoms": "",
                "customsScaledValue": "",
            },
            "customsUnitList": [],
            "goodsOtherUnits": [],
        }
    ]


def build_t130_request(settings, encrypted_result, item_name):
    return {
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
            "dataExchangeId": uuid.uuid4().hex,
            "interfaceCode": T130_INTERFACE_CODE,
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
                "referenceNo": item_name[:50],
                "operatorName": EFRIS_OPERATOR_NAME,
            },
        },
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": "",
        },
    }


def submit_t130_request(settings, request_data, aes_key, item_name):
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
    except (requests.exceptions.RequestException, ValueError) as exc:
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            {},
            error=str(exc),
            aes_key=aes_key,
            reference_docname=item_name,
            reference_doctype="Item",
            service=T130_SERVICE_NAME,
        )
        raise EFRISIntegrationError(f"T130 request failed: {exc}")

    return response_data, headers


def decrypt_t130_response(response_data, aes_key, settings_aes_key=""):
    encrypted_content = response_data.get("data", {}).get("content")
    if not encrypted_content:
        return []

    keys = [key for key in [aes_key, settings_aes_key] if key]
    last_error = None
    for key in dict.fromkeys(keys):
        try:
            return decrypt_efris_response_content(encrypted_content, key)
        except Exception as exc:
            last_error = exc

    raise EFRISIntegrationError(f"Failed to decrypt T130 response: {last_error}")


def validate_t130_response(response_data, decrypted_data):
    return_state = response_data.get("returnStateInfo") or {}
    return_code = str(return_state.get("returnCode") or "").strip()
    return_message = str(return_state.get("returnMessage") or "").strip()

    if return_code not in {"", "00"}:
        raise EFRISIntegrationError(f"T130 error {return_code}: {return_message or 'Unknown EFRIS error'}")
    if return_message and return_message.upper() != "SUCCESS":
        raise EFRISIntegrationError(f"T130 failed: {return_message}")

    result = decrypted_data[0] if isinstance(decrypted_data, list) and decrypted_data else decrypted_data
    if not isinstance(result, dict):
        return {}

    item_return_code = str(result.get("returnCode") or "").strip()
    item_return_message = str(result.get("returnMessage") or "").strip()
    if item_return_code not in {"", "00"}:
        raise EFRISIntegrationError(
            f"T130 item error {item_return_code}: {item_return_message or 'Unknown EFRIS error'}"
        )

    return result


def update_item_efris_fields(item, payload_item, response_item, efris_uom, vat):
    values = {
        "custom_efris_product_code": response_item.get("goodsCode") or payload_item["goodsCode"],
        "custom_goods_service_name": response_item.get("goodsName") or payload_item["goodsName"],
        "custom_goods_category_id": response_item.get("commodityCategoryId")
        or payload_item["commodityCategoryId"],
        "custom_uom_code_efris": efris_uom,
        "custom_efris_price": str(response_item.get("unitPrice") or payload_item["unitPrice"]),
        "custom_vat_": vat,
    }
    frappe.db.set_value("Item", item.name, values, update_modified=True)
    frappe.db.commit()
    return values


def _sync_item_with_efris(item_name, goods_name, category_id, efris_uom, unit_price, vat):
    item = frappe.get_doc("Item", item_name)
    if not frappe.has_permission("Item", "write", doc=item):
        frappe.throw("You do not have permission to update this Item.")

    vat = validate_vat_value(vat)
    payload = build_t130_payload(item, goods_name, category_id, efris_uom, unit_price)
    settings = get_efris_settings()
    encrypted_result = encrypt_dynamic_json(payload)
    if not encrypted_result.get("success"):
        raise EFRISIntegrationError(f"Failed to encrypt T130 payload: {encrypted_result.get('error')}")

    aes_key = encrypted_result.get("aes_key", "")
    request_data = build_t130_request(settings, encrypted_result, item.name)
    response_data, headers = submit_t130_request(settings, request_data, aes_key, item.name)

    try:
        decrypted_data = decrypt_t130_response(
            response_data,
            aes_key,
            settings_aes_key=(getattr(settings, "aes_key", "") or "").strip(),
        )
        response_item = validate_t130_response(response_data, decrypted_data)
    except EFRISIntegrationError as exc:
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            error=str(exc),
            aes_key=aes_key,
            reference_docname=item.name,
            reference_doctype="Item",
            service=T130_SERVICE_NAME,
        )
        raise

    log_integration_request(
        "Completed",
        settings.server_url,
        headers,
        request_data,
        response_data,
        aes_key=aes_key,
        reference_docname=item.name,
        reference_doctype="Item",
        service=T130_SERVICE_NAME,
    )

    saved_values = update_item_efris_fields(
        item,
        payload[0],
        response_item,
        efris_uom=str(efris_uom).strip(),
        vat=vat,
    )

    return {
        "success": True,
        "operation": "updated" if payload[0]["operationType"] == MODIFY_OPERATION else "created",
        "message": f"Item {item.name} was successfully {('updated' if payload[0]['operationType'] == MODIFY_OPERATION else 'created')} in EFRIS.",
        "values": saved_values,
        "response": response_item,
    }


@frappe.whitelist()
def sync_item_with_efris(item_name, goods_name, category_id, efris_uom, unit_price, vat):
    try:
        return _sync_item_with_efris(
            item_name,
            goods_name,
            category_id,
            efris_uom,
            unit_price,
            vat,
        )
    except EFRISIntegrationError as exc:
        frappe.throw(str(exc), title="EFRIS Item Sync Failed")
