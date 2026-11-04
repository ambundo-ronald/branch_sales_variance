// Copyright (c) 2026, Jorovin and contributors
// For license information, please see license.txt

const BDT_CHANNELS = ["Cash", "Pochi", "I&M Bank", "Co-op Bank", "Equity Bank", "NCBA"];

frappe.ui.form.on("Branch Daily Takings", {
	onload(frm) {
		// start every new sheet with one row per channel
		if (frm.is_new() && !(frm.doc.lines || []).length) {
			BDT_CHANNELS.forEach((channel) => frm.add_child("lines", { channel }));
			frm.refresh_field("lines");
		}
	},
	refresh(frm) {
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Open Variance Report"), () => {
				frappe.set_route("query-report", "Branch Sales Variance", {
					pos_profile: frm.doc.pos_profile,
					from_date: frm.doc.posting_date,
					to_date: frm.doc.posting_date,
				});
			});
		}
	},
});
