# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 150,
		},
		{"label": _("Item Name"), "fieldname": "item_name", "fieldtype": "Data", "width": 190},
		{
			"label": _("EFRIS Product Code"),
			"fieldname": "efris_product_code",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": _("EFRIS Price"),
			"fieldname": "efris_price",
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"label": _("EFRIS Qty"),
			"fieldname": "efris_qty",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("ERPNext Warehouse Qty"),
			"fieldname": "erpnext_qty",
			"fieldtype": "Float",
			"width": 160,
		},
		{
			"label": _("Difference"),
			"fieldname": "difference",
			"fieldtype": "Float",
			"width": 110,
		},
	]


def get_data(filters):
	item_conditions = [
		"item.is_stock_item = 1",
		"IFNULL(item.custom_efris_product_code, '') != ''",
	]
	stock_conditions = ["1 = 1"]
	query_values = {}

	if not cint(filters.get("include_disabled")):
		item_conditions.append("item.disabled = 0")
	if filters.get("item_code"):
		item_conditions.append("item.item_code = %(item_code)s")
		query_values["item_code"] = filters.item_code
	if filters.get("item_group"):
		item_conditions.append("item.item_group = %(item_group)s")
		query_values["item_group"] = filters.item_group
	if filters.get("company"):
		stock_conditions.append("warehouse.company = %(company)s")
		query_values["company"] = filters.company
	if filters.get("warehouse"):
		stock_conditions.append("bin.warehouse = %(warehouse)s")
		query_values["warehouse"] = filters.warehouse

	rows = frappe.db.sql(
		f"""
		SELECT
			item.item_code,
			item.item_name,
			item.custom_efris_product_code AS efris_product_code,
			item.custom_efris_price AS efris_price,
			COALESCE(
				(
					SELECT ledger.balance
					FROM `tabEFRIS Stock Ledger Entry` ledger
					WHERE ledger.item_code = item.item_code
					ORDER BY
						ledger.posting_date DESC,
						ledger.posting_time DESC,
						ledger.creation DESC
					LIMIT 1
				),
				0
			) AS efris_qty,
			COALESCE(stock.erpnext_qty, 0) AS erpnext_qty
		FROM `tabItem` item
		LEFT JOIN (
			SELECT
				bin.item_code,
				SUM(bin.actual_qty) AS erpnext_qty
			FROM `tabBin` bin
			INNER JOIN `tabWarehouse` warehouse
				ON warehouse.name = bin.warehouse
			WHERE {" AND ".join(stock_conditions)}
			GROUP BY bin.item_code
		) stock
			ON stock.item_code = item.item_code
		WHERE {" AND ".join(item_conditions)}
		ORDER BY item.item_code
		""",
		query_values,
		as_dict=True,
	)

	data = []
	for row in rows:
		efris_qty = flt(row.efris_qty)
		erpnext_qty = flt(row.erpnext_qty)
		difference = efris_qty - erpnext_qty
		if cint(filters.get("only_differences")) and not difference:
			continue

		data.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"efris_product_code": row.efris_product_code,
				"efris_price": flt(str(row.efris_price or "").replace(",", "")),
				"efris_qty": efris_qty,
				"erpnext_qty": erpnext_qty,
				"difference": difference,
			}
		)

	return data
