# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	validate_filters(filters)

	columns = get_columns(filters)
	data = get_data(filters)
	return columns, data


def validate_filters(filters):
	if (
		filters.view != "Closing Balances"
		and filters.from_date
		and filters.to_date
		and getdate(filters.from_date) > getdate(filters.to_date)
	):
		frappe.throw(_("From Date cannot be after To Date."))
	if filters.view == "Closing Balances" and not filters.to_date:
		frappe.throw(_("As Of / To Date is required for the Closing Balances view."))


def get_columns(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{
			"label": _("Posting Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 105,
		},
		{
			"label": _("Posting Time"),
			"fieldname": "posting_time",
			"fieldtype": "Time",
			"width": 95,
		},
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 150,
		},
		{
			"label": _("Item Name"),
			"fieldname": "item_name",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("EFRIS Product Code"),
			"fieldname": "efris_product_code",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("EFRIS Goods Name"),
			"fieldname": "efris_goods_name",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("In Qty"),
			"fieldname": "qty_in",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("Out Qty"),
			"fieldname": "qty_out",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("Balance Qty"),
			"fieldname": "balance",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Voucher Type"),
			"fieldname": "voucher_type",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Voucher No"),
			"fieldname": "voucher_no",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Sales Invoice"),
			"fieldname": "sales_invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"width": 150,
		},
	]

	if filters.view == "Closing Balances":
		return [
			column
			for column in columns
			if column["fieldname"] not in {"qty_in", "qty_out"}
		]

	return columns


def get_data(filters):
	if filters.view == "Closing Balances":
		return get_closing_balance_rows(filters)

	opening_rows = get_opening_rows(filters)
	ledger_rows = get_ledger_rows(filters)
	return opening_rows + ledger_rows


def get_closing_balance_rows(filters):
	"""Return each item's last recorded EFRIS balance at the requested date and time."""
	conditions = get_common_conditions(filters, include_opening_entries=True)
	conditions.append(
		"(posting_date < %(to_date)s OR "
		"(posting_date = %(to_date)s AND posting_time <= %(closing_time)s))"
	)
	query_values = dict(filters)
	query_values["closing_time"] = filters.to_time or "23:59:59"

	entries = frappe.db.sql(
		f"""
		SELECT
			posting_date,
			posting_time,
			item_code,
			item_name,
			efris_product_code,
			efris_goods_name,
			uom,
			balance,
			voucher_type,
			voucher_no,
			sales_invoice,
			is_opening_entry
		FROM `tabEFRIS Stock Ledger Entry`
		WHERE {" AND ".join(conditions)}
		ORDER BY posting_date DESC, posting_time DESC, creation DESC
		""",
		query_values,
		as_dict=True,
	)

	latest_by_item = {}
	for entry in entries:
		key = build_group_key(entry)
		if key not in latest_by_item:
			entry.qty_in = 0
			entry.qty_out = 0
			latest_by_item[key] = entry

	rows = list(latest_by_item.values())
	rows.sort(key=lambda row: ((row.get("item_code") or ""), (row.get("efris_product_code") or "")))
	return rows


def get_opening_rows(filters):
	if not filters.from_date:
		return []

	conditions = get_common_conditions(filters)
	conditions.append("posting_date < %(from_date)s")

	opening_entries = frappe.db.sql(
		f"""
		SELECT
			posting_date,
			posting_time,
			item_code,
			item_name,
			efris_product_code,
			efris_goods_name,
			uom,
			balance
		FROM `tabEFRIS Stock Ledger Entry`
		WHERE {" AND ".join(conditions)}
		ORDER BY posting_date DESC, posting_time DESC, creation DESC
		""",
		filters,
		as_dict=True,
	)

	latest_by_key = {}
	for entry in opening_entries:
		key = build_group_key(entry)
		if key not in latest_by_key:
			latest_by_key[key] = entry

	rows = []
	for entry in latest_by_key.values():
		rows.append(
			{
				"posting_date": entry.posting_date,
				"posting_time": entry.posting_time,
				"item_code": entry.item_code,
				"item_name": entry.item_name or _("Opening"),
				"efris_product_code": entry.efris_product_code,
				"efris_goods_name": entry.efris_goods_name,
				"uom": entry.uom,
				"qty_in": 0,
				"qty_out": 0,
				"balance": flt(entry.balance),
				"voucher_type": "Opening",
				"voucher_no": "",
				"sales_invoice": "",
				"is_opening_entry": 1,
			}
		)

	rows.sort(key=lambda row: ((row.get("item_code") or ""), (row.get("efris_product_code") or "")))
	return rows


def get_ledger_rows(filters):
	conditions = get_common_conditions(filters)
	if filters.from_date:
		conditions.append("posting_date >= %(from_date)s")
	if filters.to_date:
		conditions.append("posting_date <= %(to_date)s")
	if filters.from_time:
		conditions.append("posting_time >= %(from_time)s")
	if filters.to_time:
		conditions.append("posting_time <= %(to_time)s")

	ledger_rows = frappe.db.sql(
		f"""
		SELECT
			posting_date,
			posting_time,
			item_code,
			item_name,
			efris_product_code,
			efris_goods_name,
			uom,
			qty_in,
			qty_out,
			balance,
			voucher_type,
			voucher_no,
			sales_invoice,
			is_opening_entry
		FROM `tabEFRIS Stock Ledger Entry`
		WHERE {" AND ".join(conditions)}
		ORDER BY posting_date ASC, posting_time ASC, creation ASC
		""",
		filters,
		as_dict=True,
	)

	return ledger_rows


def get_common_conditions(filters, include_opening_entries=None):
	conditions = ["1=1"]

	if filters.item_code:
		conditions.append("item_code = %(item_code)s")
	if filters.efris_product_code:
		conditions.append("efris_product_code = %(efris_product_code)s")
	if filters.voucher_type:
		conditions.append("voucher_type = %(voucher_type)s")
	if filters.voucher_no:
		conditions.append("voucher_no = %(voucher_no)s")
	if filters.sales_invoice:
		conditions.append("sales_invoice = %(sales_invoice)s")
	if include_opening_entries is None:
		include_opening_entries = cint(filters.include_opening_entries) == 1
	if not include_opening_entries:
		conditions.append("IFNULL(is_opening_entry, 0) = 0")

	return conditions


def build_group_key(entry):
	return (
		entry.get("item_code") or "",
		entry.get("efris_product_code") or "",
		entry.get("uom") or "",
	)
