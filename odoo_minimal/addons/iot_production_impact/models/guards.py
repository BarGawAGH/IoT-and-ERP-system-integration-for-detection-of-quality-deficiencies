from odoo.exceptions import AccessError, ValidationError

MANAGER = "iot_production_impact.group_impact_manager"
BRIDGE = "iot_poc_community.group_bridge"
TOKEN_KEY = "_iot_impact_internal_capability"
TOKEN = object()


def is_internal(records):
    return records.env.context.get(TOKEN_KEY) is TOKEN


def trusted(records):
    """An in-process capability, not a JSON-settable context flag."""
    return records.with_context(**{
        TOKEN_KEY: TOKEN,
        "tracking_disable": True,
        "mail_notrack": True,
        "mail_create_nolog": True,
        "mail_create_nosubscribe": True,
        "mail_activity_quick_update": True,
        "mail_notify_force_send": False,
    })


def require_manager(records):
    if records.env.user.has_group(BRIDGE) or not records.env.user.has_group(MANAGER):
        raise AccessError("A production-impact manager is required; the integration bridge cannot make decisions.")


def require_company(records):
    if any(c.id not in records.env.user.company_ids.ids for c in records.company_id):
        raise AccessError("Company access denied.")


def reason_text(reason):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 8000:
        raise ValidationError("A nonempty explanation of at most 8000 characters is required.")
    return reason.strip()
