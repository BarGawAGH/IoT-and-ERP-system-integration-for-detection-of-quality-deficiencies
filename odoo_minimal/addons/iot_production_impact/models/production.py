from odoo import fields, models
from odoo.exceptions import UserError


class ManufacturingOrder(models.Model):
    _inherit = 'mrp.production'

    iot_impact_case_ids = fields.One2many('iot.impact.case', 'production_id', readonly=True)
    iot_impact_origin_case_id = fields.Many2one('iot.impact.case', readonly=True, copy=False,
                                             ondelete='restrict', check_company=True)
    iot_impact_hold = fields.Boolean(compute='_compute_iot_impact_hold')

    def _compute_iot_impact_hold(self):
        for production in self:
            production.iot_impact_hold = bool(production.iot_impact_case_ids.filtered('completion_hold'))

    def _button_mark_done_sanity_checks(self):
        # No state value is added to Odoo's standard manufacturing state machine.
        # Historical unresolved cases block completion; simulation stock is recorded
        # into quarantine first, then separately governed by the lot release gate.
        cases = self.env['iot.impact.case'].sudo().search([
            ('production_id', 'in', self.ids), ('evidence_mode', '=', 'historical'), ('state', '=', 'held'),
        ])
        if cases:
            raise UserError('IOT_PRODUCTION_HOLD: unresolved historical case %s prevents completion of its manufacturing order. A workflow-only review does not establish product quality.' %
                            ', '.join(cases.mapped('code')))
        return super()._button_mark_done_sanity_checks()

    def action_open_iot_impact_cases(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': 'Production impact cases',
                'res_model': 'iot.impact.case', 'view_mode': 'list,form',
                'domain': ['|', ('production_id', '=', self.id), ('replacement_production_id', '=', self.id)]}
