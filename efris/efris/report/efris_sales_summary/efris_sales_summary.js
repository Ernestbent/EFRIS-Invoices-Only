// Copyright (c) 2026, Othieno Benedict Ernest and contributors
// For license information, please see license.txt

frappe.query_reports["EFRIS Sales Summary"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: moment(frappe.datetime.get_today()).startOf("year").format("YYYY-MM-DD"),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: "Daily\nWeekly\nMonthly\nYearly",
			default: "Monthly",
			reqd: 1,
		},
		{
			fieldname: "invoice_kind",
			label: __("Document Type"),
			fieldtype: "Select",
			options: "Both\nInvoice\nReceipt",
			default: "Both",
			reqd: 1,
		},
	],
	get_chart_data(columns, result) {
		const rows = (result || []).filter(
			(row) => row && row.period && row.gross_sales !== undefined
		);

		const get_label = (row) => {
			const sort_key = String(row._sort_key || "");
			const period = String(row.period || "");

			if (/^\d{4}$/.test(sort_key)) {
				return sort_key;
			}
			if (/^\d{4}-\d{2}$/.test(sort_key)) {
				return moment(sort_key, "YYYY-MM").format("MMM YYYY");
			}
			if (/^\d{4}-\d{2}-\d{2}$/.test(sort_key)) {
				const date = moment(sort_key, "YYYY-MM-DD");
				return period.includes("–")
					? __("Week of {0}", [date.format("DD MMM")])
					: date.format("DD MMM YYYY");
			}
			return period;
		};

		return {
			data: {
				labels: rows.map(get_label),
				datasets: [
					{
						name: __("Gross Sales"),
						values: rows.map((row) => flt(row.gross_sales)),
					},
				],
			},
			type: "donut",
			height: 360,
			colors: ["#0b78c2", "#3fa7c4", "#b7dce7", "#f7c600"],
			truncateLegends: 1,
			tooltipOptions: {
				formatTooltipY: (value) => format_number(value, null, 0),
			},
		};
	},
};
