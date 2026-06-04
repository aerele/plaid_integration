// Copyright (c) 2026, Aerele Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Plaid Settings", {
	onload(frm) {
		if (!frm.doc.transaction_history_days) {
			frm.set_value("transaction_history_days", 90);
		}
	},

	refresh(frm) {
		if (frm.doc.enabled && !frm.doc.__unsaved) {
			frm.add_custom_button(__("Link Bank Account"), () => {
				show_company_dialog(frm);
			});

			frm.add_custom_button(__("Sync Now"), () => {
				sync_all_transactions(frm);
			});
		}
	},
});

function show_company_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Select Company"),
		fields: [
			{
				fieldtype: "Link",
				label: __("Company"),
				fieldname: "company",
				options: "Company",
				reqd: 1,
				default: frappe.defaults.get_default("company"),
			},
		],
		primary_action_label: __("Continue"),
		primary_action(values) {
			dialog.hide();
			fetch_link_token_and_open(frm, values.company);
		},
	});

	dialog.show();
}

function fetch_link_token_and_open(frm, company) {
	frappe.call({
		method: "plaid_integration.plaid_integration.api.get_link_token",
		freeze: true,
		freeze_message: __("Connecting to Plaid..."),
		callback(r) {
			if (!r.message || !r.message.link_token) {
				frappe.msgprint({
					title: __("Connection Failed"),
					message: __("Could not connect to Plaid. Please check your Client ID and Secret and try again."),
					indicator: "red",
				});
				return;
			}

			new plaid_integration.PlaidLink({
				link_token: r.message.link_token,
				on_success(public_token, metadata) {
					on_bank_selected(frm, public_token, metadata, company);
				},
			}).open();
		},
		error_callback() {
			frappe.msgprint({
				title: __("Connection Failed"),
				message: __("Could not reach Plaid. Please check your internet connection and try again."),
				indicator: "red",
			});
		},
	});
}

function on_bank_selected(frm, public_token, metadata, company) {
	const institution_name = metadata.institution.name;

	frappe.show_alert({
		message: __("Bank selected. Linking accounts from {0}...", [institution_name]),
		indicator: "blue",
	});

	frappe.call({
		method: "plaid_integration.plaid_integration.api.add_bank_accounts",
		args: { public_token, institution_name, company },
		freeze: true,
		freeze_message: __("Linking your bank accounts. Please wait..."),
		callback(r) {
			if (!r.message) return;

			const { queued, added, total } = r.message;

			if (queued) {
				frappe.show_alert({
					message: __("{0} accounts found. Linking in the background — you will be notified when done.", [total]),
					indicator: "blue",
				});
				return;
			}

			if (added.length) {
				frappe.show_alert({
					message: __("{0} bank account(s) linked successfully from {1}.", [added.length, institution_name]),
					indicator: "green",
				});
			} else {
				frappe.show_alert({
					message: __("All {0} account(s) from {1} are already linked.", [total, institution_name]),
					indicator: "orange",
				});
			}

			frm.reload_doc();
		},
		error_callback() {
			frappe.msgprint({
				title: __("Linking Failed"),
				message: __("Your bank was connected but we could not save the accounts. Please check the Error Log or contact support."),
				indicator: "red",
			});
		},
	});
}

function sync_all_transactions(frm) {
	frappe.call({
		method: "plaid_integration.plaid_integration.api.sync_all_transactions",
		freeze: true,
		freeze_message: __("Queuing transaction sync..."),
		callback(r) {
			if (!r.message) return;

			const { queued } = r.message;

			if (!queued) {
				frappe.show_alert({
					message: __("No active bank connections found to sync."),
					indicator: "orange",
				});
				return;
			}

			frappe.show_alert({
				message: __("{0} bank connection(s) queued for sync. You will be notified when complete.", [queued]),
				indicator: "blue",
			});

			frm.reload_doc();
		},
		error_callback() {
			frappe.msgprint({
				title: __("Sync Failed"),
				message: __("Could not start transaction sync. Please check the Error Log."),
				indicator: "red",
			});
		},
	});
}
