import base64
import gzip
import json
import uuid

import frappe
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from efris.efris.background_tasks.encryption import encrypt_dynamic_json
from efris.efris.custom_scripts.upload_invoice import (
	EAT_TIMEZONE,
	EFRIS_OPERATOR_NAME,
	clean_brn,
	get_efris_request_time,
	get_efris_settings,
	log_integration_request,
)

T106_SERVICE_NAME = "T106 Invoice Receipt Query"
T106_PAGE_SIZE = 90
T106_INVOICE_KINDS = {"Invoice": "1", "Receipt": "2"}


def build_t106_payload(from_date, to_date, page_no, invoice_kind):
	return {
		"invoiceType": "1",
		"invoiceKind": invoice_kind,
		"isInvalid": "0",
		"startDate": str(from_date),
		"endDate": str(to_date),
		"pageNo": str(page_no),
		"pageSize": str(T106_PAGE_SIZE),
		"queryType": "1",
	}


def build_t106_request(settings, encrypted_result):
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
			"interfaceCode": "T106",
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


def _decode_json_bytes(value):
	try:
		return json.loads(value.decode("utf-8"))
	except (UnicodeDecodeError, json.JSONDecodeError):
		return None


def _decrypt_with_key(encrypted_bytes, aes_key_hex):
	key = bytes.fromhex(aes_key_hex)
	if len(encrypted_bytes) % AES.block_size:
		return None

	decrypted = AES.new(key, AES.MODE_ECB).decrypt(encrypted_bytes)
	try:
		decrypted = unpad(decrypted, AES.block_size)
	except ValueError:
		pass

	decoded = _decode_json_bytes(decrypted)
	if decoded is not None:
		return decoded

	try:
		return _decode_json_bytes(gzip.decompress(decrypted))
	except (OSError, EOFError):
		return None


def decrypt_t106_content(content, response_description, keys):
	try:
		decoded_content = base64.b64decode(content, validate=True)
	except Exception as exc:
		frappe.throw(f"T106 response content is not valid Base64: {exc}")

	candidates = [decoded_content]
	if str((response_description or {}).get("zipCode") or "0") == "1":
		try:
			candidates.insert(0, gzip.decompress(decoded_content))
		except (OSError, EOFError):
			pass

	for candidate in candidates:
		plaintext = _decode_json_bytes(candidate)
		if plaintext is not None:
			return plaintext

		for aes_key in keys:
			if not aes_key:
				continue
			try:
				plaintext = _decrypt_with_key(candidate, aes_key)
			except (ValueError, TypeError):
				plaintext = None
			if plaintext is not None:
				return plaintext

	frappe.throw(
		"Failed to decrypt the T106 response. Refresh the AES key and confirm that "
		"the certificate configured in EFRIS Settings belongs to this device."
	)


def fetch_t106_page(settings, from_date, to_date, page_no, invoice_kind):
	payload = build_t106_payload(from_date, to_date, page_no, invoice_kind)
	encrypted_result = encrypt_dynamic_json(payload)
	if not encrypted_result.get("success"):
		frappe.throw(f"Failed to encrypt T106 payload: {encrypted_result.get('error')}")

	request_data = build_t106_request(settings, encrypted_result)
	headers = {"Content-Type": "application/json"}
	response_data = {}
	aes_key_used = encrypted_result.get("aes_key", "")

	try:
		response = requests.post(
			settings.server_url,
			json=request_data,
			headers=headers,
			timeout=60,
		)
		response.raise_for_status()
		response_data = response.json()

		return_state = response_data.get("returnStateInfo") or {}
		return_code = str(return_state.get("returnCode") or "").strip()
		return_message = str(return_state.get("returnMessage") or "").strip()
		if return_code not in {"", "00"}:
			frappe.throw(
				f"T106 page {page_no} failed with code {return_code}: "
				f"{return_message or 'Unknown EFRIS error'}"
			)
		if return_message.upper() != "SUCCESS":
			frappe.throw(
				f"T106 page {page_no} failed: {return_message or 'Unknown EFRIS error'}"
			)

		content = response_data.get("data", {}).get("content")
		if not content:
			frappe.throw(f"T106 page {page_no} did not return content.")

		settings_key = str(getattr(settings, "aes_key", "") or "").strip()
		keys = list(dict.fromkeys([aes_key_used, settings_key]))
		decrypted = decrypt_t106_content(
			content,
			response_data.get("data", {}).get("dataDescription") or {},
			keys,
		)
		if not isinstance(decrypted, dict):
			frappe.throw(f"T106 page {page_no} returned an invalid response object.")

		page = decrypted.get("page") or {}
		records = decrypted.get("records") or []
		if not isinstance(records, list):
			frappe.throw(f"T106 page {page_no} returned invalid records.")

		try:
			page_count = int(page.get("pageCount") or 1)
		except (TypeError, ValueError):
			frappe.throw(f"T106 page {page_no} returned an invalid page count.")
		if page_count < 1:
			frappe.throw(f"T106 page {page_no} returned an invalid page count.")

		log_integration_request(
			"Completed",
			settings.server_url,
			headers,
			request_data,
			response_data,
			aes_key=aes_key_used,
			service=T106_SERVICE_NAME,
		)
		return records, page_count
	except Exception as exc:
		log_integration_request(
			"Failed",
			settings.server_url,
			headers,
			request_data,
			response_data,
			str(exc),
			aes_key=aes_key_used,
			service=T106_SERVICE_NAME,
		)
		raise


def fetch_t106_invoices(from_date, to_date, invoice_kind="Both"):
	settings = get_efris_settings()
	kinds = (
		list(T106_INVOICE_KINDS.values())
		if invoice_kind == "Both"
		else [T106_INVOICE_KINDS[invoice_kind]]
	)
	all_records = []
	seen_records = set()

	for kind in kinds:
		page_no = 1
		page_count = 1
		while page_no <= page_count:
			records, reported_page_count = fetch_t106_page(
				settings,
				from_date,
				to_date,
				page_no,
				kind,
			)
			if page_no == 1:
				page_count = reported_page_count
			elif reported_page_count != page_count:
				frappe.throw(
					f"T106 page count changed while fetching invoice kind {kind}. "
					"Please refresh the report."
				)

			for raw_record in records:
				if not isinstance(raw_record, dict):
					frappe.throw(f"T106 page {page_no} returned an invalid invoice record.")
				record = {
					str(key).strip(): value
					for key, value in (raw_record or {}).items()
				}
				identity = (
					str(record.get("id") or ""),
					str(record.get("invoiceNo") or ""),
					str(record.get("invoiceKind") or kind),
				)
				if identity in seen_records:
					continue
				seen_records.add(identity)
				record["invoiceKind"] = str(record.get("invoiceKind") or kind)
				all_records.append(record)

			page_no += 1

	return all_records
