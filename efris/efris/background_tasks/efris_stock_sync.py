import base64
import gzip
import json
import uuid
from datetime import datetime

import frappe
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from frappe.utils import flt

from efris.efris.background_tasks.encryption import encrypt_dynamic_json
from efris.efris.custom_scripts.upload_invoice import (
	EAT_TIMEZONE,
	EFRIS_OPERATOR_NAME,
	clean_brn,
	decrypt_efris_response_content,
	get_efris_request_time,
	get_efris_settings,
	log_integration_request,
)

T127_VOUCHER_TYPE = "T127"
T127_SERVICE_NAME = "T127 Stock Query"
T127_PAGE_SIZE = 90


def build_t127_payload(page_no):
	return {
		"pageNo": str(page_no),
		"pageSize": str(T127_PAGE_SIZE),
	}


def build_t127_request(settings, encrypted_result):
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
			"interfaceCode": "T127",
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
				"referenceNo": uuid.uuid4().hex[:14],
				"operatorName": EFRIS_OPERATOR_NAME,
			},
		},
		"returnStateInfo": {
			"returnCode": "",
			"returnMessage": "",
		},
	}


def send_t127_request(server_url, request_data, aes_key=""):
	headers = {"Content-Type": "application/json"}

	try:
		response = requests.post(server_url, json=request_data, headers=headers, timeout=60)
		response.raise_for_status()
		return response.json(), headers
	except requests.exceptions.Timeout:
		error_message = "EFRIS stock request timed out. Please try again."
		log_integration_request(
			"Failed",
			server_url,
			headers,
			request_data,
			{},
			error_message,
			aes_key=aes_key,
			service=T127_SERVICE_NAME,
		)
		frappe.throw(error_message)
	except requests.exceptions.RequestException as exc:
		error_message = f"EFRIS stock request failed: {str(exc)}"
		log_integration_request(
			"Failed",
			server_url,
			headers,
			request_data,
			{},
			error_message,
			aes_key=aes_key,
			service=T127_SERVICE_NAME,
		)
		frappe.throw(error_message)


def get_t127_page_count(decrypted_data):
	page_info = decrypted_data.get("page") or {}
	raw_page_count = page_info.get("pageCount")

	try:
		page_count = int(raw_page_count)
	except (TypeError, ValueError):
		frappe.throw("T127 response did not include a valid page.pageCount value.")

	if page_count < 1:
		frappe.throw("T127 response returned an invalid page count.")

	return page_count


def fetch_t127_stock_page(settings, page_no):
	payload = build_t127_payload(page_no)
	encrypted_result = encrypt_dynamic_json(payload)

	if not encrypted_result.get("success"):
		frappe.throw(f"Failed to encrypt T127 payload: {encrypted_result.get('error')}")

	aes_key_used = encrypted_result.get("aes_key", "")
	request_data = build_t127_request(settings, encrypted_result)
	response_data, headers = send_t127_request(settings.server_url, request_data, aes_key=aes_key_used)

	try:
		return_state = response_data.get("returnStateInfo") or {}
		return_code = str(return_state.get("returnCode") or "").strip()
		return_message = str(return_state.get("returnMessage") or "").strip()
		if return_code not in {"", "00"}:
			frappe.throw(
				f"T127 page {page_no} failed with code {return_code}: "
				f"{return_message or 'Unknown EFRIS error'}"
			)
		if return_message.upper() != "SUCCESS":
			frappe.throw(
				f"T127 page {page_no} failed: {return_message or 'Unknown EFRIS error'}"
			)

		encrypted_content = response_data.get("data", {}).get("content")
		if not encrypted_content:
			frappe.throw(f"T127 page {page_no} did not return encrypted content.")

		settings_aes_key = (getattr(settings, "aes_key", "") or "").strip()
		decryption_key = aes_key_used or settings_aes_key
		if not decryption_key:
			frappe.throw("No AES key available to decrypt T127 response.")

		decrypted_data = decrypt_t127_response_content(
			encrypted_content,
			primary_key=decryption_key,
			fallback_key=(
				settings_aes_key
				if settings_aes_key and settings_aes_key != decryption_key
				else ""
			),
		)
		if not isinstance(decrypted_data, dict):
			frappe.throw(f"T127 page {page_no} returned an invalid decrypted response.")
		page_records = decrypted_data.get("records") or []
		if not isinstance(page_records, list):
			frappe.throw(f"T127 page {page_no} returned an invalid records value.")
		if any(not isinstance(record, dict) for record in page_records):
			frappe.throw(f"T127 page {page_no} returned an invalid stock record.")
		page_count = get_t127_page_count(decrypted_data)
		if page_no < page_count and not page_records:
			frappe.throw(
				f"T127 page {page_no} was empty even though EFRIS reported {page_count} pages."
			)
	except Exception as exc:
		log_integration_request(
			"Failed",
			settings.server_url,
			headers,
			request_data,
			response_data,
			str(exc),
			aes_key=aes_key_used,
			service=T127_SERVICE_NAME,
		)
		raise

	log_integration_request(
		"Completed",
		settings.server_url,
		headers,
		request_data,
		response_data,
		aes_key=aes_key_used,
		service=T127_SERVICE_NAME,
	)

	return decrypted_data, response_data


def fetch_t127_stock_data():
	settings = get_efris_settings()
	page_no = 1
	page_count = 1
	all_records = []
	page_responses = []
	seen_product_codes = set()

	while page_no <= page_count:
		decrypted_data, response_data = fetch_t127_stock_page(settings, page_no)
		reported_page_count = get_t127_page_count(decrypted_data)
		if page_no == 1:
			page_count = reported_page_count
		elif reported_page_count != page_count:
			frappe.throw(
				f"T127 page count changed from {page_count} to {reported_page_count} "
				f"while fetching page {page_no}. Run the sync again."
			)

		page_records = decrypted_data.get("records") or []
		for record in page_records:
			product_code = str(record.get("goodsCode") or "").strip()
			if product_code and product_code in seen_product_codes:
				frappe.throw(
					f"T127 returned duplicate goodsCode {product_code} while fetching page {page_no}. "
					"No ledger entries were written."
				)
			if product_code:
				seen_product_codes.add(product_code)

		all_records.extend(page_records)
		page_responses.append(response_data)
		page_no += 1

	return {
		"records": all_records,
		"page": {
			"pageCount": page_count,
			"pagesFetched": len(page_responses),
			"pageSize": T127_PAGE_SIZE,
		},
	}, page_responses


def _decrypt_t127_with_key(encrypted_content, aes_key_hex):
	aes_key_bytes = bytes.fromhex(aes_key_hex)
	compressed_bytes = base64.b64decode(encrypted_content)

	try:
		encrypted_bytes = gzip.decompress(compressed_bytes)
	except Exception:
		encrypted_bytes = None
		for trim_bytes in range(1, 5):
			try:
				encrypted_bytes = gzip.decompress(compressed_bytes[:-trim_bytes])
				break
			except Exception:
				continue
		if encrypted_bytes is None:
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


def decrypt_t127_response_content(encrypted_content, primary_key, fallback_key=""):
	keys_to_try = [primary_key]
	if fallback_key:
		keys_to_try.append(fallback_key)

	last_error = None
	for key in keys_to_try:
		try:
			return _decrypt_t127_with_key(encrypted_content, key)
		except Exception as exc:
			last_error = exc

	frappe.throw(f"Failed to decrypt T127 response: {last_error}")


def get_item_code_from_efris_mapping(product_code="", goods_name=""):
	if product_code:
		item_code = frappe.db.get_value("Item", {"custom_efris_product_code": product_code}, "name")
		if item_code:
			return item_code

	if goods_name:
		item_code = frappe.db.get_value("Item", {"custom_goods_service_name": goods_name}, "name")
		if item_code:
			return item_code

	return ""


def upsert_opening_stock_entry(record, posting_datetime):
	product_code = str(record.get("goodsCode") or "").strip()
	goods_name = str(record.get("goodsName") or "").strip()
	if not product_code:
		return False

	stock_qty = flt(record.get("stock"))
	item_code = get_item_code_from_efris_mapping(product_code=product_code, goods_name=goods_name)
	item_name = (
		goods_name
		or (frappe.db.get_value("Item", item_code, "item_name") if item_code else "")
		or product_code
	)
	uom = (
		(record.get("measureUnit") or "").strip()
		or (frappe.db.get_value("Item", item_code, "stock_uom") if item_code else "")
		or ""
	)

	existing_name = frappe.db.get_value(
		"EFRIS Stock Ledger Entry",
		{
			"is_opening_entry": 1,
			"voucher_type": T127_VOUCHER_TYPE,
			"posting_date": posting_datetime.date(),
			"efris_product_code": product_code,
		},
		"name",
	)

	values = {
		"posting_date": posting_datetime.date(),
		"posting_time": posting_datetime.time().strftime("%H:%M:%S"),
		"item_code": item_code,
		"item_name": item_name,
		"uom": uom,
		"efris_goods_name": goods_name,
		"efris_product_code": product_code,
		"qty_in": stock_qty,
		"qty_out": 0,
		"balance": stock_qty,
		"voucher_type": T127_VOUCHER_TYPE,
		"voucher_no": posting_datetime.strftime("%Y-%m-%d"),
		"is_opening_entry": 1,
	}

	if existing_name:
		ledger_doc = frappe.get_doc("EFRIS Stock Ledger Entry", existing_name)
		ledger_doc.update(values)
		ledger_doc.save(ignore_permissions=True)
	else:
		values["doctype"] = "EFRIS Stock Ledger Entry"
		frappe.get_doc(values).insert(ignore_permissions=True)

	return True


def write_t127_stock_to_ledger(decrypted_data):
	records = decrypted_data.get("records") or []
	page_info = decrypted_data.get("page") or {}
	posting_datetime = datetime.now(EAT_TIMEZONE).replace(tzinfo=None)
	created_or_updated = 0

	for record in records:
		if upsert_opening_stock_entry(record, posting_datetime):
			created_or_updated += 1

	frappe.db.commit()
	return {
		"success": True,
		"pages_expected": int(page_info.get("pageCount") or 0),
		"pages_fetched": int(page_info.get("pagesFetched") or 0),
		"page_size": int(page_info.get("pageSize") or 0),
		"records_received": len(records),
		"ledger_rows_written": created_or_updated,
		"posting_date": str(posting_datetime.date()),
	}


@frappe.whitelist()
def sync_t127_opening_stock():
	decrypted_data, _response_data = fetch_t127_stock_data()
	return write_t127_stock_to_ledger(decrypted_data)


def sync_daily_efris_stock():
	return sync_t127_opening_stock()
