import json
import uuid
from datetime import datetime, timezone, timedelta
import frappe
import requests

from efris.efris.background_tasks.encryption import encrypt_dynamic_json

## East Africa Time (UTC+3)
EAT_TIMEZONE = timezone(timedelta(hours=3))

## Dynamic tax rate variables
STANDARD_TAX_RATE = 0.18  # 18%
ZERO_TAX_RATE = 0.0      # Zero rate

## Tax category codes
STANDARD_TAX_CODE = "01"
ZERO_TAX_CODE = "02"

## Buyer type mapping
BUYER_TYPE_MAPPING = {
    "B2B": "0",
    "B2C": "1",
    "Foreigner": "2",
    "B2G": "3"
}

## Default values
DEFAULT_BUYER_TYPE = "1"  # B2C


class EFRISIntegrationError(Exception):
    ## Custom exception for EFRIS integration errors
    pass


def log_integration_request(status, url, headers, data, response, error=""):
    ## Log integration request to Integration Request doctype
    valid_statuses = ["", "Queued", "Authorized", "Completed", "Cancelled", "Failed"]
    status = status if status in valid_statuses else "Failed"
    
    integration_request = frappe.get_doc({
        "doctype": "Integration Request",
        "integration_type": "Remote",
        "method": "POST",
        "integration_request_service": "T109 Goods Upload",
        "is_remote_request": True,
        "status": status,
        "url": url,
        "request_headers": json.dumps(headers, indent=4),
        "data": json.dumps(data, indent=4),
        "output": json.dumps(response, indent=4),
        "error": error,
        "execution_time": datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    })
    integration_request.insert(ignore_permissions=True)
    frappe.db.commit()


def get_efris_settings():
    ## Fetch and validate EFRIS settings for current company
    company = frappe.defaults.get_user_default("company")
    if not company:
        raise EFRISIntegrationError("No default company set for the current session")

    efris_settings = frappe.get_doc("EFRIS Settings", {"company": company})
    
    if not efris_settings.is_active:
        raise EFRISIntegrationError("EFRIS integration is disabled")
    
    if not efris_settings.tin or not efris_settings.brn:
        raise EFRISIntegrationError("TIN and BRN are required in EFRIS Settings")
    
    return efris_settings


def clean_brn(brn):
    ## Remove leading slash and whitespace from BRN
    return brn.strip().lstrip("/") if brn else ""


def build_goods_detail(item, order_number):
    ## Build goods detail object for a single item
    return {
        "item": item.custom_goods_service_name,
        "itemCode": item.custom_efrsis_product_code,
        "qty": item.qty,
        "unitOfMeasure": item.custom_uom_code_efris,
        "unitPrice": item.rate,
        "total": item.amount,
        "taxRate": str(STANDARD_TAX_RATE),
        "tax": round(item.amount - item.net_amount, 3),
        "discountTotal": "",
        "discountTaxRate": "",
        "orderNumber": str(order_number),
        "discountFlag": "2",
        "deemedFlag": "2",
        "exciseFlag": "2",
        "categoryId": "",
        "categoryName": "",
        "goodsCategoryId": item.custom_goods_category_id,
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
    ## Process all invoice items and build goods details
    goods_details = []
    total_tax_amount = 0
    item_count = 0

    for item in items:
        item_count += 1
        
        ## Calculate tax amount
        tax_amount = round(item.amount - item.net_amount, 3)
        total_tax_amount += tax_amount
        
        ## Build goods detail
        goods_detail = build_goods_detail(item, len(goods_details))
        goods_details.append(goods_detail)

    return goods_details, total_tax_amount, item_count


def build_seller_details(efris_settings, doc):
    ## Build seller details section
    return {
        "tin": efris_settings.tin,
        "ninBrn": clean_brn(efris_settings.brn),
        "legalName": efris_settings.legal_name,
        "businessName": efris_settings.business_name,
        "address": "999 MBOGO ROAD OPPOSITE MBOGO COLLEGE KAWEMPE KAMPALA KAWEMPE DIVISION NORTH KAWEMPE DIVISION KAWEMPE 1",
        "mobilePhone": efris_settings.mobile_phone,
        "linePhone": efris_settings.line_phone,
        "emailAddress": efris_settings.email_phone,
        "placeOfBusiness": efris_settings.place_of_business,
        "referenceNo": doc.name,
        "branchId": "",
        "isCheckReferenceNo": "",
    }


def build_basic_information(efris_settings, doc, datetime_combined):
    ## Build basic information section
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
    ## Build buyer details section
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
    ## Build buyer extend section
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


def build_tax_details(total_tax_amount, gross_amount):
    ## Build tax details with two categories: zero rate and standard 18%
    tax_details = []
    
    ## Standard 18% tax category
    standard_tax = {
        "taxCategoryCode": STANDARD_TAX_CODE,
        "netAmount": round(gross_amount - total_tax_amount, 3),
        "taxRate": str(STANDARD_TAX_RATE),
        "taxAmount": round(total_tax_amount, 3),
        "grossAmount": round(gross_amount, 3),
        "exciseUnit": "",
        "exciseCurrency": "",
        "taxRateName": "",
    }
    tax_details.append(standard_tax)
    
    ## Zero rate tax category (empty/not applicable)
    zero_tax = {
        "taxCategoryCode": ZERO_TAX_CODE,
        "netAmount": 0,
        "taxRate": str(ZERO_TAX_RATE),
        "taxAmount": 0,
        "grossAmount": 0,
        "exciseUnit": "",
        "exciseCurrency": "",
        "taxRateName": "",
    }
    tax_details.append(zero_tax)
    
    return tax_details


def build_summary(doc, total_tax_amount, item_count):
    ## Build summary section
    return {
        "netAmount": round(doc.total - total_tax_amount, 3),
        "taxAmount": round(total_tax_amount, 3),
        "grossAmount": round(doc.total, 3),
        "itemCount": item_count,
        "modeCode": "0",
        "remarks": "We appreciate your continued support",
        "qrCode": "",
    }


def build_extend():
    ## Build extend section
    return {
        "reason": "",
        "reasonCode": ""
    }


def build_import_services_seller():
    ## Build import services seller section
    return {
        "importBusinessName": "",
        "importEmailAddress": "",
        "importContactNumber": "",
        "importAddress": "",
        "importInvoiceDate": "",
        "importAttachmentName": "",
        "importAttachmentContent": "",
    }


def build_airline_goods_details():
    ## Build airline goods details section
    return [{
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
    }]


def build_edc_details():
    ## Build EDC details section
    return {
        "tankNo": "",
        "pumpNo": "",
        "nozzleNo": "",
        "controllerNo": "",
        "acquisitionEquipmentNo": "",
        "levelGaugeNo": "",
        "mvrn": "",
    }


def build_invoice_data(efris_settings, doc, datetime_combined):
    ## Build complete invoice data structure for EFRIS T109 submission
    goods_details, total_tax_amount, item_count = process_invoice_items(doc.items)
    
    if not goods_details:
        raise EFRISIntegrationError("No items found in the invoice")
    
    ## Build invoice data structure
    invoice_data = {
        "sellerDetails": build_seller_details(efris_settings, doc),
        "basicInformation": build_basic_information(efris_settings, doc, datetime_combined),
        "buyerDetails": build_buyer_details(doc),
        "buyerExtend": build_buyer_extend(),
        "goodsDetails": goods_details,
        "taxDetails": build_tax_details(total_tax_amount, doc.total),
        "summary": build_summary(doc, total_tax_amount, item_count),
        "extend": build_extend(),
        "importServicesSeller": build_import_services_seller(),
        "airlineGoodsDetails": build_airline_goods_details(),
        "edcDetails": build_edc_details(),
    }
    
    return invoice_data, total_tax_amount, item_count


def build_global_info(efris_settings, doc, total_tax_amount, goods_details):
    ## Build global info section for T109
    data_exchange_id = uuid.uuid4().hex[:32]
    current_time = datetime.now(EAT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

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
            "referenceNo": doc.name,
            "operatorName": owner_full_name,
            "itemDescription": item_description,
            "currency": "UGX",
            "grossAmount": str(round(doc.total, 2)),
            "taxAmount": str(round(total_tax_amount, 2)),
        },
    }


def encrypt_invoice_data(invoice_data):
    ## Encrypt invoice data using encryption service
    frappe.log_error(
        title="EFRIS Invoice Data Before Encryption",
        message=json.dumps(invoice_data, indent=2)
    )
    
    encrypted_result = encrypt_dynamic_json(invoice_data)
    if not encrypted_result.get("success"):
        raise EFRISIntegrationError(f"Encryption failed: {encrypted_result.get('error')}")
    
    return encrypted_result


def build_post_data(encrypted_result, global_info):
    ## Build complete POST data for T109 API request
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


def submit_to_efris(efris_settings, data_to_post):
    ## Submit invoice data to EFRIS T109 API
    headers = {"Content-Type": "application/json"}
    server_url = efris_settings.server_url
    
    try:
        response = requests.post(server_url, json=data_to_post, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json(), headers, server_url
        
    except requests.exceptions.Timeout:
        error_msg = "Request timed out. Please try again."
        log_integration_request('Failed', server_url, headers, data_to_post, {}, error_msg)
        raise EFRISIntegrationError(error_msg)
        
    except requests.exceptions.RequestException as e:
        error_msg = f"API request failed: {str(e)}"
        log_integration_request('Failed', server_url, headers, data_to_post, {}, error_msg)
        raise EFRISIntegrationError(error_msg)


def handle_efris_response(doc, response_data, headers, server_url, data_to_post):
    ## Handle EFRIS T109 API response
    ## Only saves document on SUCCESS - otherwise throws error to prevent submission
    
    return_message = response_data.get("returnStateInfo", {}).get("returnMessage", "")
    
    ## Check if successful
    if return_message == "SUCCESS":
        frappe.msgprint("Sales Invoice successfully submitted to EFRIS URA via T109.")
        
        ## Log successful request
        log_integration_request('Completed', server_url, headers, data_to_post, response_data)
        
        ## ONLY save on success
        doc.save()
    else:
        ## Log failed request
        log_integration_request('Failed', server_url, headers, data_to_post, response_data, return_message)
        ## THROW ERROR - prevents submission, document stays in draft
        frappe.throw(
            title="EFRIS T109 Submission Failed",
            msg=return_message
        )

@frappe.whitelist()
def on_send(doc, event):
    ## Main entry point for EFRIS T109 invoice submission
    ## KEY LOGIC: Only doc.save() is called on SUCCESS
    ## If any error occurs, frappe.throw() prevents submission
    
    try:
        process_invoice_t109(doc)
        
    except Exception as e:
        ## frappe.throw() prevents document submission
        ## Document stays in Draft status
        frappe.throw(str(e))


def process_invoice_t109(doc):
    ## Process T109 invoice submission
    ## KEY LOGIC:
    ## - If API returns SUCCESS: doc.save() is called -> document is submitted
    ## - If API returns FAILURE: frappe.throw() is called -> document stays in Draft
    ## - If network error: frappe.throw() is called -> document stays in Draft
    
    ## Get EFRIS settings
    efris_settings = get_efris_settings()
    
    ## Prepare datetime
    datetime_combined = f"{doc.posting_date} {doc.posting_time}"
    
    ## Build invoice data
    invoice_data, total_tax_amount, item_count = build_invoice_data(efris_settings, doc, datetime_combined)
    
    ## Encrypt invoice data
    encrypted_result = encrypt_invoice_data(invoice_data)
    
    ## Build global info for T109
    global_info = build_global_info(
        efris_settings, 
        doc, 
        total_tax_amount, 
        invoice_data["goodsDetails"]
    )
    
    ## Build complete POST data
    data_to_post = build_post_data(encrypted_result, global_info)
    
    ## Submit to EFRIS
    response_data, headers, server_url = submit_to_efris(efris_settings, data_to_post)
    
    ## Handle response
    ## KEY: handle_efris_response() either:
    ##      - calls doc.save() on SUCCESS
    ##      - calls frappe.throw() on FAILURE (prevents submission)
    handle_efris_response(doc, response_data, headers, server_url, data_to_post)


def validate_efris_fields(doc, method):
    ## Validate EFRIS fields before submission for T109
    if not doc.custom_efris_invoice:
        return
    
    ## Validate required fields
    if not doc.tax_id:
        frappe.throw("Customer TIN is required for EFRIS invoices")
    
    if not doc.items:
        frappe.throw("Invoice must have at least one item")