// Copyright (c) 2026, Aerele Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Plaid Item", {
	refresh(frm) {
		if (frm.doc.__islocal) {
			return;
		}

		if (frm.doc.status === "Needs Re-auth") {
			frm.add_custom_button(__("Re-connect Bank"), () => {
				fetch_reauth_token_and_open(frm);
			}).addClass("btn-warning");
		}

		if (frm.doc.status !== "Disconnected") {
			frm.add_custom_button(__("Disconnect Bank"), () => {
				confirm_disconnect_bank(frm);
			});
		}
	},
});

function confirm_disconnect_bank(frm) {
	frappe.confirm(
		__(
			"Disconnect <b>{0}</b> from Plaid? Existing transactions are kept, but no new transactions will sync. To reconnect later, link the bank again from Plaid Settings.",
			[frm.doc.bank]
		),
		() => disconnect_bank(frm)
	);
}

function disconnect_bank(frm) {
	frappe.call({
		method: "plaid_integration.plaid_integration.api.disconnect_bank",
		args: { plaid_item: frm.doc.name },
		freeze: true,
		freeze_message: __("Disconnecting bank..."),
		callback(r) {
			if (!r.message || !r.message.disconnected) {
				return;
			}

			frappe.show_alert({
				message: __("Bank disconnected successfully."),
				indicator: "green",
			});
			frm.reload_doc();
		},
		error_callback() {
			frappe.msgprint({
				title: __("Disconnection Failed"),
				message: __("Could not disconnect the bank. Please check the Error Log."),
				indicator: "red",
			});
		},
	});
}

function fetch_reauth_token_and_open(frm) {
	frappe.call({
		method: "plaid_integration.plaid_integration.api.get_reauth_link_token",
		args: { plaid_item: frm.doc.name },
		freeze: true,
		freeze_message: __("Connecting to Plaid..."),
		callback(r) {
			if (!r.message || !r.message.link_token) {
				frappe.msgprint({
					title: __("Connection Failed"),
					message: __("Could not connect to Plaid. Please check the error log."),
					indicator: "red",
				});
				return;
			}

			new plaid_integration.PlaidLink({
				link_token: r.message.link_token,
				on_success() {
					on_reauth_success(frm);
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

function on_reauth_success(frm) {
	frappe.call({
		method: "plaid_integration.plaid_integration.api.complete_reauth",
		args: { plaid_item: frm.doc.name },
		freeze: true,
		freeze_message: __("Re-connecting bank. Please wait..."),
		callback() {
			frappe.show_alert({
				message: __("Bank re-connected successfully."),
				indicator: "green",
			});
			frm.reload_doc();
		},
		error_callback() {
			frappe.msgprint({
				title: __("Re-connection Failed"),
				message: __("Re-authentication completed but status could not be updated. Please check the error log."),
				indicator: "red",
			});
		},
	});
}
