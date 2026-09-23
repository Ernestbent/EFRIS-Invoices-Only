# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import getdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate_filters(filters)

	rows = _get_invoices(filters)
	_add_pdf_details(rows)

	if filters.get("pdf_status"):
		rows = [row for row in rows if row.pdf_status == filters.pdf_status]

	return get_columns(), rows


def get_columns():
	return [
		{
			"label": _("Sales Invoice"),
			"fieldname": "sales_invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"width": 190,
		},
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 105},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 150},
		{"label": _("Customer Name"), "fieldname": "customer_name", "fieldtype": "Data", "width": 180},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 150},
		{"label": _("Grand Total"), "fieldname": "grand_total", "fieldtype": "Currency", "options": "currency", "width": 125},
		{"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link", "options": "Currency", "width": 85},
		{"label": _("FDN"), "fieldname": "fdn", "fieldtype": "Data", "width": 155},
		{"label": _("EFRIS Invoice ID"), "fieldname": "efris_invoice_id", "fieldtype": "Data", "width": 155},
		{"label": _("Verification Code"), "fieldname": "verification_code", "fieldtype": "Data", "width": 155},
		{"label": _("PDF Status"), "fieldname": "pdf_status", "fieldtype": "Data", "width": 90},
		{"label": _("URA PDF"), "fieldname": "pdf_url", "fieldtype": "Data", "width": 90},
	]


def _validate_filters(filters):
	if filters.get("from_date") and filters.get("to_date"):
		if getdate(filters.from_date) > getdate(filters.to_date):
			frappe.throw(_("From Date cannot be after To Date"))


def _get_invoices(filters):
	query_filters = [
		["Sales Invoice", "docstatus", "=", 1],
		["Sales Invoice", "custom_efris_synced", "=", 1],
	]

	if filters.get("from_date"):
		query_filters.append(["Sales Invoice", "posting_date", ">=", filters.from_date])
	if filters.get("to_date"):
		query_filters.append(["Sales Invoice", "posting_date", "<=", filters.to_date])
	if filters.get("company"):
		query_filters.append(["Sales Invoice", "company", "=", filters.company])
	if filters.get("customer"):
		query_filters.append(["Sales Invoice", "customer", "=", filters.customer])
	if filters.get("sales_invoice"):
		query_filters.append(["Sales Invoice", "name", "=", filters.sales_invoice])

	rows = frappe.get_list(
		"Sales Invoice",
		filters=query_filters,
		fields=[
			"name as sales_invoice",
			"posting_date",
			"posting_time",
			"customer",
			"customer_name",
			"company",
			"grand_total",
			"currency",
			"custom_fdn as fdn",
			"custom_invoice_number as efris_invoice_id",
			"custom_verification_code as verification_code",
		],
		order_by="posting_date desc, posting_time desc, creation desc",
		limit_page_length=0,
	)

	return [frappe._dict(row) for row in rows]


def _add_pdf_details(rows):
	invoice_names = [row.sales_invoice for row in rows]
	if not invoice_names:
		return

	files = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": ["in", invoice_names],
			"file_type": "PDF",
		},
		fields=["attached_to_name", "file_name", "file_url", "creation"],
		order_by="creation desc",
	)

	pdf_by_invoice = {}
	for file in files:
		if "-EFRIS" not in (file.file_name or "").upper():
			continue
		pdf_by_invoice.setdefault(file.attached_to_name, file.file_url)

	for row in rows:
		row.pdf_url = pdf_by_invoice.get(row.sales_invoice)
		row.pdf_status = _("Attached") if row.pdf_url else _("Missing")
