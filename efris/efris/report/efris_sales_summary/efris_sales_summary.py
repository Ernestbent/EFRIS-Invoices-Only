# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# For license information, please see license.txt

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import getdate

from efris.efris.custom_scripts.efris_invoice_query import fetch_t106_invoices

DATE_FORMATS = (
	"%d/%m/%Y %H:%M:%S",
	"%d/%m/%Y",
	"%Y-%m-%d %H:%M:%S",
	"%Y-%m-%d",
)
GROUP_BY_OPTIONS = {"Daily", "Weekly", "Monthly", "Yearly"}
INVOICE_KIND_OPTIONS = {"Both", "Invoice", "Receipt"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	from_date, to_date = validate_filters(filters)
	records = fetch_t106_invoices(
		from_date,
		to_date,
		filters.get("invoice_kind") or "Both",
	)
	rows = aggregate_records(records, filters.get("group_by") or "Monthly")

	return (
		get_columns(),
		rows,
		None,
		get_chart(rows),
		get_report_summary(rows),
	)


def validate_filters(filters):
	from_date = getdate(filters.get("from_date"))
	to_date = getdate(filters.get("to_date"))

	if from_date > to_date:
		frappe.throw(_("From Date cannot be after To Date"))
	if (to_date - from_date).days > 366:
		frappe.throw(_("Choose a date range of 367 days or less for the live T106 report."))
	if (filters.get("group_by") or "Monthly") not in GROUP_BY_OPTIONS:
		frappe.throw(_("Invalid Group By value."))
	if (filters.get("invoice_kind") or "Both") not in INVOICE_KIND_OPTIONS:
		frappe.throw(_("Invalid Invoice Kind value."))

	return from_date, to_date


def get_columns():
	return [
		{
			"label": _("Period"),
			"fieldname": "period",
			"fieldtype": "Data",
			"width": 190,
		},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Data",
			"width": 90,
		},
		{
			"label": _("Invoices/Receipts"),
			"fieldname": "document_count",
			"fieldtype": "Int",
			"width": 140,
		},
		{
			"label": _("Gross Sales"),
			"fieldname": "gross_sales",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 145,
		},
		{
			"label": _("Tax"),
			"fieldname": "tax_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 135,
		},
		{
			"label": _("Net Sales"),
			"fieldname": "net_sales",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 145,
		},
	]


def parse_issued_date(value):
	value = str(value or "").strip()
	for date_format in DATE_FORMATS:
		try:
			return datetime.strptime(value, date_format).date()
		except ValueError:
			continue
	return None


def decimal_value(value):
	try:
		return Decimal(str(value or "0").replace(",", "").strip())
	except (InvalidOperation, ValueError):
		return Decimal("0")


def get_period(issue_date, group_by):
	if group_by == "Daily":
		return issue_date.isoformat(), issue_date.isoformat()
	if group_by == "Weekly":
		week_start = issue_date - timedelta(days=issue_date.weekday())
		week_end = week_start + timedelta(days=6)
		return week_start.isoformat(), f"{week_start:%d %b %Y} – {week_end:%d %b %Y}"
	if group_by == "Yearly":
		return f"{issue_date.year:04d}", f"{issue_date.year:04d}"

	return issue_date.strftime("%Y-%m"), issue_date.strftime("%B %Y")


def aggregate_records(records, group_by):
	totals = defaultdict(
		lambda: {
			"document_count": 0,
			"gross_sales": Decimal("0"),
			"tax_amount": Decimal("0"),
		}
	)

	for record in records:
		if str(record.get("isInvalid") or "0") == "1":
			continue
		issue_date = parse_issued_date(record.get("issuedDate"))
		if not issue_date:
			continue

		sort_key, label = get_period(issue_date, group_by)
		currency = str(record.get("currency") or "UGX").strip().upper()
		key = (sort_key, label, currency)
		totals[key]["document_count"] += 1
		totals[key]["gross_sales"] += decimal_value(record.get("grossAmount"))
		totals[key]["tax_amount"] += decimal_value(record.get("taxAmount"))

	rows = []
	for (sort_key, label, currency), values in sorted(totals.items()):
		gross_sales = values["gross_sales"]
		tax_amount = values["tax_amount"]
		rows.append(
			{
				"period": label,
				"currency": currency,
				"document_count": values["document_count"],
				"gross_sales": float(gross_sales),
				"tax_amount": float(tax_amount),
				"net_sales": float(gross_sales - tax_amount),
				"_sort_key": sort_key,
			}
		)

	return rows


def get_chart_label(row):
	sort_key = str(row.get("_sort_key") or "")
	period = str(row.get("period") or "")

	if len(sort_key) == 4 and sort_key.isdigit():
		return sort_key
	if len(sort_key) == 7:
		try:
			return datetime.strptime(sort_key, "%Y-%m").strftime("%b %Y")
		except ValueError:
			pass
	if len(sort_key) == 10:
		issued_date = datetime.strptime(sort_key, "%Y-%m-%d")
		if "–" in period:
			return f"Week of {issued_date:%d %b}"
		return issued_date.strftime("%d %b %Y")
	return period


def get_chart(rows):
	if not rows:
		return None

	return {
		"data": {
			"labels": [get_chart_label(row) for row in rows],
			"datasets": [
				{
					"name": _("Gross Sales"),
					"values": [row["gross_sales"] for row in rows],
				},
			],
		},
		"type": "donut",
		"height": 360,
		"colors": ["#0b78c2", "#3fa7c4", "#b7dce7", "#f7c600"],
		"donutOptions": {"strokeWidth": 18},
		"truncateLegends": 1,
	}


def get_report_summary(rows):
	if not rows:
		return []

	by_currency = defaultdict(
		lambda: {
			"document_count": 0,
			"gross_sales": Decimal("0"),
			"tax_amount": Decimal("0"),
		}
	)
	for row in rows:
		currency_totals = by_currency[row["currency"]]
		currency_totals["document_count"] += row["document_count"]
		currency_totals["gross_sales"] += decimal_value(row["gross_sales"])
		currency_totals["tax_amount"] += decimal_value(row["tax_amount"])

	summary = []
	for currency, values in sorted(by_currency.items()):
		summary.extend(
			[
				{
					"value": values["document_count"],
					"label": _("{0} Documents").format(currency),
					"datatype": "Int",
				},
				{
					"value": float(values["gross_sales"]),
					"label": _("{0} Gross Sales").format(currency),
					"datatype": "Currency",
					"currency": currency,
					"indicator": "Blue",
				},
				{
					"value": float(values["tax_amount"]),
					"label": _("{0} Tax").format(currency),
					"datatype": "Currency",
					"currency": currency,
					"indicator": "Orange",
				},
				{
					"value": float(values["gross_sales"] - values["tax_amount"]),
					"label": _("{0} Net Sales").format(currency),
					"datatype": "Currency",
					"currency": currency,
					"indicator": "Green",
				},
			]
		)

	return summary
