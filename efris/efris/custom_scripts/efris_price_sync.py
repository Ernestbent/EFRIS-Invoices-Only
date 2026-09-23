"""
EFRIS automatic Item price synchronization.

Purpose
-------
Detect changes to Item.custom_efris_price from Frappe's Version log and push
the latest Item data to URA EFRIS using T130 MODIFY (operationType "102").

The implementation reuses the EFRIS integration plumbing already used by the
existing Item T130 synchronization:
- EFRIS Settings
- dynamic JSON encryption/signing
- T130 request envelope
- HTTP POST
- response decryption
- Integration Request logging

Suggested scheduler:
    "cron": {
        "* * * * *": [
            "efris.efris.background_tasks.price_sync.push_price_changes"
        ]
    }

Adjust the module path above to wherever you save this file.
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
from frappe.utils import add_to_date, get_datetime, now_datetime
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

KEY = "efris_price_sync"
FIELDS = ["custom_efris_price"]

CHUNK_SIZE = 100
QUIET_MIN = 2
MAX_WAIT_MIN = 15
MAX_ATTEMPTS = 5
REJECT_RETRY_MIN = 30

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


## State

def default_state():
    return {
        "upto": None,
        "rejected": {},
        "req_fails": 0,
        "wait_until": None,
        "reject_retry": None,
    }


def load_state():
    raw = frappe.defaults.get_global_default(KEY)

    if not raw:
        return default_state()

    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        frappe.log_error(
            message=f"Invalid EFRIS price-sync state: {raw}",
            title="EFRIS Price Sync State Error",
        )
        return default_state()

    result = default_state()
    result.update(state or {})
    return result


def save_state(state):
    frappe.defaults.set_global_default(KEY, json.dumps(state))
    frappe.db.commit()


## Version/change detection

def changed_rows(after):
    """
    Return Item Version rows created after `after` where one of FIELDS appears
    in the Version data.

    Version is used because Item already has track_changes enabled.
    """
    if not after:
        return []

    like_sql = " OR ".join(["data LIKE %s"] * len(FIELDS))
    args = [f'%"{field}"%' for field in FIELDS]

    return frappe.db.sql(
        f"""
        SELECT docname, modified
        FROM `tabVersion`
        WHERE ref_doctype = 'Item'
          AND modified > %s
          AND ({like_sql})
        ORDER BY modified ASC
        """,
        tuple([str(after)] + args),
    )


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


## Scheduler entry point

def push_price_changes():
    """
    Cheap scheduler function intended to run every minute.

    It only enqueues the long-running worker when there is actual work or a
    rejected Item is due for retry.
    """
    now = now_datetime()
    state = load_state()

    if state["upto"]:
        if (
            state["wait_until"]
            and now < get_datetime(state["wait_until"])
        ):
            return

        retry_due = any(
            value[1] < MAX_ATTEMPTS
            for value in state["rejected"].values()
        )

        if not retry_due and not changed_rows(state["upto"]):
            ## Prevent the Version query window from growing forever while the
            ## system is idle.
            if get_datetime(state["upto"]) < add_to_date(
                now,
                minutes=-30,
            ):
                state["upto"] = str(
                    add_to_date(now, minutes=-10)
                )
                save_state(state)

            return

    frappe.enqueue(
        f"{__name__}.run_locked",
        queue="long",
        timeout=1500,
        job_id="efris_price_push",
        deduplicate=True,
    )


def run_locked():
    """
    MySQL/MariaDB advisory lock provides an additional guard even if queue
    deduplication is bypassed or multiple workers attempt the same job.
    """
    result = frappe.db.sql(
        "SELECT GET_LOCK('efris_price_push', 0)"
    )

    if not result or not result[0][0]:
        return

    try:
        run()
    finally:
        frappe.db.sql(
            "SELECT RELEASE_LOCK('efris_price_push')"
        )


## Synchronization engine

def run():
    now = now_datetime()
    state = load_state()

    ## First activation starts tracking from now. It intentionally does not
    ## upload historical Item changes.
    if not state["upto"]:
        state["upto"] = str(now)
        save_state(state)
        return

    if (
        state["wait_until"]
        and now < get_datetime(state["wait_until"])
    ):
        return

    rows = changed_rows(state["upto"])
    cutoff = add_to_date(now, minutes=-QUIET_MIN)

    if rows:
        newest = max(row[1] for row in rows)
        oldest = min(row[1] for row in rows)

        ## An import/bulk edit appears to still be producing Item Versions.
        if (
            newest > cutoff
            and oldest > add_to_date(
                now,
                minutes=-MAX_WAIT_MIN,
            )
        ):
            return

    names = {
        row[0]
        for row in rows
        if row[1] <= cutoff
    }

    rejected_state = state["rejected"]

    if (
        rejected_state
        and now >= get_datetime(
            state["reject_retry"] or now
        )
    ):
        names |= {
            item_name
            for item_name, value in rejected_state.items()
            if value[1] < MAX_ATTEMPTS
        }

    if not names:
        return

    try:
        rejected_now = push_names(sorted(names))

    except Exception:
        ## Keep the same synchronization window. The scheduler will retry it.
        frappe.log_error(
            frappe.get_traceback(),
            "EFRIS T130 Price Sync Failed",
        )

        state["req_fails"] += 1

        state["wait_until"] = str(
            add_to_date(
                now,
                minutes=min(
                    state["req_fails"] * 5,
                    60,
                ),
            )
        )

        save_state(state)

        return

    for item_name in names:
        if item_name in rejected_now:
            reason = str(rejected_now[item_name])
            short_reason = reason[:60]

            old = rejected_state.get(
                item_name,
                ["", 0],
            )

            rejected_state[item_name] = [
                short_reason,
                old[1] + 1,
            ]

        else:
            rejected_state.pop(
                item_name,
                None,
            )

    state["rejected"] = rejected_state
    state["upto"] = str(cutoff)
    state["req_fails"] = 0
    state["wait_until"] = None
    state["reject_retry"] = str(
        add_to_date(
            now,
            minutes=REJECT_RETRY_MIN,
        )
    )

    save_state(state)


## T109 pre-invoice protection

def sync_prices_before_invoice(doc):
    """
    Call this immediately before the existing T109 invoice upload.

    It catches invoice Items whose price changed recently but has not yet been
    picked up by the scheduled quiet-period batch.

    This function intentionally raises when EFRIS rejects an Item, because
    sending T109 immediately after a known T130 price rejection could leave
    EFRIS with a different Item price from ERPNext.
    """
    state = load_state()
    upto = state.get("upto")

    if not upto:
        return

    invoice_codes = {
        row.item_code
        for row in (doc.items or [])
        if row.item_code
    }

    if not invoice_codes:
        return

    names = sorted({
        row[0]
        for row in changed_rows(upto)
        if row[0] in invoice_codes
    })

    if not names:
        return

    try:
        rejected = push_names(names)

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
