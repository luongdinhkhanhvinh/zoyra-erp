# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import json

import frappe
from frappe import _, scrub
from frappe.model.document import Document
from frappe.utils import cint, flt, round_based_on_smallest_currency_fraction

import erpnext
from erpnext.accounts.doctype.journal_entry.journal_entry import get_exchange_rate
from erpnext.accounts.doctype.pricing_rule.utils import get_applied_pricing_rules
from erpnext.controllers.accounts_controller import (
	validate_conversion_rate,
	validate_inclusive_tax,
	validate_taxes_and_charges,
)
from erpnext.deprecation_dumpster import deprecated
from erpnext.stock.get_item_details import ItemDetailsCtx, _get_item_tax_template, get_item_tax_map
from erpnext.utilities.regional import temporary_flag

ItemWiseTaxDetail = frappe._dict


class calculate_taxes_and_totals:
	def __init__(self, doc: Document):
		self.doc = doc
		frappe.flags.round_off_applicable_accounts = []
		frappe.flags.round_row_wise_tax = frappe.get_single_value("Accounts Settings", "round_row_wise_tax")

		if doc.get("round_off_applicable_accounts_for_tax_withholding"):
			frappe.flags.round_off_applicable_accounts.append(
				doc.round_off_applicable_accounts_for_tax_withholding
			)

		self._items = self.filter_rows() if self.doc.doctype == "Quotation" else self.doc.get("items")

		get_round_off_applicable_accounts(self.doc.company, frappe.flags.round_off_applicable_accounts)
		self.calculate()

	def filter_rows(self):
		"""Exclude rows, that do not fulfill the filter criteria, from totals computation."""
		items = list(filter(lambda item: not item.get("is_alternative"), self.doc.get("items")))
		return items

	def calculate(self):
		if not len(self.doc.items):
			return

		self.discount_amount_applied = False
		self._calculate()

		if self.doc.meta.get_field("discount_amount"):
			self.set_discount_amount()
			self.apply_discount_amount()

		# Update grand total as per cash and non trade discount
		if self.doc.apply_discount_on == "Grand Total" and self.doc.get("is_cash_or_non_trade_discount"):
			self.doc.grand_total -= self.doc.discount_amount
			self.doc.base_grand_total -= self.doc.base_discount_amount
			self.doc.rounding_adjustment = self.doc.base_rounding_adjustment = 0.0
			self.set_rounded_total()

		self.calculate_shipping_charges()

		if self.doc.doctype in ["Sales Invoice", "Purchase Invoice"]:
			self.calculate_total_advance()

		if self.doc.meta.get_field("other_charges_calculation"):
			self.set_item_wise_tax_breakup()

	def _calculate(self):
		self.validate_conversion_rate()
		self.calculate_item_values()
		self.validate_item_tax_template()
		self.update_item_tax_map()
		self.initialize_taxes()
		self.determine_exclusive_rate()
		self.calculate_net_total()
		self.calculate_tax_withholding_net_total()
		self.calculate_taxes()
		self.adjust_grand_total_for_inclusive_tax()
		self.calculate_totals()
		self._cleanup()
		self.calculate_total_net_weight()

	def calculate_tax_withholding_net_total(self):
		if hasattr(self.doc, "tax_withholding_net_total"):
			sum_net_amount = 0
			sum_base_net_amount = 0
			for item in self._items:
				if hasattr(item, "apply_tds") and item.apply_tds:
					sum_net_amount += item.net_amount
					sum_base_net_amount += item.base_net_amount

			self.doc.tax_withholding_net_total = sum_net_amount
			self.doc.base_tax_withholding_net_total = sum_base_net_amount

	def validate_item_tax_template(self):
		if self.doc.get("is_return") and self.doc.get("return_against"):
			return

		for item in self.doc.items:
			if item.item_code and item.get("item_tax_template"):
				item_doc = frappe.get_cached_doc("Item", item.item_code)
				ctx = ItemDetailsCtx(
					{
						"net_rate": item.net_rate or item.rate,
						"base_net_rate": item.base_net_rate or item.base_rate,
						"tax_category": self.doc.get("tax_category"),
						"posting_date": self.doc.get("posting_date"),
						"bill_date": self.doc.get("bill_date"),
						"transaction_date": self.doc.get("transaction_date"),
						"company": self.doc.get("company"),
					}
				)

				item_group = item_doc.item_group
				item_group_taxes = []

				while item_group:
					item_group_doc = frappe.get_cached_doc("Item Group", item_group)
					item_group_taxes += item_group_doc.taxes or []
					item_group = item_group_doc.parent_item_group

				item_taxes = item_doc.taxes or []

				if not item_group_taxes and (not item_taxes):
					# No validation if no taxes in item or item group
					continue

				taxes = _get_item_tax_template(ctx, item_taxes + item_group_taxes, for_validate=True)

				if taxes:
					if item.item_tax_template not in taxes:
						item.item_tax_template = taxes[0]
						frappe.msgprint(
							_("Row {0}: Item Tax template updated as per validity and rate applied").format(
								item.idx, frappe.bold(item.item_code)
							)
						)

	def update_item_tax_map(self):
		for item in self.doc.items:
			item.item_tax_rate = get_item_tax_map(
				doc=self.doc,
				tax_template=item.item_tax_template,
				as_json=True,
			)

	def validate_conversion_rate(self):
		# validate conversion rate
		company_currency = erpnext.get_company_currency(self.doc.company)
		if not self.doc.currency or self.doc.currency == company_currency:
			self.doc.currency = company_currency
			self.doc.conversion_rate = 1.0
		else:
			validate_conversion_rate(
				self.doc.currency,
				self.doc.conversion_rate,
				self.doc.meta.get_label("conversion_rate"),
				self.doc.company,
			)

		self.doc.conversion_rate = flt(self.doc.conversion_rate)

	def calculate_item_values(self):
		if self.doc.get("is_consolidated"):
			return

		if not self.discount_amount_applied:
			for item in self.doc.items:
				self.doc.round_floats_in(item)

				if item.discount_percentage == 100:
					item.rate = 0.0
				elif item.price_list_rate:
					if not item.rate or (item.pricing_rules and item.discount_percentage > 0):
						item.rate = flt(
							item.price_list_rate * (1.0 - (item.discount_percentage / 100.0)),
							item.precision("rate"),
						)

						item.discount_amount = item.price_list_rate * (item.discount_percentage / 100.0)

					elif item.discount_amount and item.pricing_rules:
						item.rate = item.price_list_rate - item.discount_amount

				if item.doctype in [
					"Quotation Item",
					"Sales Order Item",
					"Delivery Note Item",
					"Sales Invoice Item",
					"POS Invoice Item",
					"Purchase Invoice Item",
					"Purchase Order Item",
					"Purchase Receipt Item",
				]:
					item.rate_with_margin, item.base_rate_with_margin = self.calculate_margin(item)
					if flt(item.rate_with_margin) > 0:
						item.rate = flt(
							item.rate_with_margin * (1.0 - (item.discount_percentage / 100.0)),
							item.precision("rate"),
						)

						if item.discount_amount and not item.discount_percentage:
							item.rate = item.rate_with_margin - item.discount_amount
						else:
							item.discount_amount = item.rate_with_margin - item.rate

					elif flt(item.price_list_rate) > 0:
						item.discount_amount = item.price_list_rate - item.rate
				elif flt(item.price_list_rate) > 0 and not item.discount_amount:
					item.discount_amount = item.price_list_rate - item.rate

				item.net_rate = item.rate

				if (
					not item.qty
					and self.doc.get("is_return")
					and self.doc.get("doctype") != "Purchase Receipt"
				):
					item.amount = flt(-1 * item.rate, item.precision("amount"))
				elif not item.qty and self.doc.get("is_debit_note"):
					item.amount = flt(item.rate, item.precision("amount"))
				else:
					item.amount = flt(item.rate * item.qty, item.precision("amount"))

				item.net_amount = item.amount

				self._set_in_company_currency(
					item, ["price_list_rate", "rate", "net_rate", "amount", "net_amount"]
				)

				item.item_tax_amount = 0.0

	def _set_in_company_currency(self, doc, fields):
		"""set values in base currency"""
		for f in fields:
			val = flt(
				flt(doc.get(f), doc.precision(f)) * self.doc.conversion_rate, doc.precision("base_" + f)
			)
			doc.set("base_" + f, val)

	def initialize_taxes(self):
		for tax in self.doc.get("taxes"):
			if not self.discount_amount_applied:
				validate_taxes_and_charges(tax)
				validate_inclusive_tax(tax, self.doc)

			if not (self.doc.get("is_consolidated") or tax.get("dont_recompute_tax")):
				tax.item_wise_tax_detail = {}

			tax_fields = [
				"net_amount",
				"total",
				"tax_amount_after_discount_amount",
				"tax_amount_for_current_item",
				"grand_total_for_current_item",
				"tax_fraction_for_current_item",
				"grand_total_fraction_for_current_item",
			]
			if tax.charge_type != "Actual" and not (
				self.discount_amount_applied and self.doc.apply_discount_on == "Grand Total"
			):
				tax_fields.append("tax_amount")

			for fieldname in tax_fields:
				tax.set(fieldname, 0.0)

			self.doc.round_floats_in(tax)

	def determine_exclusive_rate(self):
		if not any(cint(tax.included_in_print_rate) for tax in self.doc.get("taxes")):
			return

		for item in self.doc.items:
			item_tax_map = self._load_item_tax_rate(item.item_tax_rate)
			cumulated_tax_fraction = 0
			total_inclusive_tax_amount_per_qty = 0
			for i, tax in enumerate(self.doc.get("taxes")):
				(
					tax.tax_fraction_for_current_item,
					inclusive_tax_amount_per_qty,
				) = self.get_current_tax_fraction(tax, item_tax_map)

				if i == 0:
					tax.grand_total_fraction_for_current_item = 1 + tax.tax_fraction_for_current_item
				else:
					tax.grand_total_fraction_for_current_item = (
						self.doc.get("taxes")[i - 1].grand_total_fraction_for_current_item
						+ tax.tax_fraction_for_current_item
					)

				cumulated_tax_fraction += tax.tax_fraction_for_current_item
				total_inclusive_tax_amount_per_qty += inclusive_tax_amount_per_qty * flt(item.qty)

			if (
				not self.discount_amount_applied
				and item.qty
				and (cumulated_tax_fraction or total_inclusive_tax_amount_per_qty)
			):
				amount = flt(item.amount) - total_inclusive_tax_amount_per_qty

				item.net_amount = flt(amount / (1 + cumulated_tax_fraction), item.precision("net_amount"))
				item.net_rate = flt(item.net_amount / item.qty, item.precision("net_rate"))
				item.discount_percentage = flt(
					item.discount_percentage, item.precision("discount_percentage")
				)

				self._set_in_company_currency(item, ["net_rate", "net_amount"])

	def _load_item_tax_rate(self, item_tax_rate):
		return json.loads(item_tax_rate) if item_tax_rate else {}

	def get_current_tax_fraction(self, tax, item_tax_map):
		"""
		Get tax fraction for calculating tax exclusive amount
		from tax inclusive amount
		"""
		current_tax_fraction = 0
		inclusive_tax_amount_per_qty = 0

		if cint(tax.included_in_print_rate):
			tax_rate = self._get_tax_rate(tax, item_tax_map)

			if tax.charge_type == "On Net Total":
				current_tax_fraction = tax_rate / 100.0

			elif tax.charge_type == "On Previous Row Amount":
				current_tax_fraction = (tax_rate / 100.0) * self.doc.get("taxes")[
					cint(tax.row_id) - 1
				].tax_fraction_for_current_item

			elif tax.charge_type == "On Previous Row Total":
				current_tax_fraction = (tax_rate / 100.0) * self.doc.get("taxes")[
					cint(tax.row_id) - 1
				].grand_total_fraction_for_current_item

			elif tax.charge_type == "On Item Quantity":
				inclusive_tax_amount_per_qty = flt(tax_rate)

		if getattr(tax, "add_deduct_tax", None) and tax.add_deduct_tax == "Deduct":
			current_tax_fraction *= -1.0
			inclusive_tax_amount_per_qty *= -1.0

		return current_tax_fraction, inclusive_tax_amount_per_qty

	def _get_tax_rate(self, tax, item_tax_map):
		if tax.account_head in item_tax_map:
			return flt(item_tax_map.get(tax.account_head), self.doc.precision("rate", tax))
		else:
			return tax.rate

	def calculate_net_total(self):
		self.doc.total_qty = (
			self.doc.total
		) = self.doc.base_total = self.doc.net_total = self.doc.base_net_total = 0.0

		for item in self._items:
			self.doc.total += item.amount
			self.doc.total_qty += item.qty
			self.doc.base_total += item.base_amount
			self.doc.net_total += item.net_amount
			self.doc.base_net_total += item.base_net_amount

		self.doc.round_floats_in(self.doc, ["total", "base_total", "net_total", "base_net_total"])

	def calculate_shipping_charges(self):
		# Do not apply shipping rule for POS
		if self.doc.get("is_pos"):
			return

		if hasattr(self.doc, "shipping_rule") and self.doc.shipping_rule:
			shipping_rule = frappe.get_doc("Shipping Rule", self.doc.shipping_rule)
			shipping_rule.apply(self.doc)

			self._calculate()

	def calculate_taxes(self):
		doc = self.doc
		if not doc.get("taxes"):
			return

		# maintain actual tax rate based on idx
		actual_tax_dict = dict(
			[
				[tax.idx, flt(tax.tax_amount, tax.precision("tax_amount"))]
				for tax in doc.taxes
				if tax.charge_type == "Actual"
			]
		)

		for n, item in enumerate(self._items):
			item_tax_map = self._load_item_tax_rate(item.item_tax_rate)
			for i, tax in enumerate(doc.taxes):
				# tax_amount represents the amount of tax for the current step
				current_net_amount, current_tax_amount = self.get_current_tax_and_net_amount(
					item, tax, item_tax_map
				)
				if frappe.flags.round_row_wise_tax:
					current_tax_amount = flt(current_tax_amount, tax.precision("tax_amount"))
					current_net_amount = flt(current_net_amount, tax.precision("net_amount"))

				# Adjust divisional loss to the last item
				if tax.charge_type == "Actual":
					actual_tax_dict[tax.idx] -= current_tax_amount
					if n == len(self._items) - 1:
						current_tax_amount += actual_tax_dict[tax.idx]

				# accumulate tax amount into tax.tax_amount
				if tax.charge_type != "Actual" and not (
					self.discount_amount_applied and self.doc.apply_discount_on == "Grand Total"
				):
					tax.tax_amount += current_tax_amount
					tax.net_amount += current_net_amount

				# store tax_amount for current item as it will be used for
				# charge type = 'On Previous Row Amount'
				tax.tax_amount_for_current_item = current_tax_amount

				# set tax after discount
				tax.tax_amount_after_discount_amount += current_tax_amount

				current_tax_amount = self.get_tax_amount_if_for_valuation_or_deduction(
					current_tax_amount, tax
				)

				# note: grand_total_for_current_item contains the contribution of
				# item's amount, previously applied tax and the current tax on that item
				if i == 0:
					tax.grand_total_for_current_item = flt(item.net_amount + current_tax_amount)
				else:
					tax.grand_total_for_current_item = flt(
						doc.taxes[i - 1].grand_total_for_current_item + current_tax_amount
					)

		discount_amount_applied = self.discount_amount_applied
		if doc.apply_discount_on == "Grand Total" and (
			discount_amount_applied or doc.discount_amount or doc.additional_discount_percentage
		):
			tax_amount_precision = doc.taxes[0].precision("tax_amount")

			for i, tax in enumerate(doc.taxes):
				if discount_amount_applied:
					tax.tax_amount_after_discount_amount = flt(
						tax.tax_amount_after_discount_amount, tax_amount_precision
					)

				self.set_cumulative_total(i, tax)

			if not discount_amount_applied:
				self.grand_total_for_distributing_discount = doc.taxes[-1].total
			else:
				self.grand_total_diff = flt(
					self.grand_total_for_distributing_discount - doc.discount_amount - doc.taxes[-1].total,
					doc.precision("grand_total"),
				)

		for i, tax in enumerate(doc.taxes):
			self.round_off_totals(tax)
			self._set_in_company_currency(
				tax, ["tax_amount", "tax_amount_after_discount_amount", "net_amount"]
			)

			self.round_off_base_values(tax)
			self.set_cumulative_total(i, tax)

			self._set_in_company_currency(tax, ["total"])

	def get_tax_amount_if_for_valuation_or_deduction(self, tax_amount, tax):
		# if just for valuation, do not add the tax amount in total
		# if tax/charges is for deduction, multiply by -1
		if getattr(tax, "category", None):
			tax_amount = 0.0 if (tax.category == "Valuation") else tax_amount
			if self.doc.doctype in [
				"Purchase Order",
				"Purchase Invoice",
				"Purchase Receipt",
				"Supplier Quotation",
			]:
				tax_amount *= -1.0 if (tax.add_deduct_tax == "Deduct") else 1.0
		return tax_amount

	def set_cumulative_total(self, row_idx, tax):
		tax_amount = tax.tax_amount_after_discount_amount
		tax_amount = self.get_tax_amount_if_for_valuation_or_deduction(tax_amount, tax)

		if row_idx == 0:
			tax.total = flt(self.doc.net_total + tax_amount, tax.precision("total"))
		else:
			tax.total = flt(self.doc.get("taxes")[row_idx - 1].total + tax_amount, tax.precision("total"))

	def get_current_tax_and_net_amount(self, item, tax, item_tax_map):
		tax_rate = self._get_tax_rate(tax, item_tax_map)
		current_tax_amount = 0.0
		current_net_amount = 0.0

		if tax.charge_type == "Actual":
			current_net_amount = item.net_amount
			# distribute the tax amount proportionally to each item row
			actual = flt(tax.tax_amount, tax.precision("tax_amount"))

			if tax.get("is_tax_withholding_account") and item.meta.get_field("apply_tds"):
				if not item.get("apply_tds") or not self.doc.tax_withholding_net_total:
					current_tax_amount = 0.0
				else:
					current_tax_amount = item.net_amount * actual / self.doc.tax_withholding_net_total
			else:
				current_tax_amount = (
					item.net_amount * actual / self.doc.net_total if self.doc.net_total else 0.0
				)

		elif tax.charge_type == "On Net Total":
			if tax.account_head in item_tax_map:
				current_net_amount = item.net_amount
			current_tax_amount = (tax_rate / 100.0) * item.net_amount
		elif tax.charge_type == "On Previous Row Amount":
			current_net_amount = self.doc.get("taxes")[cint(tax.row_id) - 1].tax_amount_for_current_item
			current_tax_amount = (tax_rate / 100.0) * current_net_amount
		elif tax.charge_type == "On Previous Row Total":
			current_net_amount = self.doc.get("taxes")[cint(tax.row_id) - 1].grand_total_for_current_item
			current_tax_amount = (tax_rate / 100.0) * current_net_amount
		elif tax.charge_type == "On Item Quantity":
			# don't sum current net amount due to the field being a currency field
			current_tax_amount = tax_rate * item.qty

		if not (self.doc.get("is_consolidated") or tax.get("dont_recompute_tax")):
			self.set_item_wise_tax(item, tax, tax_rate, current_tax_amount, current_net_amount)

		return current_net_amount, current_tax_amount

	def set_item_wise_tax(self, item, tax, tax_rate, current_tax_amount, current_net_amount):
		# store tax breakup for each item
		key = item.item_code or item.item_name
		item_wise_tax_amount = current_tax_amount * self.doc.conversion_rate
		if tax.charge_type != "On Item Quantity":
			item_wise_net_amount = current_net_amount * self.doc.conversion_rate
		else:
			item_wise_net_amount = 0.0
		if frappe.flags.round_row_wise_tax:
			item_wise_tax_amount = flt(item_wise_tax_amount, tax.precision("tax_amount"))
			item_wise_net_amount = flt(item_wise_net_amount, tax.precision("net_amount"))
			if tax_data := tax.item_wise_tax_detail.get(key):
				item_wise_tax_amount += flt(tax_data.tax_amount, tax.precision("tax_amount"))
				item_wise_net_amount += flt(tax_data.net_amount, tax.precision("net_amount"))
			else:
				tax.item_wise_tax_detail[key] = ItemWiseTaxDetail(
					tax_rate=tax_rate,
					tax_amount=flt(item_wise_tax_amount, tax.precision("tax_amount")),
					net_amount=flt(item_wise_net_amount, tax.precision("net_amount")),
				)
		else:
			if tax_data := tax.item_wise_tax_detail.get(key):
				item_wise_tax_amount += tax_data.tax_amount
				item_wise_net_amount += tax_data.net_amount

			tax.item_wise_tax_detail[key] = ItemWiseTaxDetail(
				tax_rate=tax_rate,
				tax_amount=item_wise_tax_amount,
				net_amount=item_wise_net_amount,
			)

	def round_off_totals(self, tax):
		if tax.account_head in frappe.flags.round_off_applicable_accounts:
			tax.tax_amount = round(tax.tax_amount, 0)
			tax.tax_amount_after_discount_amount = round(tax.tax_amount_after_discount_amount, 0)

		tax.tax_amount = flt(tax.tax_amount, tax.precision("tax_amount"))
		tax.net_amount = flt(tax.net_amount, tax.precision("net_amount"))
		tax.tax_amount_after_discount_amount = flt(
			tax.tax_amount_after_discount_amount, tax.precision("tax_amount")
		)

	def round_off_base_values(self, tax):
		# Round off to nearest integer based on regional settings
		if tax.account_head in frappe.flags.round_off_applicable_accounts:
			tax.base_tax_amount = round(tax.base_tax_amount, 0)
			tax.base_tax_amount_after_discount_amount = round(tax.base_tax_amount_after_discount_amount, 0)

	@deprecated(
		f"{__name__}.calculate_taxes_and_totals.manipulate_grand_total_for_inclusive_tax",
		"unknown",
		"v16",
		"No known instructions.",
	)
	def manipulate_grand_total_for_inclusive_tax(self):
		# for backward compatablility - if in case used by an external application
		return self.adjust_grand_total_for_inclusive_tax()

	def adjust_grand_total_for_inclusive_tax(self):
		# if any inclusive taxes and diff
		if self.doc.get("taxes") and any(cint(t.included_in_print_rate) for t in self.doc.get("taxes")):
			last_tax = self.doc.get("taxes")[-1]
			non_inclusive_tax_amount = sum(
				self.get_tax_amount_if_for_valuation_or_deduction(d.tax_amount_after_discount_amount, d)
				for d in self.doc.get("taxes")
				if not d.included_in_print_rate
			)

			diff = (
				self.doc.total + non_inclusive_tax_amount - flt(last_tax.total, last_tax.precision("total"))
			)

			# If discount amount applied, deduct the discount amount
			# because self.doc.total is always without discount, but last_tax.total is after discount
			if self.discount_amount_applied and self.doc.discount_amount:
				diff -= flt(self.doc.discount_amount)

			diff = flt(diff, self.doc.precision("rounding_adjustment"))

			if diff and abs(diff) <= (5.0 / 10 ** last_tax.precision("tax_amount")):
				self.grand_total_diff = diff
			else:
				self.grand_total_diff = 0

	def calculate_totals(self):
		grand_total_diff = getattr(self, "grand_total_diff", 0)

		if self.doc.get("taxes"):
			self.doc.grand_total = flt(self.doc.get("taxes")[-1].total) + grand_total_diff
		else:
			self.doc.grand_total = flt(self.doc.net_total)

		if self.doc.get("taxes"):
			self.doc.total_taxes_and_charges = flt(
				self.doc.grand_total - self.doc.net_total - grand_total_diff,
				self.doc.precision("total_taxes_and_charges"),
			)
		else:
			self.doc.total_taxes_and_charges = 0.0

		self._set_in_company_currency(self.doc, ["total_taxes_and_charges"])

		if self.doc.doctype in [
			"Quotation",
			"Sales Order",
			"Delivery Note",
			"Sales Invoice",
			"POS Invoice",
		]:
			self.doc.base_grand_total = (
				flt(self.doc.grand_total * self.doc.conversion_rate, self.doc.precision("base_grand_total"))
				if self.doc.total_taxes_and_charges
				else self.doc.base_net_total
			)
		else:
			self.doc.taxes_and_charges_added = self.doc.taxes_and_charges_deducted = 0.0
			for tax in self.doc.get("taxes"):
				if tax.category in ["Valuation and Total", "Total"]:
					if tax.add_deduct_tax == "Add":
						self.doc.taxes_and_charges_added += flt(tax.tax_amount_after_discount_amount)
					else:
						self.doc.taxes_and_charges_deducted += flt(tax.tax_amount_after_discount_amount)

			self.doc.round_floats_in(self.doc, ["taxes_and_charges_added", "taxes_and_charges_deducted"])

			self.doc.base_grand_total = (
				flt(self.doc.grand_total * self.doc.conversion_rate)
				if (self.doc.taxes_and_charges_added or self.doc.taxes_and_charges_deducted)
				else self.doc.base_net_total
			)

			self._set_in_company_currency(self.doc, ["taxes_and_charges_added", "taxes_and_charges_deducted"])

		self.doc.round_floats_in(self.doc, ["grand_total", "base_grand_total"])

		self.set_rounded_total()

	def calculate_total_net_weight(self):
		if self.doc.meta.get_field("total_net_weight"):
			self.doc.total_net_weight = 0.0
			for d in self._items:
				if d.total_weight:
					self.doc.total_net_weight += d.total_weight

	def set_rounded_total(self):
		if self.doc.get("is_consolidated") and self.doc.get("rounding_adjustment"):
			return

		if self.doc.meta.get_field("rounded_total"):
			if self.doc.is_rounded_total_disabled():
				self.doc.rounded_total = 0
				self.doc.base_rounded_total = 0
				self.doc.rounding_adjustment = 0
				return

			self.doc.rounded_total = round_based_on_smallest_currency_fraction(
				self.doc.grand_total, self.doc.currency, self.doc.precision("rounded_total")
			)

			# rounding adjustment should always be the difference vetween grand and rounded total
			self.doc.rounding_adjustment = flt(
				self.doc.rounded_total - self.doc.grand_total, self.doc.precision("rounding_adjustment")
			)

			self._set_in_company_currency(self.doc, ["rounding_adjustment", "rounded_total"])

	def _cleanup(self):
		if not self.doc.get("is_consolidated"):
			for tax in self.doc.get("taxes"):
				if not tax.get("dont_recompute_tax"):
					tax.item_wise_tax_detail = json.dumps(tax.item_wise_tax_detail)

	def set_discount_amount(self):
		if self.doc.additional_discount_percentage:
			self.doc.discount_amount = flt(
				flt(self.doc.get(scrub(self.doc.apply_discount_on)))
				* self.doc.additional_discount_percentage
				/ 100,
				self.doc.precision("discount_amount"),
			)

	def apply_discount_amount(self):
		if self.doc.discount_amount:
			if not self.doc.apply_discount_on:
				frappe.throw(_("Please select Apply Discount On"))

			self.doc.base_discount_amount = flt(
				self.doc.discount_amount * self.doc.conversion_rate,
				self.doc.precision("base_discount_amount"),
			)

			if self.doc.apply_discount_on == "Grand Total" and self.doc.get("is_cash_or_non_trade_discount"):
				self.discount_amount_applied = True
				return

			total_for_discount_amount = self.get_total_for_discount_amount()
			net_total = 0
			expected_net_total = 0

			if total_for_discount_amount:
				# calculate item amount after Discount Amount
				for item in self._items:
					distributed_amount = (
						flt(self.doc.discount_amount) * item.net_amount / total_for_discount_amount
					)

					adjusted_net_amount = item.net_amount - distributed_amount
					expected_net_total += adjusted_net_amount
					item.net_amount = flt(adjusted_net_amount, item.precision("net_amount"))
					item.distributed_discount_amount = flt(
						distributed_amount, item.precision("distributed_discount_amount")
					)
					net_total += item.net_amount

					# discount amount rounding adjustment
					if rounding_difference := flt(
						expected_net_total - net_total, self.doc.precision("net_total")
					):
						item.net_amount = flt(
							item.net_amount + rounding_difference, item.precision("net_amount")
						)
						item.distributed_discount_amount = flt(
							distributed_amount + rounding_difference,
							item.precision("distributed_discount_amount"),
						)
						net_total += rounding_difference

					item.net_rate = (
						flt(item.net_amount / item.qty, item.precision("net_rate")) if item.qty else 0
					)

					self._set_in_company_currency(item, ["net_rate", "net_amount"])

				self.discount_amount_applied = True
				self._calculate()
		else:
			self.doc.base_discount_amount = 0

	def get_total_for_discount_amount(self):
		doc = self.doc
		if doc.apply_discount_on == "Net Total" or not doc.get("taxes"):
			return self.doc.net_total

		total_actual_tax = 0
		actual_taxes_dict = {}

		def update_actual_tax_dict(tax, tax_amount):
			nonlocal total_actual_tax

			if tax.get("add_deduct_tax") == "Deduct":
				tax_amount *= -1

			if tax.get("category") != "Valuation":
				total_actual_tax += tax_amount

			actual_taxes_dict[int(tax.idx)] = {
				"tax_amount": tax_amount,
				"cumulative_tax_amount": total_actual_tax,
			}

		for tax in doc.taxes:
			if tax.charge_type in ["Actual", "On Item Quantity"]:
				update_actual_tax_dict(tax, tax.tax_amount)
				continue

			if not tax.row_id:
				continue

			base_row = actual_taxes_dict.get(int(tax.row_id))
			if not base_row:
				continue

			base_tax_amount = (
				base_row["tax_amount"]
				if tax.charge_type == "On Previous Row Amount"
				else base_row["cumulative_tax_amount"]
			)
			update_actual_tax_dict(tax, base_tax_amount * tax.rate / 100)

		return getattr(self, "grand_total_for_distributing_discount", doc.grand_total) - total_actual_tax

	def calculate_total_advance(self):
		if not self.doc.docstatus.is_cancelled():
			total_allocated_amount = sum(
				flt(adv.allocated_amount, adv.precision("allocated_amount"))
				for adv in self.doc.get("advances")
			)

			self.doc.total_advance = flt(total_allocated_amount, self.doc.precision("total_advance"))

			grand_total = self.doc.rounded_total or self.doc.grand_total

			if self.doc.party_account_currency == self.doc.currency:
				invoice_total = flt(
					grand_total - flt(self.doc.write_off_amount), self.doc.precision("grand_total")
				)
			else:
				base_write_off_amount = flt(
					flt(self.doc.write_off_amount) * self.doc.conversion_rate,
					self.doc.precision("base_write_off_amount"),
				)
				invoice_total = (
					flt(grand_total * self.doc.conversion_rate, self.doc.precision("grand_total"))
					- base_write_off_amount
				)

			if invoice_total > 0 and self.doc.total_advance > invoice_total:
				frappe.throw(
					_("Advance amount cannot be greater than {0} {1}").format(
						self.doc.party_account_currency, invoice_total
					)
				)

			if self.doc.docstatus.is_draft():
				if self.doc.get("write_off_outstanding_amount_automatically"):
					self.doc.write_off_amount = 0

				self.calculate_outstanding_amount()
				self.calculate_write_off_amount()

	def is_internal_invoice(self):
		"""
		Checks if its an internal transfer invoice
		and decides if to calculate any out standing amount or not
		"""

		if self.doc.doctype in ("Sales Invoice", "Purchase Invoice") and self.doc.is_internal_transfer():
			return True

		return False

	def calculate_outstanding_amount(self):
		# NOTE:
		# write_off_amount is only for POS Invoice
		# total_advance is only for non POS Invoice
		if self.doc.doctype == "Sales Invoice":
			self.calculate_paid_amount()

		if (
			self.doc.is_return
			and self.doc.return_against
			and not self.doc.update_outstanding_for_self
			and not self.doc.get("is_pos")
			or self.is_internal_invoice()
		):
			# Do not calculate the outstanding amount for a return invoice if 'update_outstanding_for_self' is not enabled.
			self.doc.outstanding_amount = 0
			return

		self.doc.round_floats_in(self.doc, ["grand_total", "total_advance", "write_off_amount"])
		self._set_in_company_currency(self.doc, ["write_off_amount"])

		if self.doc.doctype in ["Sales Invoice", "Purchase Invoice"]:
			grand_total = self.doc.rounded_total or self.doc.grand_total
			base_grand_total = self.doc.base_rounded_total or self.doc.base_grand_total

			if self.doc.party_account_currency == self.doc.currency:
				total_amount_to_pay = flt(
					grand_total - self.doc.total_advance - flt(self.doc.write_off_amount),
					self.doc.precision("grand_total"),
				)
			else:
				total_amount_to_pay = flt(
					flt(base_grand_total, self.doc.precision("base_grand_total"))
					- self.doc.total_advance
					- flt(self.doc.base_write_off_amount),
					self.doc.precision("base_grand_total"),
				)

			self.doc.round_floats_in(self.doc, ["paid_amount"])
			change_amount = 0

			if self.doc.doctype == "Sales Invoice" and not self.doc.get("is_return"):
				self.calculate_change_amount()
				change_amount = (
					self.doc.change_amount
					if self.doc.party_account_currency == self.doc.currency
					else self.doc.base_change_amount
				)

			paid_amount = (
				self.doc.paid_amount
				if self.doc.party_account_currency == self.doc.currency
				else self.doc.base_paid_amount
			)

			self.doc.outstanding_amount = flt(
				total_amount_to_pay - flt(paid_amount) + flt(change_amount),
				self.doc.precision("outstanding_amount"),
			)

			if (
				self.doc.doctype == "Sales Invoice"
				and self.doc.get("is_pos")
				and self.doc.get("pos_profile")
				and self.doc.get("is_consolidated")
			):
				write_off_limit = flt(
					frappe.db.get_value("POS Profile", self.doc.pos_profile, "write_off_limit")
				)
				if write_off_limit and abs(self.doc.outstanding_amount) <= write_off_limit:
					self.doc.write_off_outstanding_amount_automatically = 1

			if (
				self.doc.doctype == "Sales Invoice"
				and self.doc.get("is_pos")
				and self.doc.get("is_return")
				and not self.doc.get("is_consolidated")
			):
				self.set_total_amount_to_default_mop(total_amount_to_pay)
				self.calculate_paid_amount()

	def calculate_paid_amount(self):
		paid_amount = base_paid_amount = 0.0

		if self.doc.is_pos:
			for payment in self.doc.get("payments"):
				payment.amount = flt(payment.amount)
				payment.base_amount = payment.amount * flt(self.doc.conversion_rate)
				paid_amount += payment.amount
				base_paid_amount += payment.base_amount
		elif not self.doc.is_return:
			self.doc.set("payments", [])

		if self.doc.redeem_loyalty_points and self.doc.loyalty_amount:
			base_paid_amount += self.doc.loyalty_amount
			paid_amount += self.doc.loyalty_amount / flt(self.doc.conversion_rate)

		self.doc.paid_amount = flt(paid_amount, self.doc.precision("paid_amount"))
		self.doc.base_paid_amount = flt(base_paid_amount, self.doc.precision("base_paid_amount"))

	def calculate_change_amount(self):
		self.doc.change_amount = 0.0
		self.doc.base_change_amount = 0.0
		grand_total = self.doc.rounded_total or self.doc.grand_total
		base_grand_total = self.doc.base_rounded_total or self.doc.base_grand_total

		if (
			self.doc.doctype == "Sales Invoice"
			and self.doc.paid_amount > grand_total
			and not self.doc.is_return
			and any(d.type == "Cash" for d in self.doc.payments)
		):
			self.doc.change_amount = flt(
				self.doc.paid_amount - grand_total, self.doc.precision("change_amount")
			)

			self.doc.base_change_amount = flt(
				self.doc.base_paid_amount - base_grand_total, self.doc.precision("base_change_amount")
			)

	def calculate_write_off_amount(self):
		if self.doc.get("write_off_outstanding_amount_automatically"):
			self.doc.write_off_amount = flt(
				self.doc.outstanding_amount, self.doc.precision("write_off_amount")
			)
			self.doc.base_write_off_amount = flt(
				self.doc.write_off_amount * self.doc.conversion_rate,
				self.doc.precision("base_write_off_amount"),
			)

			self.calculate_outstanding_amount()

	def calculate_margin(self, item):
		rate_with_margin = 0.0
		base_rate_with_margin = 0.0
		if item.price_list_rate:
			if item.pricing_rules and not self.doc.ignore_pricing_rule:
				has_margin = False
				for d in get_applied_pricing_rules(item.pricing_rules):
					pricing_rule = frappe.get_cached_doc("Pricing Rule", d)

					if pricing_rule.margin_rate_or_amount and (
						(
							pricing_rule.currency == self.doc.currency
							and pricing_rule.margin_type in ["Amount", "Percentage"]
						)
						or pricing_rule.margin_type == "Percentage"
					):
						item.margin_type = pricing_rule.margin_type
						item.margin_rate_or_amount = pricing_rule.margin_rate_or_amount
						has_margin = True

				if not has_margin:
					item.margin_type = None
					item.margin_rate_or_amount = 0.0

			if not item.pricing_rules and flt(item.rate) > flt(item.price_list_rate):
				item.margin_type = "Amount"
				item.margin_rate_or_amount = flt(
					item.rate - item.price_list_rate, item.precision("margin_rate_or_amount")
				)
				item.rate_with_margin = item.rate

			elif item.margin_type and item.margin_rate_or_amount:
				margin_value = (
					item.margin_rate_or_amount
					if item.margin_type == "Amount"
					else flt(item.price_list_rate) * flt(item.margin_rate_or_amount) / 100
				)
				rate_with_margin = flt(item.price_list_rate) + flt(margin_value)
				base_rate_with_margin = flt(rate_with_margin) * flt(self.doc.conversion_rate)

		return rate_with_margin, base_rate_with_margin

	def set_item_wise_tax_breakup(self):
		self.doc.other_charges_calculation = get_itemised_tax_breakup_html(self.doc)

	def set_total_amount_to_default_mop(self, total_amount_to_pay):
		total_paid_amount = 0
		for payment in self.doc.get("payments"):
			total_paid_amount += (
				payment.amount
				if self.doc.party_account_currency == self.doc.currency
				else payment.base_amount
			)

		pending_amount = total_amount_to_pay - total_paid_amount

		if pending_amount > 0:
			default_mode_of_payment = frappe.db.get_value(
				"POS Payment Method",
				{"parent": self.doc.pos_profile, "default": 1},
				["mode_of_payment"],
				as_dict=1,
			)

			if default_mode_of_payment:
				self.doc.payments = []
				self.doc.append(
					"payments",
					{
						"mode_of_payment": default_mode_of_payment.mode_of_payment,
						"amount": pending_amount,
						"default": 1,
					},
				)


def get_itemised_tax_breakup_html(doc):
	if not doc.taxes:
		return

	# get headers
	tax_accounts = []
	for tax in doc.taxes:
		if getattr(tax, "category", None) and tax.category == "Valuation":
			continue
		if tax.description not in tax_accounts:
			tax_accounts.append(tax.description)

	with temporary_flag("company", doc.company):
		headers = get_itemised_tax_breakup_header(doc.doctype + " Item", tax_accounts)
		itemised_tax_data = get_itemised_tax_breakup_data(doc)
		get_rounded_tax_amount(itemised_tax_data, doc.precision("tax_amount", "taxes"))
		update_itemised_tax_data(doc)

	return frappe.render_template(
		"templates/includes/itemised_tax_breakup.html",
		dict(
			headers=headers,
			itemised_tax_data=itemised_tax_data,
			tax_accounts=tax_accounts,
			doc=doc,
		),
	)


@frappe.whitelist()
def get_round_off_applicable_accounts(company, account_list):
	# required to set correct region
	with temporary_flag("company", company):
		return get_regional_round_off_accounts(company, account_list)


@erpnext.allow_regional
def get_regional_round_off_accounts(company, account_list):
	pass


@erpnext.allow_regional
def update_itemised_tax_data(doc):
	# Don't delete this method, used for localization
	pass


@erpnext.allow_regional
def get_itemised_tax_breakup_header(item_doctype, tax_accounts):
	return [_("Item"), _("Taxable Amount"), *tax_accounts]


@erpnext.allow_regional
def get_itemised_tax_breakup_data(doc):
	itemised_tax = get_itemised_tax(doc.taxes)
	itemised_tax_data = []
	for item_code, taxes in itemised_tax.items():
		taxable_amount = next(iter(taxes.values())).get("net_amount")
		itemised_tax_data.append(frappe._dict({"item": item_code, "taxable_amount": taxable_amount, **taxes}))

	return itemised_tax_data


def get_itemised_tax(taxes, with_tax_account=False):
	itemised_tax = {}
	for tax in taxes:
		if getattr(tax, "category", None) and tax.category == "Valuation":
			continue

		item_tax_map = json.loads(tax.item_wise_tax_detail) if tax.item_wise_tax_detail else {}
		if item_tax_map:
			for item_code, tax_data in item_tax_map.items():
				tax_data = ItemWiseTaxDetail(**tax_data)
				itemised_tax.setdefault(item_code, frappe._dict())
				itemised_tax[item_code][tax.description] = tax_data

				if with_tax_account:
					itemised_tax[item_code][tax.description].tax_account = tax.account_head

	return itemised_tax


from erpnext.deprecation_dumpster import (
	taxes_and_totals_get_itemised_taxable_amount as get_itemised_taxable_amount,
)


def get_rounded_tax_amount(itemised_tax, precision):
	# Rounding based on tax_amount precision
	for taxes in itemised_tax:
		for row in taxes.values():
			if isinstance(row, dict) and isinstance(row["tax_amount"], float):
				row["tax_amount"] = flt(row["tax_amount"], precision)


@frappe.whitelist()
def get_rounding_tax_settings():
	return frappe.get_single_value("Accounts Settings", "round_row_wise_tax")


class init_landed_taxes_and_totals:
	def __init__(self, doc):
		self.doc = doc
		self.tax_field = "taxes" if self.doc.doctype == "Landed Cost Voucher" else "additional_costs"
		self.set_account_currency()
		self.set_exchange_rate()
		self.set_amounts_in_company_currency()

	def set_account_currency(self):
		company_currency = erpnext.get_company_currency(self.doc.company)
		for d in self.doc.get(self.tax_field):
			if not d.account_currency:
				account_currency = frappe.get_cached_value("Account", d.expense_account, "account_currency")
				d.account_currency = account_currency or company_currency

	def set_exchange_rate(self):
		company_currency = erpnext.get_company_currency(self.doc.company)
		for d in self.doc.get(self.tax_field):
			if d.account_currency == company_currency:
				d.exchange_rate = 1
			elif not d.exchange_rate:
				d.exchange_rate = get_exchange_rate(
					self.doc.posting_date,
					account=d.expense_account,
					account_currency=d.account_currency,
					company=self.doc.company,
				)

			if not d.exchange_rate:
				frappe.throw(_("Row {0}: Exchange Rate is mandatory").format(d.idx))

	def set_amounts_in_company_currency(self):
		for d in self.doc.get(self.tax_field):
			d.amount = flt(d.amount, d.precision("amount"))
			d.base_amount = flt(d.amount * flt(d.exchange_rate), d.precision("base_amount"))
