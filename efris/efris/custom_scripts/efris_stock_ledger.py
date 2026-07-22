import frappe
from frappe.utils import flt

from efris.efris.custom_scripts.upload_invoice import (
	get_invoice_reference_no,
	get_item_efris_data,
)

SALES_INVOICE_VOUCHER_TYPE = "Sales Invoice"


def get_latest_item_balance(item_code="", efris_product_code=""):
	filters = {}
	if item_code:
		filters["item_code"] = item_code
	elif efris_product_code:
		filters["efris_product_code"] = efris_product_code
	else:
		return 0

	result = frappe.get_all(
		"EFRIS Stock Ledger Entry",
		filters=filters,
		fields=["balance"],
		order_by="posting_date desc, posting_time desc, creation desc",
		limit=1,
	)

	if not result:
		return 0

	return flt(result[0].get("balance"))


@frappe.whitelist()
def get_sales_invoice_item_efris_stock(item_code="", efris_product_code=""):
	if item_code and not efris_product_code:
		efris_product_code = (
			frappe.get_cached_value("Item", item_code, "custom_efris_product_code") or ""
		)

	return {
		"success": True,
		"efris_qty": get_latest_item_balance(
			item_code=item_code,
			efris_product_code=efris_product_code,
		),
	}


def has_sales_invoice_stock_movement(sales_invoice, item_code="", efris_product_code=""):
	filters = {
		"voucher_type": SALES_INVOICE_VOUCHER_TYPE,
		"sales_invoice": sales_invoice,
	}
	if item_code:
		filters["item_code"] = item_code
	elif efris_product_code:
		filters["efris_product_code"] = efris_product_code
	else:
		return False

	return bool(frappe.db.get_value("EFRIS Stock Ledger Entry", filters, "name"))


def aggregate_invoice_items(doc):
	aggregated = {}

	for item in getattr(doc, "items", []) or []:
		efris_data = get_item_efris_data(item)
		item_code = getattr(item, "item_code", "") or ""
		efris_product_code = efris_data.get("product_code", "")
		key = item_code or efris_product_code or getattr(item, "name", "")
		if not key:
			continue

		if key not in aggregated:
			aggregated[key] = {
				"item_code": item_code,
				"item_name": getattr(item, "item_name", "") or "",
				"uom": getattr(item, "custom_efris_uom", None) or getattr(item, "uom", "") or "",
				"efris_goods_name": efris_data.get("goods_service_name", ""),
				"efris_product_code": efris_product_code,
				"qty_out": 0,
			}

		aggregated[key]["qty_out"] += flt(getattr(item, "qty", 0))

	return aggregated


def create_sales_invoice_movement_entry(doc, movement):
	item_code = movement.get("item_code", "")
	efris_product_code = movement.get("efris_product_code", "")

	if has_sales_invoice_stock_movement(doc.name, item_code=item_code, efris_product_code=efris_product_code):
		return False

	previous_balance = get_latest_item_balance(item_code=item_code, efris_product_code=efris_product_code)
	qty_out = flt(movement.get("qty_out"))
	new_balance = previous_balance - qty_out

	entry = frappe.get_doc(
		{
			"doctype": "EFRIS Stock Ledger Entry",
			"posting_date": doc.posting_date,
			"posting_time": doc.posting_time,
			"item_code": item_code,
			"item_name": movement.get("item_name", ""),
			"uom": movement.get("uom", ""),
			"efris_goods_name": movement.get("efris_goods_name", ""),
			"efris_product_code": efris_product_code,
			"qty_in": 0,
			"qty_out": qty_out,
			"balance": new_balance,
			"voucher_type": SALES_INVOICE_VOUCHER_TYPE,
			"voucher_no": get_invoice_reference_no(doc),
			"sales_invoice": doc.name,
			"is_opening_entry": 0,
		}
	)
	entry.insert(ignore_permissions=True)
	return True


def process_sales_invoice_efris_stock_movement(doc, method=None):
	if isinstance(doc, str):
		doc = frappe.get_doc("Sales Invoice", doc)

	if not getattr(doc, "custom_efris_synced", 0):
		return {"success": True, "created": 0, "skipped": 0}

	if getattr(doc, "docstatus", 0) != 1:
		return {"success": True, "created": 0, "skipped": 0}

	movements = aggregate_invoice_items(doc)
	created = 0
	skipped = 0

	for movement in movements.values():
		if create_sales_invoice_movement_entry(doc, movement):
			created += 1
		else:
			skipped += 1

	frappe.db.commit()
	return {
		"success": True,
		"created": created,
		"skipped": skipped,
	}


@frappe.whitelist()
def sync_sales_invoice_stock_movement(invoice_name):
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	return process_sales_invoice_efris_stock_movement(doc)
