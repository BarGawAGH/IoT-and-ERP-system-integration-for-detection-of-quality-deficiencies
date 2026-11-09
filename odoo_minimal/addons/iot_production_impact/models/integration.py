from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .guards import trusted, require_manager, require_company


class ImpactConfiguration(models.Model):
    _name = 'iot.impact.configuration'
    _description = 'Company production-impact activation'
    _check_company_auto = True
    _rec_name = 'company_id'

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    warehouse_id = fields.Many2one('stock.warehouse', required=True, check_company=True)
    quarantine_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    rework_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    available_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    consumption_location_id = fields.Many2one('stock.location', required=True, check_company=True)
    responsible_user_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user)
    _company_unique = models.Constraint('UNIQUE(company_id)', 'One production-impact configuration is allowed per company.')

    @api.model_create_multi
    def create(self, vals_list):
        require_manager(self)
        result = super().create(vals_list)
        require_company(result)
        return result

    def write(self, vals):
        require_manager(self)
        require_company(self)
        result = super().write(vals)
        require_company(self)
        return result

    @api.constrains('responsible_user_id', 'company_id', 'warehouse_id', 'quarantine_location_id', 'rework_location_id',
                    'available_location_id', 'consumption_location_id')
    def _validate_config(self):
        for config in self:
            user = config.responsible_user_id
            if not user.has_group('iot_production_impact.group_impact_manager') or user.has_group('iot_poc_community.group_bridge'):
                raise ValidationError('The responsible user must be a production-impact manager, not a bridge account.')
            if config.company_id not in user.company_ids or config.warehouse_id.company_id != config.company_id:
                raise ValidationError('Configuration, responsible user and warehouse companies must match.')
            locations = config.quarantine_location_id | config.rework_location_id | config.available_location_id
            if len(locations) != 3 or any(loc.usage != 'internal' for loc in locations):
                raise ValidationError('Three distinct internal stock locations are required.')
            if config.consumption_location_id.usage != 'production':
                raise ValidationError('Material consumption must use a virtual Production location.')
            for location in (config.quarantine_location_id, config.rework_location_id):
                ancestors = (location.parent_path or '').split('/')
                if str(config.available_location_id.id) in ancestors or str(config.warehouse_id.lot_stock_id.id) in ancestors:
                    raise ValidationError('Restricted locations must be outside available stock.')

    def _ensure_historical_case(self, event):
        """Private, bounded transition after the original API has authenticated the event."""
        self.ensure_one()
        event.ensure_one()
        if not self.active or event.company_id != self.company_id or event.triage != 'review_required':
            return self.env['iot.impact.case']
        self.env.cr.execute('SELECT id FROM iot_impact_configuration WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if not self.active:
            return self.env['iot.impact.case']
        # Use the explicit configured responsible manager, never let the bridge
        # choose a person, company, location, disposition or arbitrary record.
        env_cases = self.env['iot.impact.case'].with_user(self.responsible_user_id).with_context(
            allowed_company_ids=[self.company_id.id], lang='en_US')
        cases = env_cases.search([('source_event_id', '=', event.id), ('evidence_mode', '=', 'historical')])
        if cases:
            if len(cases) != 1:
                raise ValidationError('Ambiguous historical case mapping.')
            return cases
        case = env_cases.create({
            'name': 'Historical cycle %s - unresolved production review' % event.cycle_label,
            'code': 'HIST-%s-%s' % (event.cycle_label, event.event_id.removeprefix('poc-v1:')[:12]),
            'evidence_mode': 'historical', 'scenario_description':
                'Automatic historical triage. No independent product inspection is available; source quality is unchanged.',
            'company_id': self.company_id.id, 'source_event_id': event.id,
            'production_id': event.production_id.id, 'lot_id': event.lot_id.id, 'quantity': 1,
            'warehouse_id': self.warehouse_id.id,
            'quarantine_location_id': self.quarantine_location_id.id,
            'rework_location_id': self.rework_location_id.id,
            'available_location_id': self.available_location_id.id,
            'consumption_location_id': self.consumption_location_id.id,
            'responsible_user_id': self.responsible_user_id.id,
            'planned_start': event.production_id.date_start or fields.Datetime.now(),
        })
        case.action_hold('Automatically created from historical event %s. A workflow-only review does not verify the physical product.' % event.event_id)
        return case

    def synchronize_historical(self):
        require_manager(self)
        require_company(self)
        count = 0
        for config in self:
            events = self.env['iot.poc.event'].search([('company_id', '=', config.company_id.id),
                                                     ('source_kind', '=', 'replay'), ('triage', '=', 'review_required')])
            for event in events:
                case = config._ensure_historical_case(event)
                count += bool(case)
        return count


class SourceEvent(models.Model):
    _inherit = 'iot.poc.event'

    @api.model
    def ingest_event(self, payload, company_id=None):
        receipt = super().ingest_event(payload, company_id=company_id)
        # The original method validates the bridge, company, immutable content
        # and mapping. This hook cannot override those validations or set QC.
        event = self.sudo().browse(receipt['record_id']).exists()
        if event and event.company_id.id == company_id and event.triage == 'review_required':
            configurations = self.env['iot.impact.configuration'].sudo().search([
                ('company_id', '=', company_id), ('active', '=', True)], limit=2)
            if len(configurations) > 1:
                raise ValidationError('Ambiguous production-impact configuration.')
            if configurations:
                configurations._ensure_historical_case(event)
        return receipt
