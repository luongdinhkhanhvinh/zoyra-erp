import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter


def execute():
	if not frappe.db.get_value("Company", {"country": "United Arab Emirates"}):
		return

	make_property_setter("Sales Invoice Item", "is_zero_rated", "fetch_if_empty", 1, "Check")
