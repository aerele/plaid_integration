frappe.provide("plaid_integration");

const PLAID_LINK_URL = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";

/**
 * PlaidLink
 *
 * Shared utility for opening the Plaid Link popup.
 * Used for both fresh bank linking (Plaid Settings) and re-authentication (Plaid Item).
 *
 * Usage:
 *   new plaid_integration.PlaidLink({
 *       link_token : "link-sandbox-...",
 *       on_success : (public_token, metadata) => { ... },
 *   }).open();
 */
plaid_integration.PlaidLink = class PlaidLink {
	constructor({ link_token, on_success, on_exit } = {}) {
		this.link_token = link_token;
		this.on_success = on_success;
		this.on_exit = on_exit;
	}

	open() {
		frappe.require(PLAID_LINK_URL, () => {
			const handler = Plaid.create({
				token: this.link_token,

				onSuccess: (public_token, metadata) => {
					this.on_success?.(public_token, metadata);
				},

				onExit: (err) => {
					if (err) {
						frappe.msgprint({
							title: __("Plaid Closed"),
							message: err.display_message || __("The bank connection was closed. Please try again."),
							indicator: "orange",
						});
					}
					this.on_exit?.(err);
				},
			});

			handler.open();
		});
	}
};
