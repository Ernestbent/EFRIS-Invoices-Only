import frappe
from frappe.utils import flt

from efris.efris.custom_scripts.efris_stock_ledger import get_latest_item_balance
from efris.efris.custom_scripts.upload_invoice import get_item_efris_data


def build_zero_stock_rows(doc):
	zero_stock_rows = []

	for item in getattr(doc, "items", []) or []:
		efris_data = get_item_efris_data(item)
		item_code = getattr(item, "item_code", "") or ""
		efris_product_code = efris_data.get("product_code", "")
		balance = get_latest_item_balance(
			item_code=item_code,
			efris_product_code=efris_product_code,
		)

		qty = flt(getattr(item, "qty", 0))
		if flt(balance) < qty:
			remove_qty = qty if flt(balance) <= 0 else qty - flt(balance)
			zero_stock_rows.append(
				{
					"row_name": item.name,
					"idx": item.idx,
					"item_code": item_code,
					"item_name": getattr(item, "item_name", "") or "",
					"qty": qty,
					"balance": flt(balance),
					"remove_qty": flt(remove_qty),
					"shortage_qty": flt(qty - flt(balance)),
					"efris_product_code": efris_product_code,
				}
			)

	return zero_stock_rows


@frappe.whitelist()
def validate_sales_invoice_efris_stock(invoice_name):
	doc = frappe.get_doc("Sales Invoice", invoice_name)
	zero_stock_rows = build_zero_stock_rows(doc)

	return {
		"success": True,
		"has_zero_stock_items": bool(zero_stock_rows),
		"items": zero_stock_rows,
	}
