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
			options: ["Daily Money", "Channel Detail", "Cashier Summary", "Stock Variance", "Stock Movement", "Returns Check"],
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
			fieldname: "channel",
			label: __("Channel"),
			fieldtype: "Select",
			options: ["", "Equity Bank", "I&M Bank", "NCBA", "Co-op Bank", "Pochi", "Cash", "Other", "Credit / Unpaid"],
			depends_on: "eval:doc.view == 'Channel Detail'",
			mandatory_depends_on: "eval:doc.view == 'Channel Detail' && !doc.bank_account",
		},
		{
			fieldname: "bank_account",
			label: __("Bank Account"),
			fieldtype: "Link",
			options: "Bank Account",
			depends_on: "eval:['Daily Money', 'Channel Detail'].includes(doc.view)",
			get_query: () => ({ filters: { is_company_account: 1 } }),
			on_change() {
				// a picked bank wins over an older channel choice in Channel Detail
				const qr = frappe.query_report;
				if (qr.get_filter_value("bank_account") && qr.get_filter_value("channel")) {
					qr.set_filter_value("channel", "");
				} else {
					qr.refresh();
				}
			},
		},
		{
			fieldname: "use_bank_statements",
			label: __("Use Imported Bank Statements"),
			fieldtype: "Check",
			default: 1,
			depends_on: "eval:['Daily Money', 'Channel Detail'].includes(doc.view)",
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

	drill(channel, from_date, to_date) {
		const qr = frappe.query_report;
		qr.set_filter_value({
			view: "Channel Detail",
			channel: decodeURIComponent(channel),
			from_date: decodeURIComponent(from_date),
			to_date: decodeURIComponent(to_date),
		});
	},

	onload(report) {
		report.page.add_inner_button(__("Back to Daily Money"), () => {
			report.set_filter_value({ view: "Daily Money" });
		});
	},

	tree: true,
	name_field: "name",
	parent_field: "parent_name",
	initial_depth: 0,

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		// Daily Money: click a channel row to open its GL-style detail (like Trial Balance -> GL)
		if (column.fieldname === "label" && data.channel && data.period_from) {
			const args = [data.channel, data.period_from, data.period_to].map((a) => encodeURIComponent(a));
			return `<a href="#" title="${__("Open the detail for this channel")}"
				onclick="frappe.query_reports['Branch Sales Variance'].drill('${args[0]}','${args[1]}','${args[2]}'); return false;">${value}</a>`;
		}

		if (["variance", "value_at_sp", "adjustment_value", "close_difference", "recon_adjustment", "min_balance"].includes(column.fieldname)) {
			const v = flt(data[column.fieldname]);
			if (v < -0.5) value = `<span style="color:var(--red-600);font-weight:600">${value}</span>`;
			else if (v > 0.5) value = `<span style="color:var(--green-600)">${value}</span>`;
		}
		if (column.fieldname === "source" && data.source && /No Statement|No lines|never imported/i.test(data.source)) {
			value = `<span style="color:var(--orange-600)">${value}</span>`;
		}
		if (column.fieldname === "flags" && data.flags) {
			value = `<span style="color:var(--red-600)">${value}</span>`;
		}
		if (column.fieldname === "unchecked" && flt(data.unchecked) > 0.5) {
			value = `<span style="color:var(--orange-600)">${value}</span>`;
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
