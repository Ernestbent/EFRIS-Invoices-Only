// Copyright (c) 2026, Othieno Benedict Ernest and contributors
// For license information, please see license.txt

frappe.query_reports["EFRIS Invoice Register"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "customer",
			label: __("Customer"),
			fieldtype: "Link",
			options: "Customer",
		},
		{
			fieldname: "sales_invoice",
			label: __("Sales Invoice"),
			fieldtype: "Link",
			options: "Sales Invoice",
		},
		{
			fieldname: "pdf_status",
			label: __("PDF Status"),
			fieldtype: "Select",
			options: "\nAttached\nMissing",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		if (column.fieldname === "pdf_url") {
			if (!value) {
				return `<span class="text-muted">${__("Missing")}</span>`;
			}

			const url = frappe.utils.escape_html(value);
			return `<a href="${url}" target="_blank" rel="noopener noreferrer">${__(
				"View PDF"
			)}</a>`;
		}

		return default_formatter(value, row, column, data);
	},
};
