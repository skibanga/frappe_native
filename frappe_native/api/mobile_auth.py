from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.db_query import DatabaseQuery


def _normalize_app_name(value: str) -> str:
	cleaned = re.sub(r"[^a-zA-Z0-9_\-\s]", "", value).strip()
	if not cleaned:
		return ""
	return cleaned


def _titleize(value: str) -> str:
	return " ".join(part.capitalize() for part in re.split(r"[_\-\s]+", value) if part)


def _safe_count(doctype: str) -> dict[str, int | str | None]:
	if not frappe.has_permission(doctype=doctype, ptype="read"):
		return {"status": "no_access", "value": None}

	try:
		rows = DatabaseQuery(doctype).execute(
			fields=["count(name) as count"],
			limit_page_length=1,
			as_list=False,
		)
		count_value = 0
		if rows and isinstance(rows[0], dict):
			count_value = int(rows[0].get("count") or 0)
		return {"status": "ok", "value": count_value}
	except Exception:
		return {"status": "error", "value": None}


@frappe.whitelist(allow_guest=True)
def get_client_id(app: str | None = None):
	"""
	Return OAuth bootstrap metadata for a specific app.

	Query param:
	- app: Frappe app identifier used during `bench native auth init`
	"""
	app_name = _normalize_app_name(app or "")
	if not app_name:
		frappe.throw(_("Missing required query parameter: app"))

	client_app_name = f"Frappe Native - {_titleize(app_name)}"
	client_name = frappe.db.get_value("OAuth Client", {"app_name": client_app_name}, "name")
	if not client_name:
		frappe.throw(
			_(
				"OAuth Client not configured for app '{0}'. Run `bench native auth init --app {1} --site {2}`."
			).format(app_name, app_name, frappe.local.site)
		)

	client = frappe.get_doc("OAuth Client", client_name)
	base_url = frappe.utils.get_url()
	site_app_name = (
		frappe.get_website_settings("app_name")
		or frappe.get_system_settings("app_name")
		or "Frappe Native"
	)

	return {
		"client_id": client.client_id or client.name,
		"redirect_uri": client.default_redirect_uri,
		"scope": client.scopes,
		"site_url": base_url,
		"sitename": frappe.local.site,
		"app_name": site_app_name,
		"authorization_endpoint": f"{base_url}/api/method/frappe.integrations.oauth2.authorize",
		"token_endpoint": f"{base_url}/api/method/frappe.integrations.oauth2.get_token",
		"revoke_endpoint": f"{base_url}/api/method/frappe.integrations.oauth2.revoke_token",
		"me_endpoint": f"{base_url}/api/method/frappe.auth.get_logged_user",
	}


@frappe.whitelist()
def get_session_snapshot():
	if frappe.session.user == "Guest":
		frappe.throw(_("Authentication required"), frappe.PermissionError)

	user = frappe.session.user
	user_doc = frappe.get_doc("User", user)
	roles = [role for role in frappe.get_roles(user) if role not in {"All", "Guest"}]

	return {
		"user": user,
		"full_name": user_doc.full_name,
		"roles": roles,
		"counts": {
			"ToDo": _safe_count("ToDo"),
			"User": _safe_count("User"),
		},
	}
