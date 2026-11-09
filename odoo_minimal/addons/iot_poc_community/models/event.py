from psycopg2.errors import UniqueViolation

from odoo import api, fields, models
from odoo.exceptions import AccessError, ConcurrencyError, ValidationError

from ..core import canonical_json, iso_utc, validate_payload, validate_review
from .guards import internal, is_internal, request_company, require_bridge, require_company, require_reviewer

OUTCOMES = [("unverified", "Unverified"), ("pass", "Pass"), ("fail", "Fail"), ("not_verifiable", "Not verifiable")]
QUALITY = [("unverified", "Unverified"), ("conforming", "Conforming"), ("nonconforming", "Nonconforming")]
EVIDENCE = [("workflow_test", "Workflow test only"), ("independent_inspection", "Independent inspection")]


class Event(models.Model):
    _name = "iot.poc.event"
    _description = "Immutable historical cycle replay"
    _rec_name = "event_id"
    _order = "received_at desc, id desc"
    _check_company_auto = True

    event_id = fields.Char(required=True, readonly=True, index=True, copy=False)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    machine_id = fields.Many2one("iot.poc.machine", required=True, readonly=True, ondelete="restrict", check_company=True)
    mapping_id = fields.Many2one("iot.poc.mapping", required=True, readonly=True, ondelete="restrict", check_company=True)
    production_id = fields.Many2one("mrp.production", required=True, readonly=True, ondelete="restrict", check_company=True)
    product_id = fields.Many2one("product.product", required=True, readonly=True, ondelete="restrict", check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, readonly=True, ondelete="restrict", check_company=True)
    session_id = fields.Char(required=True, readonly=True)
    source_kind = fields.Selection([("replay", "Historical replay")], required=True, readonly=True)
    dataset_sha256 = fields.Char(required=True, readonly=True)
    dataset_filename = fields.Char(required=True, readonly=True)
    cycle_label = fields.Integer(required=True, readonly=True, index=True)
    sample_count = fields.Integer(required=True, readonly=True)
    source_is_ok = fields.Boolean(string="Notebook count flag (is_ok)", readonly=True)
    rule_version = fields.Char(required=True, readonly=True)
    source_start = fields.Char(string="First source timestamp (timezone unknown)", required=True, readonly=True)
    source_end = fields.Char(string="Last source timestamp (timezone unknown)", required=True, readonly=True)
    source_timezone = fields.Char(default=False, readonly=True, help="Unknown. Original source strings are not converted to UTC.")
    nominal_sampling_interval_seconds = fields.Float(required=True, readonly=True)
    emitted_at = fields.Datetime(required=True, readonly=True)
    received_at = fields.Datetime(required=True, readonly=True)
    canonical_payload = fields.Text(required=True, readonly=True)
    payload_hash = fields.Char(required=True, readonly=True)
    samples_json = fields.Text(required=True, readonly=True)
    mapping_snapshot = fields.Text(required=True, readonly=True)
    triage = fields.Selection([("routine", "Routine"), ("review_required", "Review required")], required=True, readonly=True, index=True)
    review_state = fields.Selection([("pending", "Pending"), ("reviewed", "Reviewed")], required=True, default="pending", readonly=True)
    review_outcome = fields.Selection(OUTCOMES, string="Human review outcome", required=True, default="unverified", readonly=True)
    independent_quality = fields.Selection(QUALITY, string="Independent quality assessment", required=True, default="unverified", readonly=True)
    last_evidence_kind = fields.Selection(EVIDENCE, readonly=True)
    reviewed_by = fields.Many2one("res.users", readonly=True, ondelete="restrict")
    reviewed_at = fields.Datetime(readonly=True)
    review_revision = fields.Integer(readonly=True, default=0)
    audit_ids = fields.One2many("iot.poc.review", "event_id", readonly=True)
    _company_event_unique = models.Constraint("UNIQUE(company_id, event_id)", "A company accepts an event identifier only once.")

    @api.model_create_multi
    def create(self, vals_list):
        if not is_internal(self):
            raise AccessError("Events can only be created by ingest_event.")
        return super().create(vals_list)

    def write(self, vals):
        if not is_internal(self):
            raise AccessError("Source data and review fields cannot be edited directly.")
        return super().write(vals)

    def unlink(self):
        raise AccessError("Events form an append-only register and cannot be deleted.")

    def _result(self, duplicate=False):
        self.ensure_one()
        return {"event_id": self.event_id, "record_id": self.id, "payload_hash": self.payload_hash,
                "duplicate": bool(duplicate), "review_state": self.review_state,
                "review_outcome": self.review_outcome, "independent_quality": self.independent_quality,
                "triage": self.triage, "received_at": iso_utc(self.received_at)}

    def _duplicate(self, digest):
        self.ensure_one()
        if self.payload_hash != digest:
            raise ValidationError("EVENT_ID_PAYLOAD_CONFLICT: this event_id already has different immutable content.")
        return self._result(duplicate=True)

    @api.model
    def _resolve_mapping(self, payload, company):
        machines = internal(self.env["iot.poc.machine"].with_context(allowed_company_ids=company.ids)).search([
            ("company_id", "=", company.id), ("code", "=", payload["machine_ref"])], limit=2)
        if len(machines) != 1:
            raise ValidationError("UNKNOWN_MACHINE: register exactly one demonstration alias before ingesting.")
        machines._touch()
        mappings = internal(self.env["iot.poc.mapping"].with_context(allowed_company_ids=company.ids)).search([
            ("company_id", "=", company.id), ("machine_id", "=", machines.id),
            ("dataset_sha256", "=", payload["dataset_sha256"]), ("cycle_label", "=", payload["cycle_label"])], limit=2)
        if len(mappings) != 1:
            raise ValidationError("UNKNOWN_MAPPING: register one explicit dataset-cycle-to-unit mapping.")
        mappings._validate_links()
        if (mappings.code != payload["mapping_ref"] or mappings.production_id.origin != payload["production_order_ref"]
                or mappings.product_id.default_code != payload["product_ref"] or mappings.lot_id.name != payload["lot_ref"]):
            raise ValidationError("MAPPING_REFERENCE_CONFLICT: payload references disagree with the registered demonstration mapping.")
        return mappings

    @api.model
    def ingest_event(self, payload, company_id=None):
        """JSON-2 bridge endpoint. No decision is inferred from source_is_ok."""
        require_bridge(self)
        company = request_company(self, company_id)
        try:
            canonical, digest, emitted_at = validate_payload(payload)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValidationError(str(exc)) from exc
        events = internal(self.with_context(allowed_company_ids=company.ids))
        domain = [("company_id", "=", company.id), ("event_id", "=", payload["event_id"])]
        existing = events.search(domain, limit=1)
        if existing:
            return existing._duplicate(digest)
        mapping = self._resolve_mapping(payload, company)
        values = {key: payload[key] for key in (
            "event_id", "session_id", "source_kind", "dataset_sha256", "dataset_filename", "cycle_label",
            "sample_count", "source_is_ok", "rule_version", "source_start", "source_end", "nominal_sampling_interval_seconds")}
        values.update({"company_id": company.id, "machine_id": mapping.machine_id.id, "mapping_id": mapping.id,
            "production_id": mapping.production_id.id, "product_id": mapping.product_id.id, "lot_id": mapping.lot_id.id,
            "emitted_at": emitted_at, "received_at": fields.Datetime.now(), "canonical_payload": canonical,
            "payload_hash": digest, "samples_json": canonical_json(payload["samples"]),
            "triage": "routine" if payload["source_is_ok"] else "review_required",
            "mapping_snapshot": canonical_json({"mapping_code": mapping.code, "mapping_kind": mapping.mapping_kind,
                "historical_mapping_verified": False, "machine_ref": mapping.machine_id.code,
                "production_id": mapping.production_id.id, "production_name": mapping.production_id.name,
                "production_order_ref": mapping.production_id.origin, "product_ref": mapping.product_id.default_code,
                "lot_ref": mapping.lot_id.name, "cycle_to_units": 1})})
        try:
            with self.env.cr.savepoint():
                event = events.create(values)
                event.flush_recordset()
                return event._result()
        except UniqueViolation as exc:
            if exc.diag.constraint_name != self._company_event_unique.full_name(self):
                raise
            existing = events.search(domain, limit=1)
            if existing:
                return existing._duplicate(digest)
            # Odoo 20 JSON-2 retries ConcurrencyError after rolling back the whole
            # request. A unique winner may be invisible in a REPEATABLE READ snapshot.
            raise ConcurrencyError("Concurrent ingestion needs a fresh transaction snapshot.") from exc

    @api.model
    def get_event_result(self, event_id, company_id=None):
        require_bridge(self)
        company = request_company(self, company_id)
        if not isinstance(event_id, str) or len(event_id) != 71 or not event_id.startswith("poc-v1:"):
            raise ValidationError("Invalid event identifier.")
        event = internal(self.with_context(allowed_company_ids=company.ids)).search([
            ("company_id", "=", company.id), ("event_id", "=", event_id)], limit=1)
        if not event:
            raise ValidationError("UNKNOWN_EVENT: no accepted event has this company and identifier.")
        return event._result()

    def _lock_for_review(self):
        self.ensure_one()
        require_reviewer(self)
        self.check_access("read")
        require_company(self)
        self.flush_recordset()
        self.env.cr.execute("SELECT id FROM iot_poc_event WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset()

    def action_open_review(self):
        self.ensure_one()
        require_reviewer(self)
        self.check_access("read")
        require_company(self)
        return {"type": "ir.actions.act_window", "name": "Review historical replay", "res_model": "iot.poc.review.wizard",
                "view_mode": "form", "target": "new", "context": {"default_event_id": self.id,
                "default_expected_revision": self.review_revision, "default_evidence_kind": "workflow_test"}}

    def record_review(self, outcome, evidence_kind, notes, evidence_ref, expected_revision, correction_reason=None):
        self._lock_for_review()
        if type(expected_revision) is not int or expected_revision != self.review_revision:
            raise ValidationError("STALE_REVIEW: reopen the review form; another review changed this event.")
        try:
            quality = validate_review(outcome, evidence_kind, notes, evidence_ref,
                correction=self.review_state == "reviewed", correction_reason=correction_reason,
                previous_evidence_kind=self.last_evidence_kind)
        except (TypeError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc
        now, actor = fields.Datetime.now(), self.env.uid
        internal(self.env["iot.poc.review"]).create({"event_id": self.id, "company_id": self.company_id.id,
            "revision": self.review_revision + 1, "outcome_before": self.review_outcome, "outcome_after": outcome,
            "independent_before": self.independent_quality, "independent_after": quality,
            "evidence_kind": evidence_kind, "notes": notes.strip(), "evidence_ref": evidence_ref.strip(),
            "correction_reason": (correction_reason or "").strip(), "reviewer_id": actor, "recorded_at": now})
        internal(self).write({"review_state": "reviewed", "review_outcome": outcome, "independent_quality": quality,
            "last_evidence_kind": evidence_kind, "reviewed_by": actor, "reviewed_at": now,
            "review_revision": self.review_revision + 1})
        return self._result()


class Review(models.Model):
    _name = "iot.poc.review"
    _description = "Immutable human review audit"
    _order = "recorded_at desc, id desc"
    _rec_name = "event_id"
    _check_company_auto = True

    event_id = fields.Many2one("iot.poc.event", required=True, ondelete="restrict", readonly=True, check_company=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True)
    revision = fields.Integer(required=True, readonly=True)
    outcome_before = fields.Selection(OUTCOMES, required=True, readonly=True)
    outcome_after = fields.Selection(OUTCOMES, required=True, readonly=True)
    independent_before = fields.Selection(QUALITY, required=True, readonly=True)
    independent_after = fields.Selection(QUALITY, required=True, readonly=True)
    evidence_kind = fields.Selection(EVIDENCE, required=True, readonly=True)
    notes = fields.Text(required=True, readonly=True)
    evidence_ref = fields.Text(required=True, readonly=True)
    correction_reason = fields.Text(readonly=True)
    reviewer_id = fields.Many2one("res.users", required=True, ondelete="restrict", readonly=True)
    recorded_at = fields.Datetime(required=True, readonly=True)
    _event_revision_unique = models.Constraint("UNIQUE(event_id, revision)", "Each review revision is recorded once.")

    @api.model_create_multi
    def create(self, vals_list):
        if not is_internal(self):
            raise AccessError("Review audit records can only be appended by record_review.")
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError("Review history is immutable; record a correction on the event.")

    def unlink(self):
        raise AccessError("Review history cannot be deleted.")
