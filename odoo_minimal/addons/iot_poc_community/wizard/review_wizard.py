from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..models.event import EVIDENCE
from ..models.guards import require_company, require_reviewer


class ReviewWizard(models.TransientModel):
    _name = "iot.poc.review.wizard"
    _description = "Review or correct a historical replay event"

    event_id = fields.Many2one("iot.poc.event", required=True, readonly=True)
    company_id = fields.Many2one(related="event_id.company_id", store=True, readonly=True)
    expected_revision = fields.Integer(required=True, readonly=True)
    evidence_kind = fields.Selection(EVIDENCE, required=True, default="workflow_test")
    outcome = fields.Selection([("pass", "Pass"), ("fail", "Fail"), ("not_verifiable", "Not verifiable")], required=True)
    notes = fields.Text(required=True)
    evidence_ref = fields.Text(required=True, help="Workflow protocol/test reference, or an actual independent inspection record.")
    correction_reason = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        require_reviewer(self)
        records = super().create(vals_list)
        records.event_id.check_access("read")
        require_company(records)
        return records

    def write(self, vals):
        require_reviewer(self)
        result = super().write(vals)
        self.event_id.check_access("read")
        require_company(self)
        return result

    def action_submit(self):
        self.ensure_one()
        require_reviewer(self)
        self.check_access("read")
        require_company(self)
        if self.create_uid != self.env.user:
            raise AccessError("A reviewer must use their own review form.")
        self.event_id.record_review(self.outcome, self.evidence_kind, self.notes,
                                   self.evidence_ref, self.expected_revision, self.correction_reason)
        return {"type": "ir.actions.act_window_close"}
