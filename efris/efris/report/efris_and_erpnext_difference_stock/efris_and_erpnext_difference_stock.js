// Copyright (c) 2026, Othieno Benedict Ernest and contributors
// For license information, please see license.txt

frappe.query_reports["EFRIS and ERPNext Difference Stock"] = {
	formatter(value, row, column, data, default_formatter) {
		const formatted_value = default_formatter(value, row, column, data);
		const numeric_fieldtypes = ["Currency", "Float", "Int"];

		if (data && numeric_fieldtypes.includes(column.fieldtype) && Number(data[column.fieldname]) < 0) {
			return `<span style="color: var(--red-600); font-weight: 600;">${formatted_value}</span>`;
		}

		return formatted_value;
	},
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: () => {
				const company = frappe.query_report.get_filter_value("company");
				const filters = { is_group: 0 };
				if (company) filters.company = company;
				return { filters };
			},
		},
		{
			fieldname: "item_code",
			label: __("Item Code"),
			fieldtype: "Link",
			options: "Item",
			get_query: () => ({
				filters: {
					is_stock_item: 1,
					disabled: 0,
				},
			}),
		},
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group",
		},
		{
			fieldname: "only_differences",
			label: __("Only Differences"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "include_disabled",
			label: __("Include Disabled Items"),
			fieldtype: "Check",
			default: 0,
		},
	],
};
