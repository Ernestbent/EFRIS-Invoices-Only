# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate


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
		{"label": _("PDF Attached On"), "fieldname": "pdf_attached_on", "fieldtype": "Datetime", "width": 155},
		{"label": _("URA PDF"), "fieldname": "pdf_url", "fieldtype": "Data", "width": 90},
	]


def _validate_filters(filters):
	if filters.get("from_date") and filters.get("to_date"):
		if getdate(filters.from_date) > getdate(filters.to_date):
			frappe.throw(_("From Date cannot be after To Date"))
	if filters.get("pdf_from_date") and filters.get("pdf_to_date"):
		if getdate(filters.pdf_from_date) > getdate(filters.pdf_to_date):
			frappe.throw(_("PDF Attached From cannot be after PDF Attached To"))


def _get_efris_pdf_files(invoice_names=None, from_date=None, to_date=None):
	file_filters = [
		["File", "attached_to_doctype", "=", "Sales Invoice"],
		["File", "file_type", "=", "PDF"],
		["File", "file_name", "like", "%-EFRIS%.pdf"],
	]

	if invoice_names is not None:
		if not invoice_names:
			return []
		file_filters.append(["File", "attached_to_name", "in", invoice_names])
	if from_date:
		file_filters.append(["File", "creation", ">=", get_datetime(from_date)])
	if to_date:
		file_filters.append(
			["File", "creation", "<", get_datetime(add_days(getdate(to_date), 1))]
		)

	return frappe.get_all(
		"File",
		filters=file_filters,
		fields=["attached_to_name", "file_name", "file_url", "creation"],
		order_by="creation desc",
	)


def _get_pdf_filtered_invoice_names(filters):
	if not (filters.get("pdf_from_date") or filters.get("pdf_to_date")):
		return None

	files = _get_efris_pdf_files(
		from_date=filters.get("pdf_from_date"),
		to_date=filters.get("pdf_to_date"),
	)
	return sorted({file.attached_to_name for file in files if file.attached_to_name})


def _get_invoices(filters):
	query_filters = [
		["Sales Invoice", "docstatus", "=", 1],
		["Sales Invoice", "custom_efris_synced", "=", 1],
	]

	pdf_invoice_names = _get_pdf_filtered_invoice_names(filters)
	if pdf_invoice_names is not None:
		if not pdf_invoice_names:
			return []
		query_filters.append(["Sales Invoice", "name", "in", pdf_invoice_names])

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

	files = _get_efris_pdf_files(invoice_names=invoice_names)

	pdf_by_invoice = {}
	for file in files:
		pdf_by_invoice.setdefault(file.attached_to_name, file)

	for row in rows:
		pdf_file = pdf_by_invoice.get(row.sales_invoice)
		row.pdf_url = pdf_file.file_url if pdf_file else None
		row.pdf_attached_on = pdf_file.creation if pdf_file else None
		row.pdf_status = _("Attached") if row.pdf_url else _("Missing")
