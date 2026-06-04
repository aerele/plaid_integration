# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PlaidSettings(Document):
	def validate(self):
		if self.enabled:
			if self.transaction_history_days:
				if not (90 <= self.transaction_history_days <= 730):
					frappe.throw(_("Transaction History Days must be between 90 and 730."))

			if self.plaid_client_id and self.plaid_secret:
				from plaid_integration.plaid_integration.plaid_connector import PlaidConnector

				PlaidConnector(settings=self).validate_credentials()
