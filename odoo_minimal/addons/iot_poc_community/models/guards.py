"""A JSON caller cannot manufacture this in-process object capability."""
from odoo.exceptions import AccessError, ValidationError

BRIDGE = "iot_poc_community.group_bridge"
REVIEWER = "iot_poc_community.group_reviewer"
MANAGER = "iot_poc_community.group_manager"
TOKEN_KEY = "_iot_poc_internal_capability"
TOKEN = object()


def is_internal(records):
    return records.env.context.get(TOKEN_KEY) is TOKEN


def internal(records):
    clean = {k: records.env.context[k] for k in ("allowed_company_ids", "lang", "tz") if k in records.env.context}
    clean.update({TOKEN_KEY: TOKEN, "tracking_disable": True, "mail_notrack": True,
                  "mail_create_nolog": True, "mail_create_nosubscribe": True})
    return records.sudo().with_context(clean)


def require_bridge(records):
    if not records.env.user.has_group(BRIDGE):
        raise AccessError("Only the dedicated integration bridge may use this API.")


def require_reviewer(records):
    if records.env.user.has_group(BRIDGE) or not records.env.user.has_group(REVIEWER):
        raise AccessError("A human reviewer role is required; the bridge cannot make decisions.")


def require_manager(records):
    if records.env.user.has_group(BRIDGE):
        raise AccessError("The bridge cannot change the mapping configuration.")
    if not records.env.su and not records.env.user.has_group(MANAGER):
        raise AccessError("The prototype manager role is required.")


def request_company(records, company_id):
    if type(company_id) is not int or company_id <= 0:
        raise ValidationError("An explicit positive company_id is required.")
    if company_id not in records.env.user.company_ids.ids:
        raise AccessError("This account cannot use the requested company.")
    return records.env["res.company"].browse(company_id)


def require_company(records):
    if any(company.id not in records.env.user.company_ids.ids for company in records.company_id):
        raise AccessError("Company access denied.")
