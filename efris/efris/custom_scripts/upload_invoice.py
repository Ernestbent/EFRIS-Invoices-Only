import json
import uuid
import gzip
import base64
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta

import frappe
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from efris.efris.background_tasks.encryption import encrypt_dynamic_json

EAT_TIMEZONE = timezone(timedelta(hours=3))
STANDARD_TAX_RATE = 0.18
ZERO_TAX_RATE = 0.0
STANDARD_TAX_CODE = "01"
ZERO_TAX_CODE = "02"
SALES_INVOICE_UOM_MAPPING = {
    "pieces": "PP",
    "piece": "PP",
    "litre": "102",
    "liter": "102",
}
BUYER_TYPE_MAPPING = {
    "B2B": "0",
    "B2C": "1",
    "Foreigner": "2",
    "B2G": "3"
}
DEFAULT_BUYER_TYPE = "1"
EFRIS_SEND_INVOICE_ALLOWED_USERS = {
    "ernestben69@gmail.com",
    "reports@autozonepro.org",
}
ROW_FIELD_CANDIDATES = {
    "product_code": [
        "custom_efris_product_code",
        "custom_efrsis_product_code",
    ],
    "goods_service_name": [
        "custom_efris_item_name",
        "custom_goods_service_name",
    ],
    "goods_category_id": [
        "custom_goods_category_id",
    ],
}
ITEM_MASTER_FIELD_MAP = {
    "product_code": "custom_efris_product_code",
    "goods_service_name": "custom_goods_service_name",
    "goods_category_id": "custom_goods_category_id",
}


class EFRISIntegrationError(Exception):
    pass


TWO_PLACES = Decimal("0.01")
THREE_PLACES = Decimal("0.001")
STANDARD_TAX_RATE_DECIMAL = Decimal("0.18")
ZERO_TAX_RATE_DECIMAL = Decimal("0.00")


def validate_send_invoice_user():
    if frappe.session.user not in EFRIS_SEND_INVOICE_ALLOWED_USERS:
        frappe.throw("You are not allowed to send invoices to EFRIS.")


def get_invoice_reference_no(doc):
    for item in getattr(doc, "items", []) or []:
        sales_order = getattr(item, "sales_order", None)
        if sales_order:
            return sales_order

    return doc.name


def log_integration_request(status, url, headers, data, response, error="", aes_key="", reference_docname=""):
    valid_statuses = ["", "Queued", "Authorized", "Completed", "Cancelled", "Failed"]
    status = status if status in valid_statuses else "Failed"

    integration_request = frappe.get_doc({
        "doctype": "Integration Request",
        "integration_type": "Remote",
        "method": "POST",
        "integration_request_service": "T109 Goods Upload",
        "is_remote_request": True,
        "status": status,
        "custom_aes_key": aes_key or "",
        "url": url,
        "request_headers": json.dumps(headers, indent=4),
        "data": json.dumps(data, indent=4),
        "output": json.dumps(response, indent=4),
        "error": error,
        "reference_doctype": "Sales Invoice" if reference_docname else "",
        "reference_docname": reference_docname or "",
        "execution_time": datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    })
    integration_request.insert(ignore_permissions=True)
    frappe.db.commit()


def get_efris_settings():
    efris_settings = frappe.get_single("EFRIS Settings")

    if not getattr(efris_settings, "active", 0):
        raise EFRISIntegrationError("EFRIS integration is disabled")

    if not efris_settings.tin or not efris_settings.brn:
        raise EFRISIntegrationError("TIN and BRN are required in EFRIS Settings")

    return efris_settings


def clean_brn(brn):
    return brn.strip().lstrip("/") if brn else ""


def get_first_available_value(source, fieldnames):
    for fieldname in fieldnames:
        value = getattr(source, fieldname, None)
        if value not in (None, ""):
            return value
    return ""


def get_item_efris_data(item_row):
    item_code = getattr(item_row, "item_code", None)
    item_fields = list(ITEM_MASTER_FIELD_MAP.values())

    item_master_values = {}
    if item_code:
        cached_values = frappe.get_cached_value("Item", item_code, item_fields) or []
        item_master_values = dict(zip(item_fields, cached_values))

    return {
        "product_code": str(
            get_first_available_value(item_row, ROW_FIELD_CANDIDATES["product_code"])
            or item_master_values.get(ITEM_MASTER_FIELD_MAP["product_code"])
            or ""
        ).strip(),
        "goods_service_name": str(
            get_first_available_value(item_row, ROW_FIELD_CANDIDATES["goods_service_name"])
            or item_master_values.get(ITEM_MASTER_FIELD_MAP["goods_service_name"])
            or getattr(item_row, "item_name", None)
            or ""
        ).strip(),
        "goods_category_id": str(
            get_first_available_value(item_row, ROW_FIELD_CANDIDATES["goods_category_id"])
            or item_master_values.get(ITEM_MASTER_FIELD_MAP["goods_category_id"])
            or ""
        ).strip(),
    }


def sync_sales_invoice_efris_prices(doc, method=None):
    if isinstance(doc, str):
        doc = frappe.get_doc("Sales Invoice", doc)

    if not getattr(doc, "items", None):
        return

    for item in doc.items:
        item_code = getattr(item, "item_code", None)
        if not item_code:
            continue

        item.custom_efris_unit_price = frappe.get_cached_value("Item", item_code, "custom_efris_price") or ""


def get_sales_invoice_item_unit_price(item):
    unit_price = getattr(item, "custom_efris_unit_price", None)
    if unit_price in (None, ""):
        raise EFRISIntegrationError(
            f"Missing Item price on invoice item row {getattr(item, 'idx', '')} item {getattr(item, 'item_code', '')}"
        )

    return truncate_two_decimals(unit_price)


def get_sales_invoice_item_vat_rate(item):
    vat_value = getattr(item, "custom_vat", None)
    if vat_value in (None, ""):
        raise EFRISIntegrationError(
            f"Missing VAT on invoice item row {getattr(item, 'idx', '')} item {getattr(item, 'item_code', '')}"
        )

    vat_string = str(vat_value).strip().replace("%", "")
    try:
        vat_rate = Decimal(vat_string)
    except Exception as exc:
        raise EFRISIntegrationError(
            f"Invalid VAT '{vat_value}' on invoice item row {getattr(item, 'idx', '')} item {getattr(item, 'item_code', '')}"
        ) from exc

    if vat_rate > 1:
        vat_rate = vat_rate / Decimal("100")

    return truncate_two_decimals(vat_rate)


def get_sales_invoice_item_uom_code(item):
    item_uom = str(getattr(item, "uom", "") or "").strip()
    mapped_uom = SALES_INVOICE_UOM_MAPPING.get(item_uom.lower())

    if not mapped_uom:
        raise EFRISIntegrationError(
            f"Unsupported Sales Invoice Item UOM '{item_uom}' on row {getattr(item, 'idx', '')}. "
            "Add it to SALES_INVOICE_UOM_MAPPING in upload_invoice.py."
        )

    return mapped_uom


def to_decimal(value):
    if value in (None, ""):
        return Decimal("0")

    return Decimal(str(value))


def truncate_two_decimals(value):
    return to_decimal(value).quantize(TWO_PLACES, rounding=ROUND_DOWN)


def truncate_three_decimals(value):
    return to_decimal(value).quantize(THREE_PLACES, rounding=ROUND_DOWN)


def normalize_tax_rate(value):
    return truncate_two_decimals(value)


def get_tax_category_details(tax_rate):
    if tax_rate in (None, ""):
        raise EFRISIntegrationError("Missing VAT rate on Sales Invoice Item.")

    normalized_tax_rate = normalize_tax_rate(tax_rate)

    if normalized_tax_rate == STANDARD_TAX_RATE_DECIMAL:
        return STANDARD_TAX_CODE, str(STANDARD_TAX_RATE)

    if normalized_tax_rate == ZERO_TAX_RATE_DECIMAL:
        return ZERO_TAX_CODE, str(int(ZERO_TAX_RATE))

    raise EFRISIntegrationError(
        f"Unsupported EFRIS tax rate {tax_rate}. Only 0.18 and 0 are currently supported."
    )


def calculate_line_tax(total_amount, tax_rate):
    gross_amount = to_decimal(total_amount)
    rate = normalize_tax_rate(tax_rate)

    if rate <= 0:
        return Decimal("0.000")

    net_amount = gross_amount / (Decimal("1") + rate)
    return truncate_three_decimals(gross_amount - net_amount)


def calculate_invoice_totals(goods_details):
    gross_amount = Decimal("0.00")
    tax_amount = Decimal("0.000")
    tax_buckets = {
        STANDARD_TAX_CODE: {
            "taxCategoryCode": STANDARD_TAX_CODE,
            "netAmount": Decimal("0.000"),
            "taxRate": str(STANDARD_TAX_RATE),
            "taxAmount": Decimal("0.000"),
            "grossAmount": Decimal("0.00"),
        },
        ZERO_TAX_CODE: {
            "taxCategoryCode": ZERO_TAX_CODE,
            "netAmount": Decimal("0.000"),
            "taxRate": str(int(ZERO_TAX_RATE)),
            "taxAmount": Decimal("0.000"),
            "grossAmount": Decimal("0.00"),
        },
    }

    for goods_detail in goods_details:
        line_gross_amount = to_decimal(goods_detail.get("total"))
        line_tax_amount = to_decimal(goods_detail.get("tax"))
        line_net_amount = truncate_three_decimals(line_gross_amount - line_tax_amount)
        tax_category_code, tax_rate_string = get_tax_category_details(goods_detail.get("taxRate"))

        gross_amount += line_gross_amount
        tax_amount += line_tax_amount

        bucket = tax_buckets[tax_category_code]
        bucket["taxRate"] = tax_rate_string
        bucket["grossAmount"] += line_gross_amount
        bucket["taxAmount"] += line_tax_amount
        bucket["netAmount"] += line_net_amount

    gross_amount = truncate_two_decimals(gross_amount)
    tax_amount = truncate_three_decimals(tax_amount)
    net_amount = truncate_three_decimals(gross_amount - tax_amount)

    formatted_tax_details = []
    for tax_category_code in [STANDARD_TAX_CODE, ZERO_TAX_CODE]:
        bucket = tax_buckets[tax_category_code]
        formatted_tax_details.append({
            "taxCategoryCode": bucket["taxCategoryCode"],
            "netAmount": float(truncate_three_decimals(bucket["netAmount"])),
            "taxRate": bucket["taxRate"],
            "taxAmount": float(truncate_three_decimals(bucket["taxAmount"])),
            "grossAmount": float(truncate_two_decimals(bucket["grossAmount"])),
            "exciseUnit": "",
            "exciseCurrency": "",
            "taxRateName": "",
        })

    return {
        "gross_amount": gross_amount,
        "tax_amount": tax_amount,
        "net_amount": net_amount,
        "tax_details": formatted_tax_details,
    }


def build_goods_detail(item, order_number):
    efris_item_data = get_item_efris_data(item)

    if not efris_item_data["product_code"]:
        raise EFRISIntegrationError(
            f"Missing EFRIS Product Code for invoice item row {getattr(item, 'idx', '')} item {getattr(item, 'item_code', '')}"
        )

    qty = to_decimal(item.qty)
    unit_price = get_sales_invoice_item_unit_price(item)
    tax_rate = get_sales_invoice_item_vat_rate(item)
    unit_of_measure = get_sales_invoice_item_uom_code(item)
    _, tax_rate_string = get_tax_category_details(tax_rate)
    total = truncate_two_decimals(qty * unit_price)
    tax = calculate_line_tax(total, tax_rate)

    return {
        "item": efris_item_data["goods_service_name"],
        "itemCode": efris_item_data["product_code"],
        "qty": float(qty),
        "unitOfMeasure": unit_of_measure,
        "unitPrice": float(unit_price),
        "total": float(total),
        "taxRate": tax_rate_string,
        "tax": float(tax),
        "discountTotal": "",
        "discountTaxRate": "",
        "orderNumber": str(order_number),
        "discountFlag": "2",
        "deemedFlag": "2",
        "exciseFlag": "2",
        "categoryId": "",
        "categoryName": "",
        "goodsCategoryId": efris_item_data["goods_category_id"],
        "goodsCategoryName": "",
        "exciseRate": "",
        "exciseRule": "",
        "exciseTax": "",
        "pack": "",
        "stick": "",
        "exciseUnit": "",
        "exciseCurrency": "",
        "exciseRateName": "",
        "vatApplicableFlag": "1",
        "deemedExemptCode": "",
        "vatProjectId": "",
        "vatProjectName": "",
        "totalWeight": "",
        "hsCode": "",
        "hsName": "",
        "pieceQty": "",
        "pieceMeasureUnit": "",
        "highSeaBondFlag": "2",
        "highSeaBondCode": "",
        "highSeaBondNo": "",
    }


def process_invoice_items(items):
    goods_details = []
    item_count = 0

    for item in items:
        item_count += 1
        goods_detail = build_goods_detail(item, len(goods_details))
        goods_details.append(goods_detail)

    invoice_totals = calculate_invoice_totals(goods_details)
    return goods_details, invoice_totals, item_count


def build_seller_details(efris_settings, doc):
    return {
        "tin": efris_settings.tin,
        "ninBrn": clean_brn(efris_settings.brn),
        "legalName": efris_settings.legal_name,
        "businessName": efris_settings.business_name,
        "address": "999 MBOGO ROAD OPPOSITE MBOGO COLLEGE KAWEMPE KAMPALA KAWEMPE DIVISION NORTH KAWEMPE DIVISION KAWEMPE 1",
        "mobilePhone": efris_settings.mobile_phone,
        "linePhone": efris_settings.line_phone,
        "emailAddress": efris_settings.email or "",
        "placeOfBusiness": efris_settings.place_of_business,
        "referenceNo": get_invoice_reference_no(doc),
        "branchId": "",
        "isCheckReferenceNo": "",
    }


def build_basic_information(efris_settings, doc, datetime_combined):
    owner_full_name = frappe.db.get_value(
        "User",
        doc.owner,
        "full_name"
    ) or doc.owner

    return {
        "invoiceNo": "",
        "antifakeCode": "",
        "deviceNo": efris_settings.device_number,
        "issuedDate": datetime_combined,
        "operator": owner_full_name,
        "currency": "UGX",
        "oriInvoiceId": "",
        "invoiceType": "1",
        "invoiceKind": "1",
        "dataSource": "105",
        "invoiceIndustryCode": "101",
        "isBatch": "0",
    }


def build_buyer_details(doc):
    buyer_type = BUYER_TYPE_MAPPING.get(doc.customer_group, DEFAULT_BUYER_TYPE)

    return {
        "buyerTin": doc.tax_id,
        "buyerNinBrn": "",
        "buyerPassportNum": "",
        "buyerLegalName": doc.customer_name or "",
        "buyerBusinessName": doc.customer_name or "",
        "buyerAddress": doc.customer_address or "",
        "buyerEmail": doc.contact_email or "",
        "buyerMobilePhone": doc.contact_mobile or "",
        "buyerLinePhone": "",
        "buyerPlaceOfBusi": "",
        "buyerType": buyer_type,
        "buyerCitizenship": "",
        "buyerSector": "1",
        "buyerReferenceNo": "",
        "nonResidentFlag": "0",
        "deliveryTermsCode": ""
    }


def build_buyer_extend():
    return {
        "propertyType": "",
        "district": "",
        "municipalityCounty": "",
        "divisionSubcounty": "",
        "town": "",
        "cellVillage": "",
        "effectiveRegistrationDate": "",
        "meterStatus": "",
    }


def build_summary(invoice_totals, item_count):
    return {
        "netAmount": float(invoice_totals["net_amount"]),
        "taxAmount": float(invoice_totals["tax_amount"]),
        "grossAmount": float(invoice_totals["gross_amount"]),
        "itemCount": item_count,
        "modeCode": "0",
        "remarks": "We appreciate your continued support",
        "qrCode": "",
    }


def build_invoice_data(efris_settings, doc, datetime_combined):
    goods_details, invoice_totals, item_count = process_invoice_items(doc.items)

    if not goods_details:
        raise EFRISIntegrationError("No items found in the invoice")

    invoice_data = {
        "sellerDetails": build_seller_details(efris_settings, doc),
        "basicInformation": build_basic_information(efris_settings, doc, datetime_combined),
        "buyerDetails": build_buyer_details(doc),
        "buyerExtend": build_buyer_extend(),
        "goodsDetails": goods_details,
        "taxDetails": invoice_totals["tax_details"],
        "summary": build_summary(invoice_totals, item_count),
        "extend": {
            "reason": "",
            "reasonCode": ""
        },
        "importServicesSeller": {
            "importBusinessName": "",
            "importEmailAddress": "",
            "importContactNumber": "",
            "importAddress": "",
            "importInvoiceDate": "",
            "importAttachmentName": "",
            "importAttachmentContent": "",
        },
        "airlineGoodsDetails": [{
            "item": "",
            "itemCode": "",
            "qty": "",
            "unitOfMeasure": "",
            "unitPrice": "",
            "total": "",
            "taxRate": "",
            "tax": "",
            "discountTotal": "",
            "discountTaxRate": "",
            "orderNumber": "",
            "discountFlag": "",
            "deemedFlag": "",
            "exciseFlag": "",
            "categoryId": "",
            "categoryName": "",
            "goodsCategoryId": "",
            "goodsCategoryName": "",
            "exciseRate": "",
            "exciseRule": "",
            "exciseTax": "",
            "pack": "1",
            "stick": "",
            "exciseUnit": "",
            "exciseCurrency": "",
            "exciseRateName": "",
        }],
        "edcDetails": {
            "tankNo": "",
            "pumpNo": "",
            "nozzleNo": "",
            "controllerNo": "",
            "acquisitionEquipmentNo": "",
            "levelGaugeNo": "",
            "mvrn": "",
        },
    }
    
    return invoice_data, invoice_totals, item_count


def build_global_info(efris_settings, doc, invoice_totals, goods_details):
    data_exchange_id = uuid.uuid4().hex[:32]
    current_time = datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    reference_no = get_invoice_reference_no(doc)

    owner_full_name = frappe.db.get_value(
        "User",
        doc.owner,
        "full_name"
    ) or doc.owner

    item_description = ", ".join([item["item"] for item in goods_details[:3]])[:100]

    return {
        "appId": "AP04",
        "version": "1.1.20191201",
        "dataExchangeId": data_exchange_id,
        "interfaceCode": "T109",
        "requestCode": "TP",
        "requestTime": current_time,
        "responseCode": "TA",
        "userName": "admin",
        "deviceMAC": "B47720524158",
        "deviceNo": efris_settings.device_number,
        "tin": efris_settings.tin,
        "brn": clean_brn(efris_settings.brn),
        "taxpayerID": "999000002030357",
        "longitude": "32.61665",
        "latitude": "0.36601",
        "agentType": "0",
        "extendField": {
            "responseDateFormat": "dd/MM/yyyy",
            "responseTimeFormat": "dd/MM/yyyy HH:mm:ss",
            "referenceNo": reference_no,
            "operatorName": owner_full_name,
            "itemDescription": item_description,
            "currency": "UGX",
            "grossAmount": str(invoice_totals["gross_amount"]),
            "taxAmount": str(truncate_two_decimals(invoice_totals["tax_amount"])),
        },
    }


def encrypt_invoice_data(invoice_data):
    encrypted_result = encrypt_dynamic_json(invoice_data)
    if not encrypted_result.get("success"):
        raise EFRISIntegrationError(f"Encryption failed: {encrypted_result.get('error')}")
    
    return encrypted_result


def decrypt_efris_response_content(encrypted_content, aes_key_hex):
    if not encrypted_content:
        return {}

    aes_key_bytes = bytes.fromhex(aes_key_hex)
    compressed_bytes = base64.b64decode(encrypted_content)

    try:
        encrypted_bytes = gzip.decompress(compressed_bytes)
    except Exception:
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

    return json.loads(decoded_text)


def update_sales_invoice_efris_fields(doc, decrypted_response):
    basic_information = decrypted_response.get("basicInformation") or {}
    summary = decrypted_response.get("summary") or {}

    field_map = {
        "custom_qr_code": summary.get("qrCode"),
        "custom_fdn": basic_information.get("invoiceId"),
        "custom_invoice_number": basic_information.get("invoiceNo"),
        "custom_verification_code": basic_information.get("antifakeCode"),
    }

    device_number = basic_information.get("deviceNo")
    if device_number:
        field_map["custom_device_number"] = device_number

    field_map["custom_efris_synced"] = 1

    for fieldname, value in field_map.items():
        if value in (None, ""):
            continue
        if fieldname == "custom_efris_synced":
            doc.db_set(fieldname, value, update_modified=False)
        else:
            doc.db_set(fieldname, str(value), update_modified=False)

    frappe.db.commit()


def sync_efris_response_to_sales_invoice(doc, response_data, aes_key=""):
    encrypted_content = response_data.get("data", {}).get("content")
    if not encrypted_content or not aes_key:
        return

    decrypted_response = decrypt_efris_response_content(encrypted_content, aes_key)
    update_sales_invoice_efris_fields(doc, decrypted_response)


def build_post_data(encrypted_result, global_info):
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
        "globalInfo": global_info,
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": ""
        },
    }


def submit_to_efris(efris_settings, data_to_post, aes_key="", reference_docname=""):
    headers = {"Content-Type": "application/json"}
    server_url = efris_settings.server_url

    try:
        response = requests.post(server_url, json=data_to_post, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json(), headers, server_url

    except requests.exceptions.Timeout:
        error_msg = "Request timed out. Please try again."
        log_integration_request(
            'Failed',
            server_url,
            headers,
            data_to_post,
            {},
            error_msg,
            aes_key=aes_key,
            reference_docname=reference_docname,
        )
        raise EFRISIntegrationError(error_msg)

    except requests.exceptions.RequestException as e:
        error_msg = f"API request failed: {str(e)}"
        log_integration_request(
            'Failed',
            server_url,
            headers,
            data_to_post,
            {},
            error_msg,
            aes_key=aes_key,
            reference_docname=reference_docname,
        )
        raise EFRISIntegrationError(error_msg)


def handle_efris_response(doc, response_data, headers, server_url, data_to_post, aes_key=""):
    return_message = response_data.get("returnStateInfo", {}).get("returnMessage", "")

    if return_message == "SUCCESS":
        frappe.msgprint("Sales Invoice successfully submitted to EFRIS URA via T109.")
        log_integration_request(
            'Completed',
            server_url,
            headers,
            data_to_post,
            response_data,
            aes_key=aes_key,
            reference_docname=doc.name,
        )
        try:
            sync_efris_response_to_sales_invoice(doc, response_data, aes_key=aes_key)
        except Exception:
            frappe.logger().error(
                "EFRIS Sales Invoice Response Sync Error\n%s",
                frappe.get_traceback(),
            )
            frappe.msgprint(
                "Invoice was sent to EFRIS, but the returned EFRIS fields could not be updated automatically."
            )
    else:
        log_integration_request(
            'Failed',
            server_url,
            headers,
            data_to_post,
            response_data,
            return_message,
            aes_key=aes_key,
            reference_docname=doc.name,
        )
        frappe.throw(
            title="EFRIS T109 Submission Failed",
            msg=return_message
        )


@frappe.whitelist()
def on_send(invoice_name=None, doc=None, event=None):
    validate_send_invoice_user()

    if invoice_name:
        target_invoice_name = invoice_name
    elif isinstance(doc, str):
        target_invoice_name = doc
    elif doc:
        target_invoice_name = doc.name
    else:
        frappe.throw("Sales Invoice is required")

    doc = frappe.get_doc("Sales Invoice", target_invoice_name)
    sync_sales_invoice_efris_prices(doc)
    process_invoice_t109(doc)

    return {
        "success": True,
        "queued": False,
        "message": f"Invoice {target_invoice_name} submitted to EFRIS.",
    }


def process_invoice_t109(doc):
    efris_settings = get_efris_settings()
    datetime_combined = f"{doc.posting_date} {doc.posting_time}"
    invoice_data, invoice_totals, item_count = build_invoice_data(efris_settings, doc, datetime_combined)
    encrypted_result = encrypt_invoice_data(invoice_data)
    global_info = build_global_info(
        efris_settings,
        doc,
        invoice_totals,
        invoice_data["goodsDetails"]
    )
    data_to_post = build_post_data(encrypted_result, global_info)
    aes_key_used = encrypted_result.get("aes_key", "")
    response_data, headers, server_url = submit_to_efris(
        efris_settings,
        data_to_post,
        aes_key=aes_key_used,
        reference_docname=doc.name,
    )
    handle_efris_response(doc, response_data, headers, server_url, data_to_post, aes_key=aes_key_used)
