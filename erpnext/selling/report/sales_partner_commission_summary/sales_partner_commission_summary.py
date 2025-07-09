# Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _, msgprint


def execute(filters=None):
	if not filters:
		filters = {}

	columns = get_columns(filters)
	data = get_entries(filters)

	return columns, data


def get_columns(filters):
	if not filters.get("doctype"):
		msgprint(_("Please select the document type first"), raise_exception=1)

	columns = [
		{
			"label": _(filters["doctype"]),
			"options": filters["doctype"],
			"fieldname": "name",
			"fieldtype": "Link",
			"width": 140,
		},
		{
			"label": _("Customer"),
			"options": "Customer",
			"fieldname": "customer",
			"fieldtype": "Link",
			"width": 140,
		},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Data",
			"width": 80,
		},
		{
			"label": _("Territory"),
			"options": "Territory",
			"fieldname": "territory",
			"fieldtype": "Link",
			"width": 100,
		},
		{"label": _("Posting Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{
			"label": _("Amount"),
			"fieldname": "amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{
			"label": _("Sales Partner"),
			"options": "Sales Partner",
			"fieldname": "sales_partner",
			"fieldtype": "Link",
			"width": 140,
		},
		{
			"label": _("Commission Rate %"),
			"fieldname": "commission_rate",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("Total Commission"),
			"fieldname": "total_commission",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
	]

	return columns


def get_entries(filters):
	date_field = "transaction_date" if filters.get("doctype") == "Sales Order" else "posting_date"

	conditions = get_conditions(filters, date_field)
	entries = frappe.db.sql(
		"""
		SELECT
			doc.name, doc.customer, doc.territory, doc.{} as posting_date, doc.base_net_total as amount,
			doc.sales_partner, doc.commission_rate, doc.total_commission, company.default_currency as currency
		FROM
			`tab{}` as doc
		JOIN
			`tabCompany` as company ON company.name = doc.company
		WHERE
			{} and doc.docstatus = 1 and doc.sales_partner is not null
			and doc.sales_partner != '' order by doc.name desc, doc.sales_partner
		""".format(date_field, filters.get("doctype"), conditions),
		filters,
		as_dict=1,
	)

	return entries


def get_conditions(filters, date_field):
	conditions = "1=1"

	for field in ["company", "customer", "territory"]:
		if filters.get(field):
			conditions += f" and doc.{field} = %({field})s"

	if filters.get("sales_partner"):
		conditions += " and doc.sales_partner = %(sales_partner)s"

	if filters.get("from_date"):
		conditions += f" and doc.{date_field} >= %(from_date)s"

	if filters.get("to_date"):
		conditions += f" and doc.{date_field} <= %(to_date)s"

	return conditions
