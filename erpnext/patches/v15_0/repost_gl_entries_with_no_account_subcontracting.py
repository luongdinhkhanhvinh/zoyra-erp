import frappe


def execute():
	docs = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Subcontracting Receipt", "account": ["is", "not set"], "is_cancelled": 0},
		pluck="voucher_no",
	)
	for doc in docs:
		doc = frappe.get_doc("Subcontracting Receipt", doc)
		for item in doc.supplied_items:
			account, cost_center = frappe.db.get_values(
				"Subcontracting Receipt Item", item.reference_name, ["expense_account", "cost_center"]
			)[0]
			if not item.expense_account:
				item.db_set("expense_account", account)
			if not item.cost_center:
				item.db_set("cost_center", cost_center)
			repost_doc = frappe.new_doc("Repost Item Valuation")
			repost_doc.voucher_type = "Subcontracting Receipt"
			repost_doc.voucher_no = doc.name
			repost_doc.based_on = "Transaction"
			repost_doc.company = doc.company
			repost_doc.save()
			repost_doc.submit()
