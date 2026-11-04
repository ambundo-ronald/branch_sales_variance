import frappe
from frappe import _
from frappe.model.document import Document


class BranchDailyTakings(Document):
	def validate(self):
		seen = set()
		for row in self.lines:
			if row.channel in seen:
				frappe.throw(_("Row {0}: {1} is entered twice").format(row.idx, row.channel))
			seen.add(row.channel)

		dup = frappe.db.exists(
			"Branch Daily Takings",
			{"pos_profile": self.pos_profile, "posting_date": self.posting_date,
			 "docstatus": 1, "name": ("!=", self.name)},
		)
		if dup:
			frappe.throw(_("Takings for {0} on {1} already exist: {2}").format(
				self.pos_profile, self.posting_date, dup))
