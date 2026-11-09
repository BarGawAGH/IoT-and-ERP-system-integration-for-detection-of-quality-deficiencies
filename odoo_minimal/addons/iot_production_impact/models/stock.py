from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .guards import is_internal


HELD_STATES = ('held', 'rework', 'reinspection', 'scrapped', 'replacement_planned')
MOVE_STOCK_FIELDS = {'state', 'product_id', 'uom_id', 'product_uom_qty', 'quantity', 'picked',
                     'location_id', 'location_dest_id', 'company_id', 'move_line_ids', 'lot_ids',
                     'is_scrap', 'is_inventory', 'date'}
LINE_STOCK_FIELDS = {'move_id', 'state', 'product_id', 'uom_id', 'quantity', 'picked', 'lot_id',
                     'location_id', 'location_dest_id', 'company_id', 'owner_id', 'package_id',
                     'result_package_id', 'quant_id'}


def held_cases(env, lot_ids, company_ids):
    """Read only the control state with elevated access; never execute stock as sudo."""
    if not lot_ids or not company_ids:
        return env['iot.impact.case']
    cases = env['iot.impact.case'].sudo().search([
        ('lot_id', 'in', list(lot_ids)), ('company_id', 'in', list(company_ids)),
        ('evidence_mode', '=', 'simulation'), ('state', 'in', list(HELD_STATES)),
    ], order='id')
    if cases:
        env.cr.execute('SELECT id FROM iot_impact_case WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(cases.ids)])
        cases.invalidate_recordset(['state'])
    return cases.filtered(lambda c: c.state in HELD_STATES)


def held_error(case):
    raise UserError('IOT_RELEASE_HOLD: lot %s is controlled by case %s. Record an authorised disposition in Production Impact before moving it or changing its completed stock history.' %
                    (case.lot_id.name, case.code))


class StockMove(models.Model):
    _inherit = 'stock.move'

    iot_impact_case_id = fields.Many2one('iot.impact.case', readonly=True, copy=False, ondelete='restrict', check_company=True)
    iot_impact_action_key = fields.Char(readonly=True, copy=False)
    _impact_action_unique = models.Constraint('UNIQUE(iot_impact_case_id, iot_impact_action_key)',
                                              'A case action creates one stock movement.')

    @api.model_create_multi
    def create(self, vals_list):
        if not is_internal(self) and any(v.get('iot_impact_case_id') or v.get('iot_impact_action_key') for v in vals_list):
            raise AccessError('Case stock links can only be assigned by an authorised disposition.')
        return super().create(vals_list)

    def write(self, vals):
        if not is_internal(self) and {'iot_impact_case_id', 'iot_impact_action_key'}.intersection(vals):
            raise AccessError('Case stock links cannot be changed directly.')
        if not is_internal(self) and MOVE_STOCK_FIELDS.intersection(vals):
            done = self.filtered(lambda move: move.state == 'done')
            if done.filtered('iot_impact_case_id'):
                raise UserError('Completed Production Impact stock documents cannot be rewritten.')
            done.move_line_ids._check_impact_history()
        if vals.get('state') == 'done':
            self._check_impact_release()
        return super().write(vals)

    def unlink(self):
        if not is_internal(self):
            if self.filtered(lambda move: move.state == 'done' and move.iot_impact_case_id):
                raise UserError('Completed Production Impact stock documents cannot be deleted.')
            self.filtered(lambda move: move.state == 'done').move_line_ids._check_impact_history()
        return super().unlink()

    def _check_impact_release(self):
        if is_internal(self):
            return
        # Guard the detailed lot, including inbound adjustments that could put a
        # held lot into available stock. Initial manufacturing is posted into
        # quarantine before this prototype can activate its simulation hold.
        lines = self.move_line_ids.filtered(lambda line: line.quantity > 0 and line.lot_id)
        if not lines:
            return
        cases = held_cases(self.env, lines.lot_id.ids, lines.company_id.ids)
        for case in cases:
            if lines.filtered(lambda line: line.lot_id == case.lot_id and line.company_id == case.company_id):
                held_error(case)

    def _action_done(self, cancel_backorder=False):
        self._check_impact_release()
        return super()._action_done(cancel_backorder=cancel_backorder)


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    def _check_impact_history(self, vals=None):
        if is_internal(self):
            return
        vals = vals or {}
        target_move = self.env['stock.move'].browse(vals.get('move_id')).exists() if vals.get('move_id') else self.env['stock.move']
        done = self.filtered(lambda line: line.move_id.state == 'done' or target_move.state == 'done')
        if not done and target_move.state != 'done':
            return
        if done.move_id.filtered('iot_impact_case_id') or target_move.iot_impact_case_id:
            raise UserError('Completed Production Impact stock documents cannot be rewritten.')
        pairs = {(line.lot_id.id, line.company_id.id) for line in done if line.lot_id}
        for line in done:
            lot_id = vals.get('lot_id', line.lot_id.id)
            company_id = vals.get('company_id', line.company_id.id)
            if lot_id and company_id:
                pairs.add((lot_id, company_id))
        if not self and target_move.state == 'done':
            lot_id = vals.get('lot_id')
            company_id = vals.get('company_id') or target_move.company_id.id
            if lot_id:
                pairs.add((lot_id, company_id))
        for case in held_cases(self.env, {p[0] for p in pairs}, {p[1] for p in pairs}):
            if (case.lot_id.id, case.company_id.id) in pairs:
                held_error(case)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_impact_history(vals)
        return super().create(vals_list)

    def write(self, vals):
        if LINE_STOCK_FIELDS.intersection(vals):
            self._check_impact_history(vals)
        return super().write(vals)

    def unlink(self):
        self._check_impact_history()
        return super().unlink()


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'
    iot_impact_case_id = fields.Many2one('iot.impact.case', readonly=True, copy=False, ondelete='restrict', check_company=True)


class StockLot(models.Model):
    _inherit = 'stock.lot'
    iot_impact_case_ids = fields.One2many('iot.impact.case', 'lot_id', readonly=True)
