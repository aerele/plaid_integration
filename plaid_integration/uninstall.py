# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import click

from plaid_integration.plaid_integration.constants.custom_fields import CUSTOM_FIELDS


def before_uninstall():
	try:
		print("Removing Plaid Integration custom fields...")
		_delete_custom_fields(CUSTOM_FIELDS)
	except Exception as e:
		click.secho(
			"Removing customizations for Plaid Integration failed. Please try again.",
			fg="bright_red",
		)
		raise e

	click.secho("Plaid Integration removed successfully!", fg="green")


def _delete_custom_fields(custom_fields):
	import frappe

	for doctype, fields in custom_fields.items():
		frappe.db.delete(
			"Custom Field",
			{
				"fieldname": ("in", [field["fieldname"] for field in fields]),
				"dt": doctype,
			},
		)
		frappe.clear_cache(doctype=doctype)
