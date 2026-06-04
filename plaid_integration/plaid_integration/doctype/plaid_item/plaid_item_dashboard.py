from frappe import _


def get_data():
	return {
		"fieldname": "plaid_item",
		"transactions": [
			{"label": _("Bank Accounts"), "items": ["Bank Account"]},
		],
	}
