from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..core import HASH_RE
from .guards import require_company, require_manager


class Machine(models.Model):
    _name = "iot.poc.machine"
    _description = "Demonstration machine alias"
    _check_company_auto = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    mapping_revision = fields.Integer(default=0, readonly=True, copy=False)
    mapping_ids = fields.One2many("iot.poc.mapping", "machine_id", readonly=True)
    _company_code_unique = models.Constraint("UNIQUE(company_id, code)", "Machine code must be unique within a company.")

    def _touch(self):
        # Ingress and configuration mutations UPDATE the same tuple. A racing
        # REPEATABLE READ transaction must restart rather than use old mappings.
        self.flush_recordset(["mapping_revision"])
        for record in self.sorted("id"):
            self.env.cr.execute("UPDATE iot_poc_machine SET mapping_revision = mapping_revision + 1 WHERE id = %s", [record.id])
        self.invalidate_recordset(["mapping_revision"])

    @api.constrains("name", "code")
    def _check_names(self):
        for record in self:
            if not record.name.strip() or not record.code.strip() or record.code != record.code.strip() or len(record.code) > 128 or "|" in record.code:
                raise ValidationError("Use a nonempty machine name and an unambiguous code up to 128 characters.")

    @api.model_create_multi
    def create(self, vals_list):
        require_manager(self)
        if any("mapping_revision" in vals for vals in vals_list):
            raise AccessError("The revision is managed internally.")
        records = super().create(vals_list)
        require_company(records)
        return records

    def write(self, vals):
        require_manager(self)
        self.check_access("write")
        require_company(self)
        if "mapping_revision" in vals:
            raise AccessError("The revision is managed internally.")
        self._touch()
        if self.env["iot.poc.event"].sudo().search_count([("machine_id", "in", self.ids)]):
            raise ValidationError("A machine used by an event is frozen; retain its provenance.")
        if {"company_id", "code"} & vals.keys() and self.mapping_ids:
            raise ValidationError("A machine with mappings cannot change identity or company.")
        result = super().write(vals)
        require_company(self)
        return result

    def unlink(self):
        require_manager(self)
        self.check_access("unlink")
        require_company(self)
        self._touch()
        if self.mapping_ids:
            raise ValidationError("Remove unused mappings before removing a machine.")
        return super().unlink()


class Mapping(models.Model):
    _name = "iot.poc.mapping"
    _description = "Explicit demonstration cycle-to-unit mapping"
    _check_company_auto = True

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    machine_id = fields.Many2one("iot.poc.machine", required=True, ondelete="restrict", check_company=True)
    production_id = fields.Many2one("mrp.production", required=True, ondelete="restrict", check_company=True)
    product_id = fields.Many2one("product.product", required=True, ondelete="restrict", check_company=True)
    lot_id = fields.Many2one("stock.lot", required=True, ondelete="restrict", check_company=True)
    dataset_sha256 = fields.Char(required=True, index=True)
    cycle_label = fields.Integer(required=True)
    mapping_kind = fields.Selection([("demonstration", "Demonstration ERP context")], required=True, default="demonstration")
    historical_mapping_verified = fields.Boolean(default=False, readonly=True,
        help="False: the historical machine identifier and historical ERP order linkage have not been established.")
    _code_unique = models.Constraint("UNIQUE(company_id, code)", "Mapping code must be unique within a company.")
    _cycle_unique = models.Constraint("UNIQUE(company_id, machine_id, dataset_sha256, cycle_label)", "A dataset cycle has one mapping.")
    _production_unique = models.Constraint("UNIQUE(production_id)", "One demonstration unit/MO cannot represent several cycles.")
    _positive_label = models.Constraint("CHECK(cycle_label > 0)", "Cycle label must be positive.")
    _demonstration_only = models.Constraint("CHECK(historical_mapping_verified IS NOT TRUE)", "This mapping does not assert a historical ERP relationship.")

    def _validate_links(self):
        units = self.env.ref("uom.product_uom_unit")
        for record in self:
            mo, product, lot = record.production_id, record.product_id, record.lot_id
            if (record.mapping_kind != "demonstration" or record.historical_mapping_verified
                    or record.machine_id.company_id != record.company_id or mo.company_id != record.company_id
                    or product.company_id and product.company_id != record.company_id
                    or lot.company_id and lot.company_id != record.company_id
                    or mo.product_id != product or lot.product_id != product
                    or mo.uom_id != units or product.uom_id != units or abs(mo.product_qty - 1) > 1e-9
                    or mo.state in ("draft", "cancel") or mo.lot_producing_ids != lot
                    or not product.is_storable or product.tracking not in ("lot", "serial")):
                raise ValidationError("Mapping needs a confirmed one-unit MO with the same product, company and explicitly assigned lot.")

    @api.constrains("name", "code", "company_id", "machine_id", "production_id", "product_id", "lot_id", "dataset_sha256", "cycle_label", "mapping_kind", "historical_mapping_verified")
    def _check_mapping(self):
        for record in self:
            if not record.name.strip() or not record.code or record.code != record.code.strip() or len(record.code) > 128 or "|" in record.code:
                raise ValidationError("Use a nonempty name and mapping code up to 128 characters.")
            if not record.dataset_sha256 or not HASH_RE.fullmatch(record.dataset_sha256):
                raise ValidationError("Register the full lowercase dataset SHA-256 digest.")
        self._validate_links()

    @api.model_create_multi
    def create(self, vals_list):
        require_manager(self)
        # Resolve no machine after locking: context defaults cannot bypass the
        # same machine-row serialization used by ingestion and updates.
        if any(type(vals.get("machine_id")) is not int or vals["machine_id"] <= 0 for vals in vals_list):
            raise ValidationError("An explicit machine_id is required when creating a mapping.")
        machines = self.env["iot.poc.machine"].browse(sorted({vals["machine_id"] for vals in vals_list})).exists()
        machines.check_access("read")
        require_company(machines)
        machines._touch()
        records = super().create(vals_list)
        require_company(records)
        return records

    def write(self, vals):
        require_manager(self)
        self.check_access("write")
        require_company(self)
        machines = self.machine_id
        if "machine_id" in vals:
            if type(vals["machine_id"]) is not int or vals["machine_id"] <= 0:
                raise ValidationError("An explicit machine_id is required.")
            machines |= self.env["iot.poc.machine"].browse(vals["machine_id"])
        machines.check_access("read")
        require_company(machines)
        machines._touch()
        if self.env["iot.poc.event"].sudo().search_count([("mapping_id", "in", self.ids)]):
            raise ValidationError("Mappings used by an event are immutable.")
        result = super().write(vals)
        require_company(self)
        return result

    def unlink(self):
        require_manager(self)
        self.check_access("unlink")
        require_company(self)
        self.machine_id._touch()
        if self.env["iot.poc.event"].sudo().search_count([("mapping_id", "in", self.ids)]):
            raise ValidationError("Mappings used by an event cannot be deleted.")
        return super().unlink()
