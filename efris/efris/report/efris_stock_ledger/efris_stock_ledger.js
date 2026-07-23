// Copyright (c) 2026, Othieno Benedict Ernest and contributors
// For license information, please see license.txt

frappe.query_reports["EFRIS Stock Ledger"] = {
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		const qtyIn = data ? Number(data.qty_in || 0) : 0;
		const qtyOut = data ? Number(data.qty_out || 0) : 0;

		if (column.fieldname === "qty_in" && qtyIn > 0) {
			return `<span style="color: #1e9e57; font-weight: 600;">${value}</span>`;
		}

		if (column.fieldname === "qty_out" && qtyOut > 0) {
			return `<span style="color: #d94841; font-weight: 600;">${value}</span>`;
		}

		return value;
	},
	"filters": [
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Closing Balances", "Ledger Entries"],
			default: "Closing Balances",
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
			depends_on: "eval:doc.view == 'Ledger Entries'",
		},
		{
			fieldname: "to_date",
			label: __("As Of / To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "from_time",
			label: __("From Time"),
			fieldtype: "Time",
			depends_on: "eval:doc.view == 'Ledger Entries'",
		},
		{
			fieldname: "to_time",
			label: __("To Time"),
			fieldtype: "Time",
		},
		{
			fieldname: "item_code",
			label: __("Item Code"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "efris_product_code",
			label: __("EFRIS Product Code"),
			fieldtype: "Data",
		},
		{
			fieldname: "voucher_type",
			label: __("Voucher Type"),
			fieldtype: "Select",
			options: ["", "T127", "Sales Invoice"],
		},
		{
			fieldname: "voucher_no",
			label: __("Voucher No"),
			fieldtype: "Data",
		},
		{
			fieldname: "sales_invoice",
			label: __("Sales Invoice"),
			fieldtype: "Link",
			options: "Sales Invoice",
		},
		{
			fieldname: "include_opening_entries",
			label: __("Include Opening Entries"),
			fieldtype: "Check",
			default: 1,
			depends_on: "eval:doc.view == 'Ledger Entries'",
		},
	]
};
