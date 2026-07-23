import frappe
from frappe.utils import add_days, cint, flt, getdate, nowdate

from efris.efris.custom_scripts.upload_invoice import (
	get_invoice_reference_no,
	get_item_efris_data,
)

SALES_INVOICE_VOUCHER_TYPE = "Sales Invoice"


def get_latest_item_balance(item_code="", efris_product_code=""):
	filter_candidates = []
	if efris_product_code:
		filter_candidates.append({"efris_product_code": efris_product_code})
	if item_code:
		filter_candidates.append({"item_code": item_code})

	for filters in filter_candidates:
		result = frappe.get_all(
			"EFRIS Stock Ledger Entry",
			filters=filters,
			fields=["balance"],
			order_by="posting_date desc, posting_time desc, creation desc",
			limit=1,
		)
		if result:
			return flt(result[0].get("balance"))

	return 0


def get_all_warehouses_stock_qty(item_code=""):
	if not item_code:
		return 0

	warehouse_bins = frappe.get_all(
		"Bin",
		filters={"item_code": item_code},
		fields=["actual_qty"],
	)
	return sum(flt(warehouse_bin.get("actual_qty")) for warehouse_bin in warehouse_bins)


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
		"all_warehouses_qty": get_all_warehouses_stock_qty(item_code),
	}


@frappe.whitelist()
def get_sales_invoice_efris_stock_rows(invoice_name="", items=None):
	if invoice_name:
		doc = frappe.get_doc("Sales Invoice", invoice_name)
		doc.check_permission("read")
	else:
		doc = None

	if isinstance(items, str):
		items = frappe.parse_json(items)

	if items is None and doc:
		items = [
			{
				"row_name": item.name,
				"item_code": item.item_code,
				"efris_product_code": getattr(item, "custom_efris_product_code", "") or "",
			}
			for item in doc.items
		]

	items = items or []
	if not isinstance(items, list):
		frappe.throw("Sales Invoice items must be a list.")
	if len(items) > 500:
		frappe.throw("Cannot fetch EFRIS stock for more than 500 invoice rows at once.")

	rows = {}
	for item in items:
		if not isinstance(item, dict):
			frappe.throw("Each Sales Invoice item must be an object.")

		row_name = str(item.get("row_name") or "").strip()
		if not row_name:
			continue

		rows[row_name] = get_sales_invoice_item_efris_stock(
			item_code=str(item.get("item_code") or "").strip(),
			efris_product_code=str(item.get("efris_product_code") or "").strip(),
		)

	return {
		"success": True,
		"rows": rows,
		"warehouse_scope": "all",
	}


def backfill_sales_invoice_stock_for_creation_date(creation_date="", dry_run=1):
	"""Populate EFRIS stock fields without changing invoice or item modified timestamps."""
	target_date = getdate(creation_date or nowdate())
	next_date = add_days(target_date, 1)
	invoices = frappe.get_all(
		"Sales Invoice",
		filters=[
			["creation", ">=", f"{target_date} 00:00:00"],
			["creation", "<", f"{next_date} 00:00:00"],
		],
		fields=["name", "modified"],
		order_by="creation asc",
	)
	invoice_names = [invoice.name for invoice in invoices]
	if not invoice_names:
		return {
			"success": True,
			"dry_run": bool(cint(dry_run)),
			"creation_date": str(target_date),
			"invoices_found": 0,
			"item_rows_updated": 0,
		}

	items = frappe.get_all(
		"Sales Invoice Item",
		filters={
			"parenttype": "Sales Invoice",
			"parent": ["in", invoice_names],
		},
		fields=[
			"name",
			"parent",
			"item_code",
			"custom_efris_product_code",
			"modified",
		],
		order_by="parent asc, idx asc",
	)
	stock_by_item = {}
	updates = []

	for item in items:
		item_code = str(item.item_code or "").strip()
		product_code = str(item.custom_efris_product_code or "").strip()
		stock_key = (item_code, product_code)
		if stock_key not in stock_by_item:
			stock_by_item[stock_key] = get_sales_invoice_item_efris_stock(
				item_code=item_code,
				efris_product_code=product_code,
			)

		stock = stock_by_item[stock_key]
		efris_qty = flt(stock.get("efris_qty"))
		all_warehouses_qty = flt(stock.get("all_warehouses_qty"))
		updates.append(
			{
				"name": item.name,
				"custom_efris_qty": efris_qty,
				# Retain the existing fieldname for database compatibility; its label is All Warehouses Qty.
				"custom_containers_qty": all_warehouses_qty,
				"custom_diffefris__main": efris_qty - all_warehouses_qty,
			}
		)

	if cint(dry_run):
		return {
			"success": True,
			"dry_run": True,
			"creation_date": str(target_date),
			"invoices_found": len(invoices),
			"item_rows_updated": len(updates),
		}

	invoice_modified_before = {invoice.name: invoice.modified for invoice in invoices}
	item_modified_before = {item.name: item.modified for item in items}

	try:
		for update in updates:
			item_name = update.pop("name")
			frappe.db.set_value(
				"Sales Invoice Item",
				item_name,
				update,
				update_modified=False,
			)

		invoice_modified_after = dict(
			frappe.get_all(
				"Sales Invoice",
				filters={"name": ["in", invoice_names]},
				fields=["name", "modified"],
				as_list=True,
			)
		)
		item_modified_after = dict(
			frappe.get_all(
				"Sales Invoice Item",
				filters={"name": ["in", list(item_modified_before)]},
				fields=["name", "modified"],
				as_list=True,
			)
		)
		if invoice_modified_after != invoice_modified_before:
			raise RuntimeError("A Sales Invoice modified timestamp changed; no changes were committed.")
		if item_modified_after != item_modified_before:
			raise RuntimeError("A Sales Invoice Item modified timestamp changed; no changes were committed.")

		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise

	return {
		"success": True,
		"dry_run": False,
		"creation_date": str(target_date),
		"invoices_found": len(invoices),
		"item_rows_updated": len(updates),
		"modified_timestamps_preserved": True,
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
