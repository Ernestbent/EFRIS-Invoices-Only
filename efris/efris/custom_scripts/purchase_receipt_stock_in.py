import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

import frappe
import requests

from efris.efris.background_tasks.encryption import encrypt_dynamic_json
from efris.efris.custom_scripts.item_sync import get_efris_uom_code
from efris.efris.custom_scripts.upload_invoice import (
    EAT_TIMEZONE,
    EFRISIntegrationError,
    EFRIS_OPERATOR_NAME,
    clean_brn,
    decrypt_efris_response_content,
    get_efris_request_time,
    get_efris_settings,
    log_integration_request,
)

T131_INTERFACE_CODE = "T131"
T131_SERVICE_NAME = "T131 Goods Stock In"
PURCHASE_RECEIPT_VOUCHER_TYPE = "Purchase Receipt"
STOCK_IN_TYPE_MAPPING = {
    "Import": "101",
    "Local Purchase": "102",
}
ITEM_EFRIS_FIELDS = [
    "custom_efris_product_code",
    "custom_goods_service_name",
    "custom_goods_category_id",
    "custom_uom_code_efris",
    "custom_purchase_price",
]


def normalize_positive_decimal(value, label):
    cleaned_value = str(value or "").replace(",", "").strip()
    if not cleaned_value:
        raise EFRISIntegrationError(f"{label} is required.")

    try:
        number = Decimal(cleaned_value)
    except InvalidOperation as exc:
        raise EFRISIntegrationError(f"{label} must be a valid number.") from exc

    if number <= 0:
        raise EFRISIntegrationError(f"{label} must be greater than zero.")

    return number


def get_purchase_receipt_stock_in_type(doc):
    stock_in_type = str(getattr(doc, "custom_stock_in_type", "") or "Local Purchase").strip()
    stock_in_type_code = STOCK_IN_TYPE_MAPPING.get(stock_in_type)
    if not stock_in_type_code:
        raise EFRISIntegrationError(
            f"Unsupported EFRIS Stock In Type '{stock_in_type}'. Use Local Purchase or Import."
        )
    return stock_in_type_code


def get_item_efris_stock_in_data(item_row):
    item_code = str(getattr(item_row, "item_code", "") or "").strip()
    if not item_code:
        raise EFRISIntegrationError(
            f"Missing Item Code on Purchase Receipt row {getattr(item_row, 'idx', '')}."
        )

    values = frappe.get_cached_value("Item", item_code, ITEM_EFRIS_FIELDS) or []
    item_values = dict(zip(ITEM_EFRIS_FIELDS, values))
    product_code = str(item_values.get("custom_efris_product_code") or "").strip()
    efris_uom = str(item_values.get("custom_uom_code_efris") or "").strip()

    if not product_code:
        raise EFRISIntegrationError(
            f"Missing EFRIS Product Code in Item {item_code}."
        )
    if not efris_uom:
        raise EFRISIntegrationError(
            f"Missing EFRIS Unit of Measure in Item {item_code}."
        )

    quantity_value = getattr(item_row, "stock_qty", None)
    if quantity_value in (None, ""):
        quantity_value = getattr(item_row, "qty", None)

    return {
        "item_code": item_code,
        "item_name": getattr(item_row, "item_name", "") or "",
        "goods_name": str(item_values.get("custom_goods_service_name") or "").strip(),
        "goods_category_id": str(item_values.get("custom_goods_category_id") or "").strip(),
        "product_code": product_code,
        "uom": get_efris_uom_code(efris_uom),
        "quantity": normalize_positive_decimal(
            quantity_value,
            f"Stock Quantity for Item {item_code}",
        ),
        "unit_price": normalize_positive_decimal(
            item_values.get("custom_purchase_price"),
            f"Purchase Price in Item {item_code}",
        ),
    }


def aggregate_purchase_receipt_items(doc):
    aggregated_items = {}

    for item_row in doc.items:
        item_data = get_item_efris_stock_in_data(item_row)
        product_code = item_data["product_code"]
        existing_item = aggregated_items.get(product_code)

        if not existing_item:
            aggregated_items[product_code] = item_data
            continue

        if existing_item["uom"] != item_data["uom"]:
            raise EFRISIntegrationError(
                f"EFRIS Product Code {product_code} has different units of measure on this receipt."
            )
        if existing_item["unit_price"] != item_data["unit_price"]:
            raise EFRISIntegrationError(
                f"EFRIS Product Code {product_code} has different Item Purchase Prices on this receipt."
            )

        existing_item["quantity"] += item_data["quantity"]

    if not aggregated_items:
        raise EFRISIntegrationError("The Purchase Receipt has no stock items to send to EFRIS.")

    return list(aggregated_items.values())


def build_t131_payload(doc, aggregated_items):
    supplier_tin = frappe.get_cached_value("Supplier", doc.supplier, "tax_id") or ""

    return {
        "goodsStockIn": {
            "operationType": "101",
            "supplierTin": str(supplier_tin).strip(),
            "supplierName": doc.supplier_name or "",
            "adjustType": "",
            "remarks": getattr(doc, "remarks", "") or "",
            "stockInDate": str(doc.posting_date),
            "stockInType": get_purchase_receipt_stock_in_type(doc),
            "productionBatchNo": "",
            "productionDate": "",
            "branchId": "",
            "invoiceNo": "",
            "isCheckBatchNo": "0",
            "rollBackIfError": "0",
            "goodsTypeCode": "101",
        },
        "goodsStockInItem": [
            {
                "commodityGoodsId": "",
                "goodsCode": item["product_code"],
                "measureUnit": item["uom"],
                "quantity": format(item["quantity"], "f"),
                "unitPrice": format(item["unit_price"], "f"),
                "remarks": "",
                "fuelTankId": "",
                "lossQuantity": "",
                "originalQuantity": "",
            }
            for item in aggregated_items
        ],
    }


def build_t131_request(settings, encrypted_result, purchase_receipt_name):
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
            "interfaceCode": T131_INTERFACE_CODE,
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
                "referenceNo": purchase_receipt_name[:50],
                "operatorName": EFRIS_OPERATOR_NAME,
            },
        },
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": "",
        },
    }


def submit_t131_request(settings, request_data):
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            settings.server_url,
            json=request_data,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        return response.json(), headers
    except requests.exceptions.Timeout as exc:
        raise EFRISIntegrationError("EFRIS stock-in request timed out. Please try again.") from exc
    except requests.exceptions.RequestException as exc:
        raise EFRISIntegrationError(f"EFRIS stock-in request failed: {exc}") from exc
    except ValueError as exc:
        raise EFRISIntegrationError("EFRIS returned an invalid response.") from exc


def get_t131_item_errors(response_data, aes_key, settings_aes_key=""):
    encrypted_content = response_data.get("data", {}).get("content")
    if not encrypted_content:
        return []

    decrypted_data = None
    for key in dict.fromkeys(key for key in (aes_key, settings_aes_key) if key):
        try:
            decrypted_data = decrypt_efris_response_content(encrypted_content, key)
            break
        except Exception:
            continue

    if decrypted_data is None:
        return []

    if isinstance(decrypted_data, list):
        records = decrypted_data
    elif isinstance(decrypted_data, dict):
        records = (
            decrypted_data.get("records")
            or decrypted_data.get("goodsStockInItem")
            or [decrypted_data]
        )
    else:
        return []

    errors = []
    for record in records:
        if not isinstance(record, dict):
            continue
        return_code = str(record.get("returnCode") or "").strip()
        return_message = str(record.get("returnMessage") or "").strip()
        if return_code not in {"", "00"} or (
            return_message and return_message.upper() != "SUCCESS"
        ):
            errors.append(return_message or f"EFRIS item error {return_code}")

    return errors


def validate_t131_response(response_data, aes_key, settings_aes_key=""):
    return_state = response_data.get("returnStateInfo") or {}
    return_code = str(return_state.get("returnCode") or "").strip()
    return_message = str(return_state.get("returnMessage") or "").strip()
    item_errors = get_t131_item_errors(response_data, aes_key, settings_aes_key)

    if return_code not in {"", "00"}:
        raise EFRISIntegrationError(
            f"T131 error {return_code}: {return_message or 'Unknown EFRIS error'}"
        )
    if return_message.upper() != "SUCCESS":
        error_detail = "; ".join(item_errors) or return_message or "Unknown EFRIS error"
        raise EFRISIntegrationError(f"T131 failed: {error_detail}")
    if item_errors:
        raise EFRISIntegrationError(f"T131 item errors: {'; '.join(item_errors)}")


def has_completed_t131_sync(purchase_receipt_name):
    return bool(
        frappe.db.exists(
            "Integration Request",
            {
                "integration_request_service": T131_SERVICE_NAME,
                "reference_doctype": PURCHASE_RECEIPT_VOUCHER_TYPE,
                "reference_docname": purchase_receipt_name,
                "status": "Completed",
            },
        )
    )


def record_purchase_receipt_stock_movement(doc, aggregated_items):
    from efris.efris.custom_scripts.efris_stock_ledger import get_latest_item_balance

    posting_datetime = datetime.now(EAT_TIMEZONE).replace(tzinfo=None)
    created = 0

    for item in aggregated_items:
        if frappe.db.exists(
            "EFRIS Stock Ledger Entry",
            {
                "voucher_type": PURCHASE_RECEIPT_VOUCHER_TYPE,
                "voucher_no": doc.name,
                "efris_product_code": item["product_code"],
            },
        ):
            continue

        previous_balance = Decimal(
            str(
                get_latest_item_balance(
                    item_code=item["item_code"],
                    efris_product_code=item["product_code"],
                )
            )
        )
        new_balance = previous_balance + item["quantity"]

        frappe.get_doc(
            {
                "doctype": "EFRIS Stock Ledger Entry",
                "posting_date": posting_datetime.date(),
                "posting_time": posting_datetime.time().strftime("%H:%M:%S"),
                "item_code": item["item_code"],
                "item_name": item["item_name"],
                "uom": item["uom"],
                "efris_goods_name": item["goods_name"],
                "efris_product_code": item["product_code"],
                "qty_in": float(item["quantity"]),
                "qty_out": 0,
                "balance": float(new_balance),
                "voucher_type": PURCHASE_RECEIPT_VOUCHER_TYPE,
                "voucher_no": doc.name,
                "is_opening_entry": 0,
            }
        ).insert(ignore_permissions=True)
        created += 1

    frappe.db.commit()
    return created


def validate_purchase_receipt(doc):
    if doc.docstatus != 1:
        raise EFRISIntegrationError("Submit the Purchase Receipt before syncing it with EFRIS.")
    if doc.is_return:
        raise EFRISIntegrationError("Return Purchase Receipts cannot be sent as T131 stock-in transactions.")
    if not doc.items:
        raise EFRISIntegrationError("The Purchase Receipt has no items to send to EFRIS.")
    if has_completed_t131_sync(doc.name):
        raise EFRISIntegrationError(f"Purchase Receipt {doc.name} is already synced with EFRIS.")


def process_purchase_receipt_t131(doc):
    validate_purchase_receipt(doc)
    aggregated_items = aggregate_purchase_receipt_items(doc)
    settings = get_efris_settings()
    payload = build_t131_payload(doc, aggregated_items)
    encrypted_result = encrypt_dynamic_json(payload)
    if not encrypted_result.get("success"):
        raise EFRISIntegrationError(
            f"Failed to encrypt T131 payload: {encrypted_result.get('error')}"
        )

    aes_key = encrypted_result.get("aes_key", "")
    request_data = build_t131_request(settings, encrypted_result, doc.name)
    response_data = {}
    headers = {"Content-Type": "application/json"}

    try:
        response_data, headers = submit_t131_request(settings, request_data)
        validate_t131_response(
            response_data,
            aes_key,
            settings_aes_key=(getattr(settings, "aes_key", "") or "").strip(),
        )
    except Exception as exc:
        log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            error=str(exc),
            aes_key=aes_key,
            reference_docname=doc.name,
            reference_doctype=PURCHASE_RECEIPT_VOUCHER_TYPE,
            service=T131_SERVICE_NAME,
        )
        raise

    log_integration_request(
        "Completed",
        settings.server_url,
        headers,
        request_data,
        response_data,
        aes_key=aes_key,
        reference_docname=doc.name,
        reference_doctype=PURCHASE_RECEIPT_VOUCHER_TYPE,
        service=T131_SERVICE_NAME,
    )

    ledger_warning = ""
    ledger_entries_created = 0
    try:
        ledger_entries_created = record_purchase_receipt_stock_movement(doc, aggregated_items)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(
            frappe.get_traceback(),
            f"EFRIS T131 Stock Ledger Update Error - {doc.name}",
        )
        ledger_warning = (
            "EFRIS accepted the stock-in, but the local EFRIS Stock Ledger could not be updated. "
            "The scheduled T127 stock sync will reconcile it."
        )

    return {
        "success": True,
        "message": f"Purchase Receipt {doc.name} stock successfully submitted to EFRIS URA via T131.",
        "items_sent": len(aggregated_items),
        "ledger_entries_created": ledger_entries_created,
        "warning": ledger_warning,
    }


def get_permitted_purchase_receipt(purchase_receipt_name, permission_type="read"):
    doc = frappe.get_doc(PURCHASE_RECEIPT_VOUCHER_TYPE, purchase_receipt_name)
    if not frappe.has_permission(
        PURCHASE_RECEIPT_VOUCHER_TYPE,
        permission_type,
        doc=doc,
    ):
        frappe.throw("You do not have permission to sync this Purchase Receipt with EFRIS.")
    return doc


@frappe.whitelist()
def get_purchase_receipt_efris_sync_status(purchase_receipt_name):
    get_permitted_purchase_receipt(purchase_receipt_name)
    return {
        "success": True,
        "synced": has_completed_t131_sync(purchase_receipt_name),
    }


@frappe.whitelist()
def sync_purchase_receipt_with_efris(purchase_receipt_name):
    doc = get_permitted_purchase_receipt(purchase_receipt_name, permission_type="submit")

    try:
        return process_purchase_receipt_t131(doc)
    except EFRISIntegrationError as exc:
        frappe.throw(str(exc), title="EFRIS Stock-In Blocked")
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"EFRIS T131 Purchase Receipt Error - {purchase_receipt_name}",
        )
        raise
