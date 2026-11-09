import json

from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError

from .guards import is_internal, trusted, require_manager, require_company, reason_text


STATES = [
    ('new', 'New'), ('held', 'On hold'), ('rework', 'Rework in progress'),
    ('reinspection', 'Awaiting reinspection'), ('released', 'Released in simulation'),
    ('scrapped', 'Scrapped in simulation'), ('replacement_planned', 'Replacement planned'),
]


class ImpactCase(models.Model):
    _name = 'iot.impact.case'
    _description = 'Production impact case'
    _order = 'code, id'
    _check_company_auto = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True, copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    evidence_mode = fields.Selection([('historical', 'Historical replay - unresolved'),
                                      ('simulation', 'Explicit workflow simulation')], required=True, default='historical')
    scenario_description = fields.Text(required=True)
    source_event_id = fields.Many2one('iot.poc.event', check_company=True, ondelete='restrict')
    source_cycle_label = fields.Integer(related='source_event_id.cycle_label', readonly=True)
    source_review_outcome = fields.Selection(related='source_event_id.review_outcome', readonly=True)
    independent_quality = fields.Selection(related='source_event_id.independent_quality', readonly=True)
    production_id = fields.Many2one('mrp.production', required=True, check_company=True, ondelete='restrict')
    product_id = fields.Many2one('product.product', related='production_id.product_id', store=True, readonly=True)
    lot_id = fields.Many2one('stock.lot', required=True, check_company=True, ondelete='restrict')
    quantity = fields.Float(required=True, default=1, digits='Product Unit')
    warehouse_id = fields.Many2one('stock.warehouse', required=True, check_company=True)
    quarantine_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    rework_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    available_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    consumption_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    responsible_user_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user)
    supplier_id = fields.Many2one('res.partner', check_company=True)
    rework_product_id = fields.Many2one('product.product', check_company=True)
    rework_quantity = fields.Float(default=1, digits='Product Unit')
    planned_start = fields.Datetime(required=True, default=fields.Datetime.now)
    original_planned_start = fields.Datetime(readonly=True)
    state = fields.Selection(STATES, default='new', required=True, readonly=True, index=True)
    hold_reason = fields.Text(readonly=True)
    held_at = fields.Datetime(readonly=True)
    released_at = fields.Datetime(readonly=True)
    replacement_production_id = fields.Many2one('mrp.production', readonly=True, check_company=True, ondelete='restrict')
    purchase_order_id = fields.Many2one('purchase.order', readonly=True, check_company=True, ondelete='restrict')
    activity_id = fields.Many2one('mail.activity', readonly=True, ondelete='set null')
    activity_record_id = fields.Integer(readonly=True, help='Original task ID retained after task completion.')
    activity_created_at = fields.Datetime(readonly=True)
    activity_closed_at = fields.Datetime(readonly=True)
    move_ids = fields.One2many('stock.move', 'iot_impact_case_id', readonly=True)
    audit_ids = fields.One2many('iot.impact.audit', 'case_id', readonly=True)
    material_ids = fields.One2many('iot.impact.material', 'case_id', readonly=True)
    completion_hold = fields.Boolean(compute='_compute_completion_hold', store=True)
    quarantine_quantity = fields.Float(compute='_compute_quantities', digits='Product Unit')
    rework_stock_quantity = fields.Float(compute='_compute_quantities', digits='Product Unit')
    available_quantity = fields.Float(compute='_compute_quantities', digits='Product Unit')

    _code_company_unique = models.Constraint('UNIQUE(company_id, code)', 'A case code is unique in a company.')
    _controlled_lot_unique = models.Constraint('UNIQUE(company_id, lot_id, evidence_mode)', 'A lot has one case per evidence mode.')
    _positive_quantity = models.Constraint('CHECK(quantity > 0)', 'The controlled quantity must be positive.')

    @api.depends('evidence_mode', 'state')
    def _compute_completion_hold(self):
        for case in self:
            case.completion_hold = case.evidence_mode == 'historical' and case.state == 'held'

    def _compute_quantities(self):
        for case in self:
            case.quarantine_quantity = case._available(case.product_id, case.quarantine_location_id, case.lot_id)
            case.rework_stock_quantity = case._available(case.product_id, case.rework_location_id, case.lot_id)
            case.available_quantity = case._available(case.product_id, case.available_location_id, case.lot_id)

    @api.model_create_multi
    def create(self, vals_list):
        if not is_internal(self):
            require_manager(self)
        for vals in vals_list:
            if any(k in vals for k in ('state', 'audit_ids', 'move_ids', 'material_ids', 'held_at', 'released_at',
                                      'hold_reason', 'replacement_production_id', 'purchase_order_id',
                                      'activity_id', 'activity_record_id', 'activity_created_at',
                                      'activity_closed_at', 'original_planned_start')):
                raise AccessError('Case state and generated records can only be changed by audited actions.')
            vals.setdefault('original_planned_start', vals.get('planned_start') or fields.Datetime.now())
        result = super().create(vals_list)
        require_company(result)
        return result

    def write(self, vals):
        if is_internal(self):
            return super().write(vals)
        require_manager(self)
        require_company(self)
        if any(c.state != 'new' for c in self):
            raise AccessError('Active cases are immutable except through audited disposition actions.')
        forbidden = {'state', 'audit_ids', 'move_ids', 'material_ids', 'held_at', 'released_at', 'hold_reason',
                     'replacement_production_id', 'purchase_order_id', 'activity_record_id', 'activity_id',
                     'activity_created_at', 'activity_closed_at', 'original_planned_start'}
        if forbidden.intersection(vals):
            raise AccessError('Use an audited disposition action.')
        result = super().write(vals)
        require_company(self)
        return result

    def unlink(self):
        raise AccessError('Production impact cases and their audit history cannot be deleted.')

    @api.constrains('evidence_mode', 'source_event_id', 'production_id', 'lot_id', 'quantity', 'warehouse_id',
                    'quarantine_location_id', 'rework_location_id', 'available_location_id',
                    'consumption_location_id', 'company_id', 'responsible_user_id')
    def _validate_case(self):
        for c in self:
            if c.lot_id.product_id != c.product_id or c.lot_id.company_id != c.company_id:
                raise ValidationError('The controlled lot must belong to the manufacturing product and company.')
            if c.production_id.company_id != c.company_id or c.warehouse_id.company_id != c.company_id:
                raise ValidationError('The manufacturing order and warehouse must belong to the case company.')
            if c.product_id.uom_id.compare(c.quantity, 1) != 0:
                raise ValidationError('This prototype controls exactly one unit per case.')
            if c.responsible_user_id.has_group('iot_poc_community.group_bridge'):
                raise ValidationError('The integration bridge cannot own a disposition task.')
            if c.company_id not in c.responsible_user_id.company_ids:
                raise ValidationError('The responsible user must have access to the case company.')
            locations = c.quarantine_location_id | c.rework_location_id | c.available_location_id
            if len(locations) != 3 or any(loc.usage != 'internal' for loc in locations):
                raise ValidationError('Available, quarantine and rework locations must be distinct internal locations.')
            for restricted in (c.quarantine_location_id, c.rework_location_id):
                ancestors = (restricted.parent_path or '').split('/')
                if str(c.available_location_id.id) in ancestors or str(c.warehouse_id.lot_stock_id.id) in ancestors:
                    raise ValidationError('Quarantine and rework must be outside the warehouse available-stock subtree.')
            if c.consumption_location_id.usage != 'production':
                raise ValidationError('Rework material consumption needs a virtual Production location.')
            if c.evidence_mode == 'historical':
                if not c.source_event_id or c.source_event_id.triage != 'review_required':
                    raise ValidationError('A historical case requires a source event that needs review.')
                if c.source_event_id.production_id != c.production_id or c.source_event_id.lot_id != c.lot_id:
                    raise ValidationError('Historical cases must preserve the source manufacturing and lot links.')
            else:
                if not (c.product_id.default_code or '').startswith('IMP-'):
                    raise ValidationError('Workflow simulations are restricted to isolated IMP- demonstration products.')

    def _lock(self, simulation=False):
        self.ensure_one()
        require_manager(self)
        require_company(self)
        self.check_access('write')
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM iot_impact_case WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if simulation and self.evidence_mode != 'simulation':
            raise UserError('Historical quality is unverified. Stock dispositions are only allowed in explicitly marked workflow simulations.')

    def _has_action(self, key):
        return bool(self.env['iot.impact.audit'].search_count([('case_id', '=', self.id), ('action_key', '=', key)]))

    def _record(self, key, reason, before, values=None):
        self.ensure_one()
        trusted(self.env['iot.impact.audit']).create({
            'case_id': self.id, 'company_id': self.company_id.id, 'action_key': key,
            'recorded_at': fields.Datetime.now(), 'actor_id': self.env.uid,
            'evidence_mode': self.evidence_mode, 'state_before': before, 'state_after': self.state,
            'reason': reason_text(reason), 'details_json': json.dumps(values or {}, sort_keys=True, default=str),
        })

    def _available(self, product, location, lot=None):
        if not product or not location:
            return 0.0
        return self.env['stock.quant']._get_available_quantity(product, location, lot_id=lot or None, strict=True)

    def _need_stock(self, product, location, quantity, lot=None):
        actual = self._available(product, location, lot)
        if product.uom_id.compare(actual, quantity) < 0:
            raise UserError('Insufficient unreserved stock in %s for %s: need %s, available %s.' %
                            (location.display_name, product.display_name, quantity, actual))
        return actual

    def _create_activity(self):
        if self.activity_record_id:
            return
        activity = trusted(self.env['mail.activity']).create({
            'res_model_id': self.env['ir.model']._get_id('mrp.production'),
            'res_id': self.production_id.id,
            'activity_type_id': self.env.ref('mail.mail_activity_data_todo').id,
            'user_id': self.responsible_user_id.id,
            'date_deadline': fields.Date.context_today(self),
            'summary': 'Resolve IoT production case %s' % self.code,
            'note': ('Historical source quality remains unverified. ' if self.evidence_mode == 'historical'
                     else 'Explicit workflow simulation; no independent physical inspection. ') +
                    'Open Production Impact and review case %s.' % self.code,
        })
        trusted(self).write({'activity_id': activity.id, 'activity_record_id': activity.id,
                             'activity_created_at': fields.Datetime.now()})

    def _close_activity(self):
        # Keep the closure in our immutable audit, without posting chatter or emailing followers.
        if self.activity_id:
            self.activity_id.unlink()
        if self.activity_record_id and not self.activity_closed_at:
            trusted(self).write({'activity_id': False, 'activity_closed_at': fields.Datetime.now()})

    def _hold_locked(self, reason):
        if self._has_action('hold'):
            return self
        if self.state != 'new':
            raise UserError('Only a new case can be placed on hold.')
        if self.evidence_mode == 'simulation':
            if self.production_id.state != 'done':
                raise UserError('First record the simulated manufacturing output into quarantine.')
            self._need_stock(self.product_id, self.quarantine_location_id, self.quantity, self.lot_id)
        elif self.production_id.state in ('done', 'cancel'):
            raise UserError('A historical order is already completed or cancelled. This prototype cannot retroactively hold or undo its execution.')
        before = self.state
        trusted(self).write({'state': 'held', 'hold_reason': reason_text(reason), 'held_at': fields.Datetime.now()})
        self._create_activity()
        self._record('hold', reason, before, {'task_id': self.activity_record_id,
                     'production_id': self.production_id.id, 'source_event_id': self.source_event_id.id or None})
        return self

    def action_hold(self, reason):
        self._lock()
        return self._hold_locked(reason_text(reason))

    def _move(self, key, product, qty, source, destination, lot=None, scrap=False):
        self.ensure_one()
        existing = self.env['stock.move'].search([('iot_impact_case_id', '=', self.id), ('iot_impact_action_key', '=', key)])
        if existing:
            if len(existing) != 1 or existing.state != 'done':
                raise UserError('An incomplete or ambiguous movement already exists for this action.')
            return existing
        self._need_stock(product, source, qty, lot)
        line = {'company_id': self.company_id.id, 'product_id': product.id, 'uom_id': product.uom_id.id,
                'quantity': qty, 'picked': True, 'location_id': source.id, 'location_dest_id': destination.id}
        if lot:
            line['lot_id'] = lot.id
        vals = {'company_id': self.company_id.id, 'product_id': product.id, 'uom_id': product.uom_id.id,
                'product_uom_qty': qty, 'location_id': source.id, 'location_dest_id': destination.id,
                'origin': self.code, 'reference': '%s/%s' % (self.code, key), 'picked': True,
                'iot_impact_case_id': self.id, 'iot_impact_action_key': key,
                'move_line_ids': [Command.create(line)]}
        if scrap:
            vals.update({'is_scrap': True, 'should_replenish_scrapped': False})
        move = trusted(self.env['stock.move']).create(vals)
        move._action_confirm(merge=False, create_proc=False)
        move.write({'picked': True})
        move.move_line_ids.write({'picked': True})
        if scrap:
            move.action_scrap()
        else:
            move._action_done()
        if move.state != 'done':
            raise UserError('The stock operation did not finish; no disposition has been committed.')
        return move

    def action_release(self, reason):
        self._lock(simulation=True)
        reason = reason_text(reason)
        if self._has_action('release'):
            return self
        if self.state not in ('held', 'reinspection'):
            raise UserError('Release requires a held case or completed rework awaiting a new decision.')
        before = self.state
        move = self._move('release', self.product_id, self.quantity, self.quarantine_location_id,
                          self.available_location_id, self.lot_id)
        trusted(self).write({'state': 'released', 'released_at': fields.Datetime.now()})
        self._close_activity()
        self._record('release', reason, before, {'stock_move_id': move.id, 'simulation_decision': True,
                      'independent_inspection_performed': False})
        return self

    def action_start_rework(self, reason, planned_start=None):
        self._lock(simulation=True)
        reason = reason_text(reason)
        if self._has_action('start_rework'):
            return self
        if self.state != 'held':
            raise UserError('Rework can only start from a held simulation case.')
        if not self.rework_product_id or self.rework_quantity <= 0:
            raise UserError('Configure the additional material and positive quantity for rework.')
        date = fields.Datetime.to_datetime(planned_start) if planned_start else self.planned_start
        if not date:
            raise ValidationError('A planned start is required.')
        before = self.state
        move = self._move('to_rework', self.product_id, self.quantity, self.quarantine_location_id,
                          self.rework_location_id, self.lot_id)
        trusted(self).write({'state': 'rework', 'planned_start': date})
        self._record('start_rework', reason, before, {'stock_move_id': move.id, 'planned_start': date,
                     'extra_component_id': self.rework_product_id.id, 'extra_quantity': self.rework_quantity})
        return self

    def action_finish_rework(self, reason):
        self._lock(simulation=True)
        reason = reason_text(reason)
        if self._has_action('finish_rework'):
            return self
        if self.state != 'rework':
            raise UserError('Finish rework only after it has started.')
        before = self.state
        before_available = self._need_stock(self.rework_product_id, self.available_location_id, self.rework_quantity)
        material_move = self._move('rework_consumption', self.rework_product_id, self.rework_quantity,
                                   self.available_location_id, self.consumption_location_id)
        return_move = self._move('rework_to_quarantine', self.product_id, self.quantity,
                                 self.rework_location_id, self.quarantine_location_id, self.lot_id)
        trusted(self.env['iot.impact.material']).create({
            'case_id': self.id, 'company_id': self.company_id.id, 'phase': 'rework',
            'product_id': self.rework_product_id.id, 'uom_id': self.rework_product_id.uom_id.id,
            'required_quantity': self.rework_quantity, 'available_before': before_available,
            'reserved_quantity': 0, 'shortage_quantity': 0, 'consumed_quantity': material_move.quantity,
            'move_id': material_move.id,
        })
        trusted(self).write({'state': 'reinspection'})
        self._record('finish_rework', reason, before, {'material_move_id': material_move.id,
                      'return_move_id': return_move.id, 'additional_material_consumed': material_move.quantity,
                      'release_still_requires_separate_simulation_decision': True})
        return self

    def action_scrap(self, reason):
        self._lock(simulation=True)
        reason = reason_text(reason)
        if self._has_action('scrap'):
            return self
        if self.state not in ('held', 'reinspection'):
            raise UserError('Scrap requires a held case or completed rework awaiting disposition.')
        scrap_location = self.company_id.scrap_location_id
        if not scrap_location or scrap_location.usage != 'inventory':
            raise UserError('Configure the company scrap location before this simulation.')
        before = self.state
        move = self._move('scrap', self.product_id, self.quantity, self.quarantine_location_id,
                          scrap_location, self.lot_id, scrap=True)
        trusted(self).write({'state': 'scrapped'})
        self._close_activity()
        self._record('scrap', reason, before, {'stock_move_id': move.id, 'scrapped_quantity': move.quantity,
                     'simulation_decision': True, 'independent_inspection_performed': False})
        return self

    def action_plan_replacement(self, reason, planned_start=None):
        self._lock(simulation=True)
        reason = reason_text(reason)
        if self._has_action('plan_replacement'):
            return self
        if self.state != 'scrapped':
            raise UserError('A replacement requires an explicitly recorded scrap decision in this scenario.')
        date = fields.Datetime.to_datetime(planned_start) if planned_start else self.planned_start
        if not date or not self.production_id.bom_id:
            raise UserError('A planned start and original bill of materials are required.')
        origin = '%s/REPLACEMENT' % self.code
        if self.env['mrp.production'].search_count([('company_id', '=', self.company_id.id), ('origin', '=', origin)]):
            raise UserError('An unmanaged replacement reference already exists; reconcile it before retrying.')
        before = self.state
        production = trusted(self.env['mrp.production']).create({
            'company_id': self.company_id.id, 'product_id': self.product_id.id,
            'product_qty': self.quantity, 'uom_id': self.product_id.uom_id.id,
            'bom_id': self.production_id.bom_id.id, 'picking_type_id': self.warehouse_id.manu_type_id.id,
            'location_src_id': self.available_location_id.id, 'location_dest_id': self.quarantine_location_id.id,
            'date_start': date, 'origin': origin, 'iot_impact_origin_case_id': self.id,
        })
        production.action_confirm()
        production.action_assign()
        lines = []
        shortages = []
        for move in production.move_raw_ids.filtered(lambda m: m.state != 'cancel'):
            if move.product_id.tracking:
                raise UserError('The material-planning simulation expects untracked components.')
            # At this point quantity is the reservation on an unconsumed move in Odoo 20.
            reserved = move.uom_id._compute_quantity(move.quantity, move.product_id.uom_id)
            required = move.uom_id._compute_quantity(move.product_uom_qty, move.product_id.uom_id)
            free = self._available(move.product_id, self.available_location_id)
            shortage = max(0.0, required - reserved - free)
            material = trusted(self.env['iot.impact.material']).create({
                'case_id': self.id, 'company_id': self.company_id.id, 'phase': 'replacement',
                'product_id': move.product_id.id, 'uom_id': move.product_id.uom_id.id,
                'required_quantity': required, 'available_before': reserved + free,
                'reserved_quantity': reserved, 'shortage_quantity': shortage,
                'consumed_quantity': 0, 'move_id': move.id,
            })
            if move.product_id.uom_id.compare(shortage, 0) > 0:
                if not self.supplier_id:
                    raise UserError('A demonstration supplier is required to create a draft shortage request.')
                lines.append(Command.create({
                    'product_id': move.product_id.id, 'name': '%s - explicit simulation shortage' % move.product_id.display_name,
                    'product_qty': shortage, 'uom_id': move.product_id.uom_id.id,
                    'date_planned': date, 'price_unit': 0, 'tax_ids': [Command.clear()],
                }))
                shortages.append({'product_id': move.product_id.id, 'quantity': shortage, 'material_id': material.id})
        purchase = self.env['purchase.order']
        if lines:
            purchase = trusted(self.env['purchase.order']).create({
                'company_id': self.company_id.id, 'partner_id': self.supplier_id.id,
                'origin': origin, 'partner_ref': '%s - SIMULATION - NOT SENT' % self.code,
                'order_line': lines, 'iot_impact_case_id': self.id,
            })
            if purchase.state != 'draft':
                raise UserError('The shortage request must remain a draft RFQ.')
        trusted(self).write({'state': 'replacement_planned', 'replacement_production_id': production.id,
                             'purchase_order_id': purchase.id or False, 'planned_start': date})
        self._record('plan_replacement', reason, before, {'manufacturing_order_id': production.id,
                     'planned_start': date, 'purchase_order_id': purchase.id or None, 'shortages': shortages,
                     'purchase_sent': False, 'replacement_completed': False})
        return self

    def action_reschedule(self, reason, planned_start):
        self._lock(simulation=True)
        reason = reason_text(reason)
        date = fields.Datetime.to_datetime(planned_start)
        if not date:
            raise ValidationError('A new planned start is required.')
        key = 'reschedule:' + fields.Datetime.to_string(date)
        if self._has_action(key):
            return self
        if self.state not in ('held', 'rework', 'reinspection', 'replacement_planned'):
            raise UserError('Only an open intervention or replacement plan can be rescheduled.')
        before = self.state
        old_date = self.planned_start
        production = self.replacement_production_id
        if production:
            if production.state in ('done', 'cancel'):
                raise UserError('A completed or cancelled replacement cannot be rescheduled.')
            production.date_start = date
        # Keep the draft RFQ due date consistent with the approved simulation schedule.
        if self.purchase_order_id:
            if self.purchase_order_id.state != 'draft':
                raise UserError('The linked request is no longer an unsent draft; automatic rescheduling is disabled.')
            self.purchase_order_id.order_line.filtered(lambda l: not l.display_type).date_planned = date
        trusted(self).write({'planned_start': date})
        self._record(key, reason, before, {'old_planned_start': old_date, 'new_planned_start': date,
                     'manufacturing_order_id': production.id or None, 'schedule_is_optimised': False})
        return self

    def action_open_disposition(self):
        self.ensure_one()
        require_manager(self)
        return {'type': 'ir.actions.act_window', 'name': 'Record production disposition',
                'res_model': 'iot.impact.disposition', 'view_mode': 'form', 'target': 'new',
                'context': {'default_case_id': self.id, 'default_planned_start': self.planned_start}}


class ImpactAudit(models.Model):
    _name = 'iot.impact.audit'
    _description = 'Append-only production disposition audit'
    _order = 'id'
    _rec_name = 'action_key'
    case_id = fields.Many2one('iot.impact.case', required=True, readonly=True, ondelete='restrict')
    company_id = fields.Many2one('res.company', required=True, readonly=True)
    action_key = fields.Char(required=True, readonly=True)
    recorded_at = fields.Datetime(required=True, readonly=True)
    actor_id = fields.Many2one('res.users', required=True, readonly=True, ondelete='restrict')
    evidence_mode = fields.Selection([('historical', 'Historical unresolved case'), ('simulation', 'Workflow simulation')], required=True, readonly=True)
    state_before = fields.Char(readonly=True)
    state_after = fields.Char(readonly=True)
    reason = fields.Text(required=True, readonly=True)
    details_json = fields.Text(required=True, readonly=True)
    _action_unique = models.Constraint('UNIQUE(case_id, action_key)', 'A disposition action is recorded once.')

    @api.model_create_multi
    def create(self, vals_list):
        if not is_internal(self):
            raise AccessError('Audit entries are appended only by disposition actions.')
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError('Audit history cannot be rewritten.')

    def unlink(self):
        raise AccessError('Audit history cannot be deleted.')


class ImpactMaterial(models.Model):
    _name = 'iot.impact.material'
    _description = 'Observed production material effect'
    _order = 'case_id, phase, id'
    case_id = fields.Many2one('iot.impact.case', required=True, readonly=True, ondelete='restrict')
    company_id = fields.Many2one('res.company', required=True, readonly=True)
    phase = fields.Selection([('rework', 'Additional rework consumption'), ('replacement', 'Replacement material requirement')], required=True, readonly=True)
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    uom_id = fields.Many2one('uom.uom', required=True, readonly=True)
    required_quantity = fields.Float(readonly=True, digits='Product Unit')
    available_before = fields.Float(readonly=True, digits='Product Unit')
    reserved_quantity = fields.Float(readonly=True, digits='Product Unit')
    shortage_quantity = fields.Float(readonly=True, digits='Product Unit')
    consumed_quantity = fields.Float(readonly=True, digits='Product Unit')
    move_id = fields.Many2one('stock.move', required=True, readonly=True, ondelete='restrict')
    _move_phase_unique = models.Constraint('UNIQUE(case_id, phase, move_id)', 'A material effect is recorded once per stock movement.')

    @api.model_create_multi
    def create(self, vals_list):
        if not is_internal(self):
            raise AccessError('Material snapshots are created only by audited actions.')
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError('Observed material snapshots cannot be rewritten.')

    def unlink(self):
        raise AccessError('Observed material snapshots cannot be deleted.')
