# Copyright (c) 2026, Jorovin and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BranchTakingsLine(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		channel: DF.Literal["Equity Bank", "I&M Bank", "NCBA", "Co-op Bank", "Pochi", "Cash", "Other"]
		non_sales_amount: DF.Currency
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		remarks: DF.Data | None
		statement_amount: DF.Currency
	# end: auto-generated types

	pass
