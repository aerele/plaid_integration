# Copyright (c) 2026, Aerele Technologies and contributors
# For license information, please see license.txt

import click
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from plaid_integration.plaid_integration.constants.custom_fields import CUSTOM_FIELDS


def after_install():
	try:
		print("Setting up Plaid Integration custom fields...")
		create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	except Exception as e:
		click.secho(
			"Installation for Plaid Integration failed. Please try re-installing the app.",
			fg="bright_red",
		)
		raise e

	click.secho("Plaid Integration installed successfully!", fg="green")
