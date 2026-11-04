frappe.query_reports["Branch Sales Variance"] = {
	filters: [
		{
			fieldname: "pos_profile",
			label: __("Branch"),
			fieldtype: "Link",
			options: "POS Profile",
			default: "Lyons Ruiru",
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.month_start(),
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
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Daily Money", "Cashier Summary", "Stock Variance", "Stock Movement", "Returns Check"],
			default: "Daily Money",
			reqd: 1,
		},
		{
			fieldname: "period",
			label: __("Group By"),
			fieldtype: "Select",
			options: ["Day", "Week", "Month"],
			default: "Day",
			depends_on: "eval:doc.view == 'Daily Money'",
		},
		{
			fieldname: "use_bank_statements",
			label: __("Use Imported Bank Statements"),
			fieldtype: "Check",
			default: 1,
			depends_on: "eval:doc.view == 'Daily Money'",
		},
		{
			fieldname: "cashier",
			label: __("Cashier"),
			fieldtype: "Link",
			options: "User",
			depends_on: "eval:['Cashier Summary', 'Returns Check'].includes(doc.view)",
		},
		{
			fieldname: "late_return_days",
			label: __("Flag Returns After (Days)"),
			fieldtype: "Int",
			default: 2,
			depends_on: "eval:doc.view == 'Returns Check'",
		},
	],

	tree: true,
	name_field: "name",
	parent_field: "parent_name",
	initial_depth: 0,

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		if (["variance", "value_at_sp", "adjustment_value", "close_difference", "recon_adjustment", "min_balance"].includes(column.fieldname)) {
			const v = flt(data[column.fieldname]);
			if (v < -0.5) value = `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
			else if (v > 0.5) value = `<span style="color:var(--green-600)">${value}</span>`;
		}
		if (column.fieldname === "source" && data.source && data.source.includes("No Statement")) {
			value = `<span style="color:var(--orange-600)">${value}</span>`;
		}
		if (column.fieldname === "flags" && data.flags) {
			value = `<span style="color:var(--red-600)">${value}</span>`;
		}
		if (column.fieldname === "uncounted_shifts" && data.uncounted_shifts) {
			value = `<span style="color:var(--orange-600);font-weight:600">${value}</span>`;
		}
		if (data.indent === 0 && data.label && column.fieldname !== "label" && data.bold) {
			value = `<b>${value}</b>`;
		}
		return value;
	},
};
