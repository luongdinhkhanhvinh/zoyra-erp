// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.provide("erpnext.accounts");
erpnext.sales_common.setup_selling_controller();

erpnext.accounts.pos.setup("POS Invoice");
erpnext.selling.POSInvoiceController = class POSInvoiceController extends erpnext.selling.SellingController {
	settings = {};

	setup(doc) {
		this.setup_posting_date_time_check();
		super.setup(doc);
	}

	company() {
		erpnext.accounts.dimensions.update_dimension(this.frm, this.frm.doctype);
		this.frm.set_value("set_warehouse", "");
		this.frm.set_value("taxes_and_charges", "");
	}

	onload(doc) {
		super.onload();
		this.frm.ignore_doctypes_on_cancel_all = [
			"POS Invoice Merge Log",
			"POS Closing Entry",
			"Serial and Batch Bundle",
		];

		if (doc.__islocal && doc.is_pos && frappe.get_route_str() !== "point-of-sale") {
			this.frm.script_manager.trigger("is_pos");
			this.frm.refresh_fields();
		}

		this.frm.set_query("set_warehouse", function (doc) {
			return {
				filters: {
					company: doc.company ? doc.company : "",
				},
			};
		});

		this.frm.set_query("item_code", "items", function (doc) {
			return {
				query: "erpnext.accounts.doctype.pos_invoice.pos_invoice.item_query",
				filters: {
					has_variants: ["=", 0],
					is_sales_item: ["=", 1],
					disabled: ["=", 0],
					is_fixed_asset: ["=", 0],
					pos_profile: ["=", doc.pos_profile],
				},
			};
		});

		erpnext.accounts.dimensions.setup_dimension_filters(this.frm, this.frm.doctype);
	}

	onload_post_render(frm) {
		super.onload_post_render();
		this.pos_profile(frm);
	}

	refresh(doc) {
		super.refresh();

		if (doc.docstatus == 1 && !doc.is_return) {
			this.frm.add_custom_button(__("Return"), this.make_sales_return.bind(this), __("Create"));
			if (["Partly Paid", "Overdue", "Unpaid"].includes(doc.status)) {
				this.frm.add_custom_button(
					__("Payment"),
					this.collect_outstanding_payment.bind(this),
					__("Create")
				);
			}
			this.frm.page.set_inner_btn_group_as_primary(__("Create"));
		}

		if (doc.is_return && doc.__islocal) {
			this.frm.return_print_format = "Sales Invoice Return";
			this.frm.set_value("consolidated_invoice", "");
		}

		this.frm.set_query(
			"customer",
			function () {
				const customer_groups = this.settings?.customer_groups;

				if (!customer_groups?.length) return {};

				return {
					filters: {
						customer_group: ["in", customer_groups],
					},
				};
			}.bind(this)
		);
	}

	is_pos() {
		this.set_pos_data();
	}

	async set_pos_data() {
		if (this.frm.doc.is_pos) {
			this.frm.set_value("allocate_advances_automatically", 0);
			if (!this.frm.doc.company) {
				this.frm.set_value("is_pos", 0);
				frappe.msgprint(__("Please specify Company to proceed"));
			} else {
				const r = await this.frm.call({
					doc: this.frm.doc,
					method: "set_missing_values",
					freeze: true,
				});
				if (!r.exc) {
					if (r.message) {
						this.frm.pos_print_format = r.message.print_format || "";
						this.frm.meta.default_print_format = r.message.print_format || "";
						this.frm.doc.campaign = r.message.campaign;
						this.frm.allow_print_before_pay = r.message.allow_print_before_pay;
					}
					this.frm.script_manager.trigger("update_stock");
					this.calculate_taxes_and_totals();
					this.frm.doc.taxes_and_charges && this.frm.script_manager.trigger("taxes_and_charges");
					frappe.model.set_default_values(this.frm.doc);
					this.set_dynamic_labels();
				}
			}
		}
	}

	customer() {
		if (!this.frm.doc.customer) return;
		const pos_profile = this.frm.doc.pos_profile;
		if (this.frm.updating_party_details) return;
		erpnext.utils.get_party_details(
			this.frm,
			"erpnext.accounts.party.get_party_details",
			{
				posting_date: this.frm.doc.posting_date,
				party: this.frm.doc.customer,
				party_type: "Customer",
				account: this.frm.doc.debit_to,
				price_list: this.frm.doc.selling_price_list,
				pos_profile: pos_profile,
				company_address: this.frm.doc.company_address,
			},
			() => {
				this.apply_pricing_rule();
			}
		);
	}

	pos_profile(frm) {
		if (!frm.pos_profile || frm.pos_profile == "") {
			this.update_customer_groups_settings([]);
			return;
		}

		frappe.call({
			method: "erpnext.selling.page.point_of_sale.point_of_sale.get_pos_profile_data",
			args: { pos_profile: frm.pos_profile },
			callback: ({ message: profile }) => {
				this.update_customer_groups_settings(profile?.customer_groups);
				this.frm.set_value("company", profile?.company);
			},
		});
	}

	update_customer_groups_settings(customer_groups) {
		this.settings.customer_groups = customer_groups?.map((group) => group.name);
	}

	amount() {
		this.write_off_outstanding_amount_automatically();
	}

	change_amount() {
		if (this.frm.doc.paid_amount > this.frm.doc.grand_total) {
			this.calculate_write_off_amount();
		} else {
			this.frm.set_value("change_amount", 0.0);
			this.frm.set_value("base_change_amount", 0.0);
		}

		this.frm.refresh_fields();
	}

	loyalty_amount() {
		this.calculate_outstanding_amount();
		this.frm.refresh_field("outstanding_amount");
		this.frm.refresh_field("paid_amount");
		this.frm.refresh_field("base_paid_amount");
	}

	write_off_outstanding_amount_automatically() {
		if (cint(this.frm.doc.write_off_outstanding_amount_automatically)) {
			frappe.model.round_floats_in(this.frm.doc, ["grand_total", "paid_amount"]);
			// this will make outstanding amount 0
			this.frm.set_value(
				"write_off_amount",
				flt(
					this.frm.doc.grand_total - this.frm.doc.paid_amount - this.frm.doc.total_advance,
					precision("write_off_amount")
				)
			);
		}

		this.calculate_outstanding_amount(false);
		this.frm.refresh_fields();
	}

	make_sales_return() {
		frappe.model.open_mapped_doc({
			method: "erpnext.accounts.doctype.pos_invoice.pos_invoice.make_sales_return",
			frm: this.frm,
		});
	}

	async collect_outstanding_payment() {
		const total_amount = flt(this.frm.doc.rounded_total) | flt(this.frm.doc.grand_total);
		const paid_amount = flt(this.frm.doc.paid_amount);
		const outstanding_amount = flt(this.frm.doc.outstanding_amount);
		const me = this;

		const table_fields = [
			{
				fieldname: "mode_of_payment",
				fieldtype: "Link",
				in_list_view: 1,
				label: __("Mode of Payment"),
				options: "Mode of Payment",
				reqd: 1,
			},
			{
				fieldname: "amount",
				fieldtype: "Currency",
				in_list_view: 1,
				label: __("Amount"),
				options: this.frm.doc.currency,
				reqd: 1,
				onchange: function () {
					dialog.fields_dict.payments.df.data.some((d) => {
						if (d.idx == this.doc.idx) {
							d.amount = this.value === null ? 0 : this.value;
							dialog.fields_dict.payments.grid.refresh();
							return true;
						}
					});

					let amount = 0;
					for (let d of dialog.fields_dict.payments.df.data) {
						amount += d.amount;
					}

					let change_amount = total_amount - (paid_amount + amount);

					dialog.fields_dict.outstanding_amount.set_value(
						outstanding_amount - amount < 0 ? 0 : outstanding_amount - amount
					);
					dialog.fields_dict.paid_amount.set_value(paid_amount + amount);
					dialog.fields_dict.change_amount.set_value(change_amount < 0 ? change_amount * -1 : 0);
				},
			},
		];
		const payment_method_data = await this.fetch_pos_payment_methods();
		const dialog = new frappe.ui.Dialog({
			title: __("Collect Outstanding Amount"),
			fields: [
				{
					fieldname: "payments",
					fieldtype: "Table",
					label: __("Payments"),
					cannot_add_rows: false,
					in_place_edit: true,
					reqd: 1,
					data: payment_method_data,
					fields: table_fields,
				},
				{
					fieldname: "section_break_1",
					fieldtype: "Section Break",
				},
				{
					fieldname: "outstanding_amount",
					fieldtype: "Currency",
					label: __("Outstanding Amount"),
					read_only: 1,
					default: outstanding_amount,
				},
				{
					fieldname: "column_break_1",
					fieldtype: "Column Break",
				},
				{
					fieldname: "paid_amount",
					fieldtype: "Currency",
					label: __("Paid Amount"),
					read_only: 1,
					default: paid_amount,
				},
				{
					fieldname: "change_amount",
					fieldtype: "Currency",
					label: __("Change Amount"),
					read_only: 1,
					default: 0,
				},
			],
			primary_action_label: __("Submit"),
			primary_action(values) {
				dialog.hide();
				me.frm.call({
					doc: me.frm.doc,
					method: "update_payments",
					args: {
						payments: values.payments.filter((d) => d.amount != 0),
					},
					freeze: true,
					callback: function (r) {
						if (!r.exc) {
							frappe.show_alert({
								message: __("Payments updated."),
								indicator: "green",
							});
							me.frm.reload_doc();
						} else {
							frappe.show_alert({
								message: __("Payments could not be updated."),
								indicator: "red",
							});
						}
					},
				});
			},
		});
		dialog.show();
	}

	async fetch_pos_payment_methods() {
		const pos_profile = this.frm.doc.pos_profile;
		if (!pos_profile) return;
		const pos_profile_doc = await frappe.db.get_doc("POS Profile", pos_profile);
		const data = [];
		pos_profile_doc.payments.forEach((pay) => {
			const { mode_of_payment } = pay;
			data.push({ mode_of_payment, amount: 0 });
		});
		return data;
	}
};

extend_cscript(cur_frm.cscript, new erpnext.selling.POSInvoiceController({ frm: cur_frm }));

frappe.ui.form.on("POS Invoice", {
	redeem_loyalty_points: function (frm) {
		frm.events.get_loyalty_details(frm);
	},

	loyalty_points: function (frm) {
		if (frm.redemption_conversion_factor) {
			frm.events.set_loyalty_points(frm);
		} else {
			frappe.call({
				method: "erpnext.accounts.doctype.loyalty_program.loyalty_program.get_redeemption_factor",
				args: {
					loyalty_program: frm.doc.loyalty_program,
				},
				callback: function (r) {
					if (r) {
						frm.redemption_conversion_factor = r.message;
						frm.events.set_loyalty_points(frm);
					}
				},
			});
		}
	},

	get_loyalty_details: function (frm) {
		if (frm.doc.customer && frm.doc.redeem_loyalty_points) {
			frappe.call({
				method: "erpnext.accounts.doctype.loyalty_program.loyalty_program.get_loyalty_program_details",
				args: {
					customer: frm.doc.customer,
					loyalty_program: frm.doc.loyalty_program,
					expiry_date: frm.doc.posting_date,
					company: frm.doc.company,
				},
				callback: function (r) {
					if (r) {
						frm.set_value("loyalty_redemption_account", r.message.expense_account);
						frm.set_value("loyalty_redemption_cost_center", r.message.cost_center);
						frm.redemption_conversion_factor = r.message.conversion_factor;
					}
				},
			});
		}
	},

	set_loyalty_points: function (frm) {
		if (frm.redemption_conversion_factor) {
			let loyalty_amount = flt(
				frm.redemption_conversion_factor * flt(frm.doc.loyalty_points),
				precision("loyalty_amount")
			);
			var remaining_amount =
				flt(frm.doc.grand_total) - flt(frm.doc.total_advance) - flt(frm.doc.write_off_amount);
			if (frm.doc.grand_total && remaining_amount < loyalty_amount) {
				let redeemable_points = parseInt(remaining_amount / frm.redemption_conversion_factor);
				frappe.throw(__("You can only redeem max {0} points in this order.", [redeemable_points]));
			}
			frm.set_value("loyalty_amount", loyalty_amount);
		}
	},

	request_for_payment: function (frm) {
		if (!frm.doc.contact_mobile) {
			frappe.throw(__("Please enter mobile number first."));
		}
		frm.dirty();
		frm.save().then(() => {
			frappe.dom.freeze(__("Waiting for payment..."));
			frappe
				.call({
					method: "create_payment_request",
					doc: frm.doc,
				})
				.fail(() => {
					frappe.dom.unfreeze();
					frappe.msgprint(__("Payment request failed"));
				})
				.then(({ message }) => {
					const payment_request_name = message.name;
					setTimeout(() => {
						frappe.db
							.get_value("Payment Request", payment_request_name, ["status", "grand_total"])
							.then(({ message }) => {
								if (message.status != "Paid") {
									frappe.dom.unfreeze();
									frappe.msgprint({
										message: __(
											"Payment Request took too long to respond. Please try requesting for payment again."
										),
										title: __("Request Timeout"),
									});
								} else if (frappe.dom.freeze_count != 0) {
									frappe.dom.unfreeze();
									frm.reload_doc();
									cur_pos.payment.events.submit_invoice();

									frappe.show_alert({
										message: __("Payment of {0} received successfully.", [
											format_currency(message.grand_total, frm.doc.currency, 0),
										]),
										indicator: "green",
									});
								}
							});
					}, 60000);
				});
		});
	},
});

frappe.ui.form.on("Sales Invoice Payment", {
	mode_of_payment: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "set_account_for_mode_of_payment",
			callback: function (r) {
				refresh_field("payments");
			},
		});
	},
});
