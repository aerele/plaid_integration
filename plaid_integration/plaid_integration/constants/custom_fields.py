# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

CUSTOM_FIELDS = {
	"Bank Account": [
		{
			"fieldname": "plaid_section",
			"label": "Plaid Integration",
			"fieldtype": "Section Break",
			"insert_after": "branch_code",
		},
		{
			"fieldname": "plaid_item",
			"label": "Plaid Item",
			"fieldtype": "Link",
			"options": "Plaid Item",
			"insert_after": "plaid_section",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "integration_id",
			"label": "Plaid Account ID",
			"fieldtype": "Data",
			"insert_after": "plaid_item",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "plaid_column_break",
			"fieldtype": "Column Break",
			"insert_after": "integration_id",
		},
		{
			"fieldname": "last_integration_date",
			"label": "Last Sync Date",
			"fieldtype": "Date",
			"insert_after": "plaid_column_break",
			"read_only": 1,
			"no_copy": 1,
		},
	],
}
