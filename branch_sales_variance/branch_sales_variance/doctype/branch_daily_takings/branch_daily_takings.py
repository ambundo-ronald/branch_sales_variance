# Copyright (c) 2026, Jorovin and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BranchDailyTakings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from branch_sales_variance.branch_sales_variance.doctype.branch_takings_line.branch_takings_line import (
			BranchTakingsLine,
		)

		amended_from: DF.Link | None
		counted_by: DF.Link | None
		lines: DF.Table[BranchTakingsLine]
		naming_series: DF.Literal["BDT-.YYYY.-.#####"]
		pos_profile: DF.Link
		posting_date: DF.Date
		remarks: DF.SmallText | None
	# end: auto-generated types

	def validate(self):
		seen = set()
		for row in self.lines:
			if row.channel in seen:
				frappe.throw(_("Row {0}: {1} is entered twice").format(row.idx, row.channel))
			seen.add(row.channel)

		dup = frappe.db.exists(
			"Branch Daily Takings",
			{
				"pos_profile": self.pos_profile,
				"posting_date": self.posting_date,
				"docstatus": 1,
				"name": ("!=", self.name),
			},
		)
		if dup:
			frappe.throw(
				_("Takings for {0} on {1} already exist: {2}").format(self.pos_profile, self.posting_date, dup)
			)
