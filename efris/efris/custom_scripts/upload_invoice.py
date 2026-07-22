import json
import uuid
import gzip
import base64
from decimal import Decimal, InvalidOperation, ROUND_DOWN
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
EXEMPT_TAX_CODE = "03"
EXEMPT_TAX_RATE = "-"
SALES_INVOICE_UOM_MAPPING = {
    "pieces": "PP",
    "piece": "PP",
    "pp-piece": "PP",
    "pair": "111",
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
EFRIS_OPERATOR_NAME = "Hardev"
EFRIS_SEND_INVOICE_USER = "reports@autozonepro.org"
class EFRISIntegrationError(Exception):
    pass


TWO_PLACES = Decimal("0.01")
THREE_PLACES = Decimal("0.001")
STANDARD_TAX_RATE_DECIMAL = Decimal("0.18")
ZERO_TAX_RATE_DECIMAL = Decimal("0.00")


def get_efris_request_time():
    return datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def get_invoice_reference_no(doc):
    for item in getattr(doc, "items", []) or []:
        sales_order = getattr(item, "sales_order", None)
        if sales_order:
            return sales_order

    return doc.name


def log_integration_request(
    status,
    url,
    headers,
    data,
    response,
    error="",
    aes_key="",
    reference_docname="",
    service="T109 Goods Upload",
    reference_doctype="Sales Invoice",
):
    valid_statuses = ["", "Queued", "Authorized", "Completed", "Cancelled", "Failed"]
    status = status if status in valid_statuses else "Failed"

    integration_request = frappe.get_doc({
        "doctype": "Integration Request",
        "integration_type": "Remote",
        "method": "POST",
        "integration_request_service": service,
        "is_remote_request": True,
        "status": status,
        "custom_aes_key": aes_key or "",
        "url": url,
        "request_headers": json.dumps(headers, indent=4),
        "data": json.dumps(data, indent=4),
        "output": json.dumps(response, indent=4),
        "error": error,
        "reference_doctype": reference_doctype if reference_docname else "",
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


def get_item_efris_data(item_row):
    master_product_code = ""
    master_goods_name = ""
    master_category_id = ""
    if item_row.item_code:
        master_values = frappe.get_cached_value(
            "Item",
            item_row.item_code,
            [
                "custom_efris_product_code",
                "custom_goods_service_name",
                "custom_goods_category_id",
            ],
        ) or ("", "", "")
        master_product_code, master_goods_name, master_category_id = master_values

    return {
        "product_code": str(
            getattr(item_row, "custom_efris_product_code", "")
            or getattr(item_row, "custom_efrsis_product_code", "")
            or master_product_code
            or ""
        ).strip(),
        "goods_service_name": str(
            getattr(item_row, "custom_efris_item_name", "")
            or getattr(item_row, "custom_goods_service_name", "")
            or master_goods_name
            or getattr(item_row, "item_name", "")
            or ""
        ).strip(),
        "goods_category_id": str(
            getattr(item_row, "custom_goods_category_id", "")
            or master_category_id
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

    if isinstance(unit_price, str):
        unit_price = unit_price.strip().replace(",", "")

    try:
        return truncate_two_decimals(unit_price)
    except Exception as exc:
        raise EFRISIntegrationError(
            f"Invalid Item price '{unit_price}' on invoice item row {getattr(item, 'idx', '')} item {getattr(item, 'item_code', '')}"
        ) from exc


def get_sales_invoice_item_vat_rate(item):
    vat_value = getattr(item, "custom_vat", None)
    if vat_value in (None, ""):
        raise EFRISIntegrationError(
            f"Missing VAT on invoice item row {getattr(item, 'idx', '')} item {getattr(item, 'item_code', '')}"
        )

    vat_string = str(vat_value).strip().replace("%", "")
    if vat_string == EXEMPT_TAX_RATE:
        return EXEMPT_TAX_RATE

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
    item_uom = str(getattr(item, "custom_efris_uom", "") or "").strip()
    mapped_uom = SALES_INVOICE_UOM_MAPPING.get(item_uom.lower())

    if not mapped_uom:
        raise EFRISIntegrationError(
            f"Unsupported Sales Invoice Item EFRIS UOM '{item_uom}' on row {getattr(item, 'idx', '')}. "
            "Add it to SALES_INVOICE_UOM_MAPPING in upload_invoice.py."
        )

    return mapped_uom


def validate_sales_invoice_efris_stock_difference(doc):
    from efris.efris.custom_scripts.efris_stock_ledger import (
        get_container_stock_qty,
        get_latest_item_balance,
    )

    mismatch_rows = []

    for item in getattr(doc, "items", []) or []:
        efris_data = get_item_efris_data(item)
        efris_qty = truncate_two_decimals(
            get_latest_item_balance(
                item_code=getattr(item, "item_code", "") or "",
                efris_product_code=efris_data.get("product_code", "") or "",
            )
        )
        warehouse_qty = getattr(item, "actual_qty", None)
        if warehouse_qty in (None, ""):
            warehouse_qty = getattr(item, "company_total_stock", 0)
        warehouse_qty = truncate_two_decimals(warehouse_qty)
        containers_qty = truncate_two_decimals(
            get_container_stock_qty(getattr(item, "item_code", "") or "")
        )
        difference = truncate_two_decimals(efris_qty - (warehouse_qty + containers_qty))

        if difference < Decimal("0.00"):
            mismatch_rows.append(
                "Row {row}: {item_code} - EFRIS {efris_qty}, Warehouse {warehouse_qty}, "
                "Containers {containers_qty}, Difference {difference} is negative".format(
                    row=getattr(item, "idx", "") or "?",
                    item_code=getattr(item, "item_code", "") or getattr(item, "item_name", "") or "Unknown Item",
                    warehouse_qty=warehouse_qty,
                    containers_qty=containers_qty,
                    efris_qty=efris_qty,
                    difference=difference,
                )
            )

    if mismatch_rows:
        raise EFRISIntegrationError(
            "Cannot submit to URA EFRIS because stock validation failed:\n" + "\n".join(mismatch_rows)
        )


def filter_efris_payload_items(doc, excluded_row_names=None, quantity_overrides=None):
    if isinstance(excluded_row_names, str):
        try:
            excluded_row_names = frappe.parse_json(excluded_row_names)
        except (TypeError, ValueError):
            raise EFRISIntegrationError("The selected EFRIS invoice items are invalid.")

    excluded_row_names = excluded_row_names or []
    if not isinstance(excluded_row_names, (list, tuple, set)):
        raise EFRISIntegrationError("The selected EFRIS invoice items are invalid.")

    if isinstance(quantity_overrides, str):
        try:
            quantity_overrides = frappe.parse_json(quantity_overrides)
        except (TypeError, ValueError):
            raise EFRISIntegrationError("The EFRIS item quantities are invalid.")

    quantity_overrides = quantity_overrides or {}
    if not isinstance(quantity_overrides, dict):
        raise EFRISIntegrationError("The EFRIS item quantities are invalid.")

    excluded_names = {str(row_name) for row_name in excluded_row_names if row_name}
    invoice_row_names = {item.name for item in getattr(doc, "items", []) or []}
    override_names = {str(row_name) for row_name in quantity_overrides}
    if (excluded_names | override_names) - invoice_row_names:
        raise EFRISIntegrationError("Some selected items do not belong to this Sales Invoice.")

    included_items = []
    adjusted_names = []
    for item in doc.items:
        if item.name in excluded_names:
            continue

        if item.name in quantity_overrides:
            try:
                quantity = Decimal(str(quantity_overrides[item.name]))
            except (InvalidOperation, TypeError, ValueError):
                raise EFRISIntegrationError(
                    f"Invalid EFRIS quantity for row {getattr(item, 'idx', '')} item {item.item_code}."
                )

            if quantity < 0:
                raise EFRISIntegrationError(
                    f"EFRIS quantity cannot be negative for row {getattr(item, 'idx', '')} item {item.item_code}."
                )
            if quantity == 0:
                excluded_names.add(item.name)
                continue

            item.qty = float(quantity)
            adjusted_names.append(item.name)

        included_items.append(item)

    if not included_items:
        raise EFRISIntegrationError("At least one item must remain in the EFRIS submission.")

    doc.set("items", included_items)
    for index, item in enumerate(doc.items, start=1):
        item.idx = index

    return sorted(excluded_names), sorted(adjusted_names)


def to_decimal(value):
    if value in (None, ""):
        return Decimal("0")

    return Decimal(str(value))


def truncate_two_decimals(value):
    return to_decimal(value).quantize(TWO_PLACES, rounding=ROUND_DOWN)


def truncate_three_decimals(value):
    return to_decimal(value).quantize(THREE_PLACES, rounding=ROUND_DOWN)


def get_tax_category_details(tax_rate):
    if tax_rate in (None, ""):
        raise EFRISIntegrationError("Missing VAT rate on Sales Invoice Item.")

    if str(tax_rate).strip() == EXEMPT_TAX_RATE:
        return EXEMPT_TAX_CODE, EXEMPT_TAX_RATE

    normalized_tax_rate = truncate_two_decimals(tax_rate)

    if normalized_tax_rate == STANDARD_TAX_RATE_DECIMAL:
        return STANDARD_TAX_CODE, str(STANDARD_TAX_RATE)

    if normalized_tax_rate == ZERO_TAX_RATE_DECIMAL:
        return ZERO_TAX_CODE, str(int(ZERO_TAX_RATE))

    raise EFRISIntegrationError(
        f"Unsupported EFRIS tax rate {tax_rate}. Only 0.18, 0, and - are currently supported."
    )


def calculate_line_tax(total_amount, tax_rate):
    if str(tax_rate).strip() == EXEMPT_TAX_RATE:
        return Decimal("0.000")

    gross_amount = to_decimal(total_amount)
    rate = truncate_two_decimals(tax_rate)

    if rate <= 0:
        return Decimal("0.000")

    net_amount = gross_amount / (Decimal("1") + rate)
    return truncate_three_decimals(gross_amount - net_amount)


def calculate_invoice_totals(goods_details):
    gross_amount = Decimal("0.00")
    tax_amount = Decimal("0.000")
    used_tax_categories = set()
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
        EXEMPT_TAX_CODE: {
            "taxCategoryCode": EXEMPT_TAX_CODE,
            "netAmount": Decimal("0.000"),
            "taxRate": EXEMPT_TAX_RATE,
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
        used_tax_categories.add(tax_category_code)
        bucket["taxRate"] = tax_rate_string
        bucket["grossAmount"] += line_gross_amount
        bucket["taxAmount"] += line_tax_amount
        bucket["netAmount"] += line_net_amount

    gross_amount = truncate_two_decimals(gross_amount)
    tax_amount = truncate_three_decimals(tax_amount)
    net_amount = truncate_three_decimals(gross_amount - tax_amount)

    formatted_tax_details = []
    for tax_category_code in [STANDARD_TAX_CODE, ZERO_TAX_CODE, EXEMPT_TAX_CODE]:
        if tax_category_code not in used_tax_categories:
            continue

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

    for item in items:
        if getattr(item, "__deleted", 0):
            continue

        qty = to_decimal(getattr(item, "qty", 0))
        if qty <= 0:
            continue

        goods_details.append(build_goods_detail(item, len(goods_details)))

    if not goods_details:
        raise EFRISIntegrationError("No valid Sales Invoice items remain to send to EFRIS.")

    return goods_details, calculate_invoice_totals(goods_details)


def build_invoice_data(efris_settings, doc):
    goods_details, invoice_totals = process_invoice_items(doc.items)
    buyer_name = (doc.customer_name or "").strip().split(" ")[0]

    invoice_data = {
        "sellerDetails": {
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
        },
        "basicInformation": {
            "invoiceNo": "",
            "antifakeCode": "",
            "deviceNo": efris_settings.device_number,
            "issuedDate": f"{doc.posting_date} {doc.posting_time}",
            "operator": EFRIS_OPERATOR_NAME,
            "currency": "UGX",
            "oriInvoiceId": "",
            "invoiceType": "1",
            "invoiceKind": "1",
            "dataSource": "105",
            "invoiceIndustryCode": "101",
            "isBatch": "0",
        },
        "buyerDetails": {
            "buyerTin": doc.tax_id,
            "buyerNinBrn": "",
            "buyerPassportNum": "",
            "buyerLegalName": buyer_name,
            "buyerBusinessName": buyer_name,
            "buyerAddress": doc.customer_address or "",
            "buyerEmail": doc.contact_email or "",
            "buyerMobilePhone": "",
            "buyerLinePhone": "",
            "buyerPlaceOfBusi": "",
            "buyerType": BUYER_TYPE_MAPPING.get(doc.customer_group, DEFAULT_BUYER_TYPE),
            "buyerCitizenship": "",
            "buyerSector": "1",
            "buyerReferenceNo": "",
            "nonResidentFlag": "0",
            "deliveryTermsCode": "",
        },
        "buyerExtend": {
            "propertyType": "",
            "district": "",
            "municipalityCounty": "",
            "divisionSubcounty": "",
            "town": "",
            "cellVillage": "",
            "effectiveRegistrationDate": "",
            "meterStatus": "",
        },
        "goodsDetails": goods_details,
        "taxDetails": invoice_totals["tax_details"],
        "summary": {
            "netAmount": float(invoice_totals["net_amount"]),
            "taxAmount": float(invoice_totals["tax_amount"]),
            "grossAmount": float(invoice_totals["gross_amount"]),
            "itemCount": len(goods_details),
            "modeCode": "0",
            "remarks": "We appreciate your continued support",
            "qrCode": "",
        },
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

    return invoice_data, invoice_totals


def build_global_info(efris_settings, doc, invoice_totals, goods_details):
    data_exchange_id = uuid.uuid4().hex[:32]
    current_time = get_efris_request_time()
    reference_no = get_invoice_reference_no(doc)

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
            "operatorName": EFRIS_OPERATOR_NAME,
            "itemDescription": item_description,
            "currency": "UGX",
            "grossAmount": str(invoice_totals["gross_amount"]),
            "taxAmount": str(truncate_two_decimals(invoice_totals["tax_amount"])),
        },
    }


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


def update_sales_invoice_efris_fields(doc, decrypted_response, stock_movement_doc=None):
    basic_information = decrypted_response.get("basicInformation") or {}
    summary = decrypted_response.get("summary") or {}

    field_map = {
        "custom_qr_code": summary.get("qrCode") or basic_information.get("qrCode"),
        "custom_fdn": basic_information.get("invoiceNo") or decrypted_response.get("invoiceNo"),
        "custom_invoice_number": basic_information.get("invoiceId") or decrypted_response.get("invoiceId"),
        "custom_verification_code": (
            basic_information.get("antifakeCode") or decrypted_response.get("antifakeCode")
        ),
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

    try:
        from efris.efris.custom_scripts.efris_stock_ledger import (
            process_sales_invoice_efris_stock_movement,
        )

        movement_doc = stock_movement_doc or doc
        movement_doc.custom_efris_synced = 1
        process_sales_invoice_efris_stock_movement(movement_doc)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"EFRIS Stock Ledger Sync Error - {doc.name}",
        )
        frappe.msgprint(
            "Invoice was sent to EFRIS, but the EFRIS stock ledger could not be updated automatically."
        )


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


def handle_efris_response(
    doc,
    response_data,
    headers,
    server_url,
    data_to_post,
    aes_key="",
    stock_movement_doc=None,
):
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
            encrypted_content = response_data.get("data", {}).get("content")
            settings_aes_key = (
                frappe.get_cached_value("EFRIS Settings", "EFRIS Settings", "aes_key") or ""
            ).strip()
            decryption_key = settings_aes_key or aes_key
            if encrypted_content and decryption_key:
                decrypted_response = decrypt_efris_response_content(encrypted_content, decryption_key)
                update_sales_invoice_efris_fields(
                    doc,
                    decrypted_response,
                    stock_movement_doc=stock_movement_doc,
                )
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
def on_send(
    invoice_name,
    excluded_row_names=None,
    quantity_overrides=None,
):
    try:
        if frappe.session.user != EFRIS_SEND_INVOICE_USER:
            frappe.throw("You are not allowed to send invoices to EFRIS.")

        payload_doc = frappe.get_doc("Sales Invoice", invoice_name)
        response_doc = frappe.get_doc("Sales Invoice", invoice_name)
        excluded_names, adjusted_names = filter_efris_payload_items(
            payload_doc,
            excluded_row_names,
            quantity_overrides,
        )

        validate_sales_invoice_efris_stock_difference(payload_doc)

        from efris.efris.custom_scripts.efris_stock_precheck import build_zero_stock_rows

        shortage_rows = [
            row for row in build_zero_stock_rows(payload_doc)
            if truncate_two_decimals(row.get("balance", 0)) <= Decimal("0.00")
        ]
        if shortage_rows:
            shortage_items = ", ".join(
                [row.get("item_code") or row.get("item_name") or row.get("efris_product_code") or "Unknown Item" for row in shortage_rows]
            )
            raise EFRISIntegrationError(
                f"Some items still have insufficient EFRIS stock and were not removed from the payload: {shortage_items}"
            )

        sync_sales_invoice_efris_prices(payload_doc)
        process_invoice_t109(payload_doc, response_doc=response_doc)

        return {
            "success": True,
            "queued": False,
            "excluded_items": len(excluded_names),
            "adjusted_items": len(adjusted_names),
            "message": f"Invoice {invoice_name} submitted to EFRIS.",
        }
    except EFRISIntegrationError as error:
        frappe.throw(str(error), title="EFRIS Send Blocked")
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"EFRIS Send Invoice Error - {invoice_name or 'Unknown Invoice'}",
        )
        raise


def process_invoice_t109(doc, response_doc=None):
    efris_settings = get_efris_settings()
    invoice_data = {}
    data_to_post = {}
    aes_key_used = ""
    response_doc = response_doc or doc

    try:
        invoice_data, invoice_totals = build_invoice_data(efris_settings, doc)
        encrypted_result = encrypt_dynamic_json(invoice_data)
        if not encrypted_result.get("success"):
            raise EFRISIntegrationError(f"Encryption failed: {encrypted_result.get('error')}")

        global_info = build_global_info(
            efris_settings,
            doc,
            invoice_totals,
            invoice_data["goodsDetails"],
        )
        data_to_post = {
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
                "returnMessage": "",
            },
        }
        aes_key_used = encrypted_result.get("aes_key", "")
        response_data, headers, server_url = submit_to_efris(
            efris_settings,
            data_to_post,
            aes_key=aes_key_used,
            reference_docname=response_doc.name,
        )
        handle_efris_response(
            response_doc,
            response_data,
            headers,
            server_url,
            data_to_post,
            aes_key=aes_key_used,
            stock_movement_doc=doc,
        )
    except Exception as exc:
        if not data_to_post:
            log_integration_request(
                "Failed",
                getattr(efris_settings, "server_url", ""),
                {},
                invoice_data,
                {},
                str(exc),
                aes_key=aes_key_used,
                reference_docname=response_doc.name,
            )
        raise
