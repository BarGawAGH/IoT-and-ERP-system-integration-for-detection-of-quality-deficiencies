from odoo import fields, models


class ImpactDisposition(models.TransientModel):
    _name = 'iot.impact.disposition'
    _description = 'Record an explicit production disposition'

    case_id = fields.Many2one('iot.impact.case', required=True, readonly=True)
    company_id = fields.Many2one(related='case_id.company_id', readonly=True)
    evidence_mode = fields.Selection(related='case_id.evidence_mode', readonly=True)
    operation = fields.Selection([
        ('hold', 'Place on hold'), ('release', 'Release to available stock - simulation'),
        ('start_rework', 'Start rework - simulation'), ('finish_rework', 'Finish rework and return to quarantine - simulation'),
        ('scrap', 'Scrap controlled unit - simulation'), ('plan_replacement', 'Approve replacement plan - simulation'),
        ('reschedule', 'Reschedule intervention / replacement - simulation'),
    ], required=True, default='hold')
    reason = fields.Text(required=True)
    planned_start = fields.Datetime(default=fields.Datetime.now)

    def action_record(self):
        self.ensure_one()
        case = self.case_id
        if self.operation == 'hold':
            case.action_hold(self.reason)
        elif self.operation == 'release':
            case.action_release(self.reason)
        elif self.operation == 'start_rework':
            case.action_start_rework(self.reason, self.planned_start)
        elif self.operation == 'finish_rework':
            case.action_finish_rework(self.reason)
        elif self.operation == 'scrap':
            case.action_scrap(self.reason)
        elif self.operation == 'plan_replacement':
            case.action_plan_replacement(self.reason, self.planned_start)
        elif self.operation == 'reschedule':
            case.action_reschedule(self.reason, self.planned_start)
        return {'type': 'ir.actions.act_window_close'}
