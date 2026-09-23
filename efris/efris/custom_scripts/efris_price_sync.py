"""
EFRIS automatic Item price synchronization.

Purpose
-------
Detect committed changes to Item.custom_efris_price from an Item document event
and push the latest Item data to URA EFRIS using T130 MODIFY (operationType
"102"). Changed Items are stored in a durable queue and sent in batches by one
deduplicated background job.

The implementation reuses the EFRIS integration plumbing already used by the
existing Item T130 synchronization:
- EFRIS Settings
- dynamic JSON encryption/signing
- T130 request envelope
- HTTP POST
- response decryption
- Integration Request logging

An hourly recovery hook only retries rows left behind by a network failure or
worker interruption. Normal synchronization is event-driven.
"""

import base64
import gzip
import json
import uuid
from decimal import Decimal, InvalidOperation

import frappe
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from frappe.utils import add_to_date, now_datetime
from frappe.utils.data import strip_html

from efris.efris.background_tasks.encryption import encrypt_dynamic_json
from efris.efris.custom_scripts.upload_invoice import (
    EFRISIntegrationError,
    EFRIS_OPERATOR_NAME,
    clean_brn,
    get_efris_request_time,
    get_efris_settings,
    log_integration_request,
)


## Configuration

CHUNK_SIZE = 100
MAX_ATTEMPTS = 5
REJECT_RETRY_MIN = 30
REQUEST_RETRY_MIN = 15
MAX_ITEMS_PER_RUN = 1000

QUEUE_DOCTYPE = "EFRIS Price Sync Queue"
QUEUE_TABLE = "`tabEFRIS Price Sync Queue`"
PRICE_FIELD = "custom_efris_price"
PRICE_SYNC_JOB_ID = "efris_price_push"
PRICE_SYNC_LOCK = "efris_price_push"

T130_INTERFACE_CODE = "T130"
T130_SERVICE_NAME = "T130 Automatic Price Sync"

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



## T130 Item payload

def normalize_unit_price(value):
    cleaned = str(value or "").replace(",", "").strip()

    if not cleaned:
        raise EFRISIntegrationError("EFRIS Unit Price is required.")

    try:
        price = Decimal(cleaned)
    except InvalidOperation:
        raise EFRISIntegrationError(
            f"EFRIS Unit Price '{value}' is not a valid number."
        )

    if price < 0:
        raise EFRISIntegrationError("EFRIS Unit Price cannot be negative.")

    return format(price, "f")


def price_of(value):
    """
    Kept as a convenience helper for callers that need a numeric value.
    The T130 payload itself uses normalize_unit_price() so precision is not
    unnecessarily lost through float conversion.
    """
    return float(str(value or "0").replace(",", "").strip() or 0)


def get_efris_uom_code(uom):
    normalized = str(uom or "").strip()

    if not normalized:
        raise EFRISIntegrationError("EFRIS Unit of Measure is required.")

    return EFRIS_UOM_MAPPING.get(normalized.lower(), normalized)


def validate_goods_category(category_id):
    category_id = str(category_id or "").strip()

    if not category_id:
        raise EFRISIntegrationError("EFRIS Goods Category is required.")

    category = frappe.db.get_value(
        "EFRIS Goods Category",
        category_id,
        ["enabled", "is_leaf_node", "excisable"],
        as_dict=True,
    )

    if not category:
        raise EFRISIntegrationError(
            "EFRIS Goods Category was not found locally. "
            "Create or import the category first."
        )

    if not category.enabled:
        raise EFRISIntegrationError(
            "The selected EFRIS Goods Category is disabled by URA."
        )

    if not category.is_leaf_node:
        raise EFRISIntegrationError(
            "Select a leaf EFRIS Goods Category."
        )

    if category.excisable:
        raise EFRISIntegrationError(
            "The selected category is excisable. "
            "Excise duty Item synchronization is not configured yet."
        )

    return category_id


def build_goods_payload(item_name, operation_type=MODIFY_OPERATION):
    """
    Build ONE goods object for a T130 batch.

    Unlike the older single-Item builder, this deliberately returns a dict,
    not [dict], so many Items can be collected into one encrypted T130 list.
    """
    item = frappe.get_doc("Item", item_name)

    goods_code = str(item.custom_efris_product_code or "").strip()

    if not goods_code:
        raise EFRISIntegrationError(
            f"{item.name}: EFRIS Product Code is missing."
        )

    if len(goods_code) > 50:
        raise EFRISIntegrationError(
            f"{item.name}: EFRIS Product Code cannot exceed 50 characters."
        )

    goods_name = str(
        item.custom_goods_service_name or item.item_name or item.item_code or ""
    ).strip()

    if not goods_name:
        raise EFRISIntegrationError(
            f"{item.name}: EFRIS Goods Name is required."
        )

    category_id = validate_goods_category(
        item.custom_goods_category_id
    )

    efris_uom = str(item.custom_uom_code_efris or item.stock_uom or "").strip()

    return {
        "operationType": operation_type,
        "goodsName": goods_name,
        "goodsCode": goods_code,
        "measureUnit": get_efris_uom_code(efris_uom),
        "unitPrice": normalize_unit_price(item.custom_efris_price),
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
        "haveOtherUnit": "102",
        "goodsTypeCode": "101",
        "haveCustomsUnit": "102",
        "goodsOtherUnits": [],
    }


## EFRIS request/response

def build_t130_request(settings, encrypted_result, reference_no):
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
                "referenceNo": str(reference_no or "EFRIS-PRICE-SYNC")[:50],
                "operatorName": EFRIS_OPERATOR_NAME,
            },
        },
        "returnStateInfo": {
            "returnCode": "",
            "returnMessage": "",
        },
    }


def _decrypt_t130_with_key(encrypted_content, aes_key):
    aes_key_bytes = bytes.fromhex(aes_key)
    compressed_bytes = base64.b64decode(encrypted_content)

    try:
        encrypted_bytes = gzip.decompress(compressed_bytes)
    except Exception:
        encrypted_bytes = None
        for trim_bytes in range(1, 5):
            try:
                encrypted_bytes = gzip.decompress(
                    compressed_bytes[:-trim_bytes]
                )
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


def decrypt_t130_response(response_data, aes_key, settings_aes_key=""):
    encrypted_content = (response_data.get("data") or {}).get("content")

    if not encrypted_content:
        return []

    keys = [
        key
        for key in (settings_aes_key, aes_key)
        if key
    ]

    last_error = None

    for key in dict.fromkeys(keys):
        try:
            return _decrypt_t130_with_key(
                encrypted_content,
                key,
            )
        except Exception as exc:
            last_error = exc

    raise EFRISIntegrationError(
        "Failed to decrypt T130 response with the configured and request AES "
        f"keys: {last_error}"
    )


def safe_log_integration_request(*args, **kwargs):
    try:
        log_integration_request(*args, **kwargs)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "EFRIS T130 Integration Request Logging Failed",
        )


def validate_request_level_response(response_data):
    state = response_data.get("returnStateInfo") or {}

    code = str(state.get("returnCode") or "").strip()
    message = str(state.get("returnMessage") or "").strip()

    if code not in {"", "00"}:
        raise EFRISIntegrationError(
            f"T130 error {code}: {message or 'Unknown EFRIS error'}"
        )

    if message and message.upper() != "SUCCESS":
        raise EFRISIntegrationError(
            f"T130 failed: {message}"
        )


def _response_items(decrypted_data):
    """
    Normalize common T130 decrypted-response shapes into a list.

    If URA changes/wraps the response differently in your environment,
    this is the one helper that should need adjustment.
    """
    if not decrypted_data:
        return []

    if isinstance(decrypted_data, list):
        return decrypted_data

    if not isinstance(decrypted_data, dict):
        return []

    ## Sometimes the actual array is nested under a response/data/result key.
    for key in (
        "data",
        "result",
        "results",
        "goods",
        "goodsList",
        "goodsResult",
        "goodsResults",
    ):
        value = decrypted_data.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for child_key in (
                "result",
                "results",
                "goods",
                "goodsList",
                "goodsResult",
                "goodsResults",
            ):
                child = value.get(child_key)
                if isinstance(child, list):
                    return child

    ## A single response object.
    if (
        "goodsCode" in decrypted_data
        or "returnCode" in decrypted_data
        or "returnMessage" in decrypted_data
    ):
        return [decrypted_data]

    return []


def parse_batch_t130_response(response_data, decrypted_data, sent_by_code):
    """
    Return {item_name: rejection_reason}.

    Request-level failures raise an exception. Item-level failures are returned
    individually so successful Items do not get retried.
    """
    validate_request_level_response(response_data)

    results = _response_items(decrypted_data)

    ## Some successful EFRIS calls may not return a per-item body.
    ## In that case the successful request-level response is treated as success.
    if not results:
        return {}

    rejected = {}

    for result in results:
        if not isinstance(result, dict):
            continue

        goods_code = str(
            result.get("goodsCode")
            or result.get("goods_code")
            or ""
        ).strip()

        code = str(result.get("returnCode") or "").strip()
        message = str(result.get("returnMessage") or "").strip()

        if code not in {"", "00"}:
            item_name = sent_by_code.get(goods_code, goods_code)

            ## If EFRIS omits goodsCode for a rejected single result,
            ## associate it with the only Item in this request.
            if not item_name and len(sent_by_code) == 1:
                item_name = next(iter(sent_by_code.values()))

            if not item_name:
                item_name = "UNKNOWN"

            rejected[item_name] = (
                message or f"EFRIS rejected the Item with code {code}"
            )

    return rejected


def efris_t130_call(payload, reference_docname="EFRIS-PRICE-SYNC"):
    """
    Send one already-built T130 payload list.

    Returns a dict of rejected Item names -> rejection reason.
    Raises EFRISIntegrationError when the entire HTTP/EFRIS request fails.
    """
    if not payload:
        return {}

    settings = get_efris_settings()

    encrypted_result = encrypt_dynamic_json(payload)

    if not encrypted_result.get("success"):
        raise EFRISIntegrationError(
            "Failed to encrypt T130 payload: "
            f"{encrypted_result.get('error')}"
        )

    aes_key = encrypted_result.get("aes_key", "")

    request_data = build_t130_request(
        settings,
        encrypted_result,
        reference_docname,
    )

    headers = {"Content-Type": "application/json"}
    response_data = {}

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
        safe_log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            error=str(exc),
            aes_key=aes_key,
            reference_docname=reference_docname,
            reference_doctype="Item",
            service=T130_SERVICE_NAME,
        )

        raise EFRISIntegrationError(
            f"T130 request failed: {exc}"
        )

    sent_by_code = {
        str(row.get("goodsCode") or ""): row.get("_item_name")
        for row in payload
        if row.get("goodsCode")
    }

    ## Internal helper metadata must never be sent to EFRIS. This normally does
    ## nothing because efris_t130_call receives the clean payload, but keeping
    ## sent_by_code here documents the expected mapping.
    sent_by_code = {
        code: name
        for code, name in sent_by_code.items()
        if name
    }

    try:
        validate_request_level_response(response_data)
        decrypted_data = decrypt_t130_response(
            response_data,
            aes_key,
            settings_aes_key=(
                frappe.get_cached_value(
                    "EFRIS Settings",
                    "EFRIS Settings",
                    "aes_key",
                )
                or ""
            ).strip(),
        )

        ## The public payload doesn't carry _item_name, so map goodsCode to
        ## Item name using the database where necessary.
        codes = [
            str(row.get("goodsCode") or "")
            for row in payload
            if row.get("goodsCode")
        ]

        if codes:
            rows = frappe.db.sql(
                """
                SELECT name, custom_efris_product_code
                FROM `tabItem`
                WHERE custom_efris_product_code IN %s
                """,
                (tuple(codes),),
            )
            sent_by_code = {
                str(goods_code): item_name
                for item_name, goods_code in rows
            }

        rejected = parse_batch_t130_response(
            response_data,
            decrypted_data,
            sent_by_code,
        )

    except Exception as exc:
        safe_log_integration_request(
            "Failed",
            settings.server_url,
            headers,
            request_data,
            response_data,
            error=str(exc),
            aes_key=aes_key,
            reference_docname=reference_docname,
            reference_doctype="Item",
            service=T130_SERVICE_NAME,
        )
        raise

    safe_log_integration_request(
        "Completed",
        settings.server_url,
        headers,
        request_data,
        response_data,
        aes_key=aes_key,
        reference_docname=reference_docname,
        reference_doctype="Item",
        service=T130_SERVICE_NAME,
    )

    return rejected


## Batch push

def push_names(names):
    """
    Push latest prices for the supplied Item names.

    Returns:
        {item_name: rejection_reason}

    Items without an EFRIS Product Code are skipped because T130 MODIFY cannot
    identify them remotely.
    """
    names = sorted(set(names or []))

    if not names:
        return {}

    rows = frappe.db.sql(
        """
        SELECT name, custom_efris_product_code
        FROM `tabItem`
        WHERE name IN %s
          AND IFNULL(custom_efris_product_code, '') != ''
        """,
        (tuple(names),),
    )

    eligible_names = [row[0] for row in rows]
    rejected = {}

    for start in range(0, len(eligible_names), CHUNK_SIZE):
        chunk = eligible_names[start:start + CHUNK_SIZE]

        payload = []
        locally_rejected = {}

        for item_name in chunk:
            try:
                payload.append(
                    build_goods_payload(
                        item_name,
                        operation_type=MODIFY_OPERATION,
                    )
                )
            except EFRISIntegrationError as exc:
                ## A bad Item should not prevent the remaining valid Items
                ## from being sent in the same scheduler run.
                locally_rejected[item_name] = str(exc)

        rejected.update(locally_rejected)

        if not payload:
            continue

        chunk_rejections = efris_t130_call(
            payload,
            reference_docname=chunk[0],
        )

        rejected.update(chunk_rejections)

    return rejected


## Event-driven queue


def _normalized_price(value):
    return str(value or "").replace(",", "").strip()


def queue_item_price_change(doc, method=None):
    """Persist a queue row only when an existing Item's EFRIS price changes."""
    previous = doc.get_doc_before_save()

    if not previous:
        return

    if _normalized_price(previous.get(PRICE_FIELD)) == _normalized_price(doc.get(PRICE_FIELD)):
        return

    if not str(doc.get("custom_efris_product_code") or "").strip():
        return

    mark_item_for_price_sync(doc.name)
    _enqueue_after_commit_once()


def mark_item_for_price_sync(item_name):
    """Create or reset the durable queue marker inside the Item transaction."""
    values = {
        "status": "Pending",
        "requested_at": now_datetime(),
        "attempts": 0,
        "last_attempt_at": None,
        "retry_after": None,
        "last_error": None,
    }

    if frappe.db.exists(QUEUE_DOCTYPE, item_name):
        frappe.db.set_value(QUEUE_DOCTYPE, item_name, values, update_modified=True)
        return

    try:
        frappe.get_doc(
            {
                "doctype": QUEUE_DOCTYPE,
                "item_code": item_name,
                **values,
            }
        ).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # Another request may have queued the same Item concurrently.
        frappe.db.set_value(QUEUE_DOCTYPE, item_name, values, update_modified=True)


def _enqueue_after_commit_once():
    flag = "efris_price_sync_enqueue_registered"
    if getattr(frappe.flags, flag, False):
        return

    setattr(frappe.flags, flag, True)
    frappe.db.after_commit.add(_safe_enqueue_pending_price_sync)


def _safe_enqueue_pending_price_sync():
    try:
        enqueue_pending_price_sync()
    except Exception:
        # The queue row is already committed. The hourly recovery hook will
        # enqueue it if Redis/RQ is temporarily unavailable.
        frappe.log_error(
            frappe.get_traceback(),
            "EFRIS Price Sync Enqueue Failed",
        )
    finally:
        setattr(frappe.flags, "efris_price_sync_enqueue_registered", False)


def _has_due_queue_rows():
    return bool(
        frappe.db.sql(
            f"""
            SELECT name
            FROM {QUEUE_TABLE}
            WHERE status IN ('Pending', 'Retrying')
              AND (retry_after IS NULL OR retry_after <= %s)
            LIMIT 1
            """,
            (now_datetime(),),
        )
    )


def enqueue_pending_price_sync():
    """Enqueue one batch worker only when at least one queue row is due."""
    if not _has_due_queue_rows():
        return False

    frappe.enqueue(
        f"{__name__}.run_pending_price_sync",
        queue="long",
        timeout=1500,
        job_id=PRICE_SYNC_JOB_ID,
        deduplicate=True,
    )
    return True


def retry_pending_price_sync():
    """Hourly recovery for request failures or an interrupted worker."""
    return enqueue_pending_price_sync()


def push_price_changes():
    """Backward-compatible entry point for an old scheduled job record."""
    return enqueue_pending_price_sync()


def _get_due_queue_rows(limit):
    return frappe.db.sql(
        f"""
        SELECT name, item_code, modified, attempts
        FROM {QUEUE_TABLE}
        WHERE status IN ('Pending', 'Retrying')
          AND (retry_after IS NULL OR retry_after <= %s)
        ORDER BY requested_at ASC, creation ASC
        LIMIT %s
        """,
        (now_datetime(), int(limit)),
        as_dict=True,
    )


def _delete_queue_row_if_unchanged(row):
    frappe.db.sql(
        f"DELETE FROM {QUEUE_TABLE} WHERE name = %s AND modified = %s",
        (row.name, row.modified),
    )


def _mark_queue_retry_if_unchanged(
    row,
    error,
    retry_minutes,
    stop_after_max_attempts,
):
    attempts = int(row.attempts or 0) + 1
    is_failed = stop_after_max_attempts and attempts >= MAX_ATTEMPTS
    attempted_at = now_datetime()
    retry_after = None if is_failed else add_to_date(attempted_at, minutes=retry_minutes)

    frappe.db.sql(
        f"""
        UPDATE {QUEUE_TABLE}
        SET status = %s,
            attempts = %s,
            last_attempt_at = %s,
            retry_after = %s,
            last_error = %s,
            modified = %s,
            modified_by = %s
        WHERE name = %s
          AND modified = %s
        """,
        (
            "Failed" if is_failed else "Retrying",
            attempts,
            attempted_at,
            retry_after,
            str(error or "Unknown EFRIS error")[:500],
            attempted_at,
            frappe.session.user,
            row.name,
            row.modified,
        ),
    )


def _process_queue_batch(rows):
    names = [row.item_code for row in rows]

    try:
        rejected = push_names(names)
    except Exception as exc:
        frappe.log_error(
            frappe.get_traceback(),
            "EFRIS T130 Price Sync Failed",
        )

        for row in rows:
            _mark_queue_retry_if_unchanged(
                row,
                exc,
                REQUEST_RETRY_MIN,
                stop_after_max_attempts=False,
            )

        frappe.db.commit()
        return {"completed": 0, "retrying": len(rows), "failed": 0}

    completed = 0
    retrying = 0
    failed = 0

    for row in rows:
        if row.item_code not in rejected:
            _delete_queue_row_if_unchanged(row)
            completed += 1
            continue

        next_attempt = int(row.attempts or 0) + 1
        _mark_queue_retry_if_unchanged(
            row,
            rejected[row.item_code],
            REJECT_RETRY_MIN,
            stop_after_max_attempts=True,
        )

        if next_attempt >= MAX_ATTEMPTS:
            failed += 1
        else:
            retrying += 1

    frappe.db.commit()
    return {"completed": completed, "retrying": retrying, "failed": failed}


def run_pending_price_sync():
    """Drain due queue rows under a site-specific MariaDB advisory lock."""
    site = getattr(frappe.local, "site", "site")
    lock_name = f"{PRICE_SYNC_LOCK}:{site}"[:64]
    result = frappe.db.sql("SELECT GET_LOCK(%s, 0)", (lock_name,))

    if not result or not result[0][0]:
        return {"skipped": True, "reason": "Price-sync worker is already running"}

    totals = {"processed": 0, "completed": 0, "retrying": 0, "failed": 0}

    try:
        while totals["processed"] < MAX_ITEMS_PER_RUN:
            limit = min(CHUNK_SIZE, MAX_ITEMS_PER_RUN - totals["processed"])
            rows = _get_due_queue_rows(limit)
            if not rows:
                break

            result = _process_queue_batch(rows)
            totals["processed"] += len(rows)
            totals["completed"] += result["completed"]
            totals["retrying"] += result["retrying"]
            totals["failed"] += result["failed"]

        return totals
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))


def run_locked():
    """Backward-compatible alias for already queued jobs from the old code."""
    return run_pending_price_sync()


def run():
    """Backward-compatible alias for callers of the old synchronization engine."""
    return run_pending_price_sync()


## T109 pre-invoice protection


def sync_prices_before_invoice(doc):
    """Synchronize queued invoice Items before T109 when explicitly called."""
    invoice_codes = sorted(
        {
            row.item_code
            for row in (doc.items or [])
            if row.item_code
        }
    )

    if not invoice_codes:
        return

    rows = frappe.db.sql(
        f"""
        SELECT name, item_code, modified, attempts
        FROM {QUEUE_TABLE}
        WHERE item_code IN %s
        """,
        (tuple(invoice_codes),),
        as_dict=True,
    )

    if not rows:
        return

    names = [row.item_code for row in rows]

    try:
        rejected = push_names(names)

        for row in rows:
            if row.item_code not in rejected:
                _delete_queue_row_if_unchanged(row)

        frappe.db.commit()

        if rejected:
            details = "; ".join(
                f"{item}: {reason}"
                for item, reason in rejected.items()
            )
            raise EFRISIntegrationError(
                "EFRIS price synchronization rejected "
                f"invoice Item(s): {details}"
            )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "EFRIS Pre-Invoice Price Sync Failed",
        )
        raise
