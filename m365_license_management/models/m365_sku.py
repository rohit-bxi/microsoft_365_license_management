# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models


class M365Sku(models.Model):
    """Subscribed SKU of a tenant (license inventory line)."""
    _name = 'm365.sku'
    _description = 'Microsoft 365 Subscription (SKU)'
    _order = 'tenant_id, name'

    name = fields.Char(string="Product", compute='_compute_name', store=True)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True,
                                ondelete='cascade', index=True)
    sku_id = fields.Char(string="SKU ID", required=True, index=True,
                         help="Microsoft Graph skuId (GUID).")
    sku_part_number = fields.Char(string="SKU", required=True, index=True)
    product_id = fields.Many2one('m365.product', string="Catalogue Product",
                                 ondelete='restrict')
    enabled_units = fields.Integer(string="Total", readonly=True)
    consumed_units = fields.Integer(string="Assigned", readonly=True)
    suspended_units = fields.Integer(string="Suspended", readonly=True)
    warning_units = fields.Integer(string="Expiring", readonly=True)
    available_units = fields.Integer(string="Available", compute='_compute_available',
                                     store=True)
    utilization = fields.Float(string="Utilisation (%)", compute='_compute_available',
                               store=True, digits=(16, 1))
    status = fields.Selection([
        ('Enabled', 'Active'),
        ('Warning', 'Warning'),
        ('Suspended', 'Suspended'),
        ('Deleted', 'Deleted'),
        ('LockedOut', 'Locked Out'),
    ], string="Status", default='Enabled', readonly=True)
    applies_to = fields.Char(string="Applies To", readonly=True)
    last_refresh = fields.Datetime(string="Last Refresh", readonly=True)
    low_alert_sent = fields.Boolean(readonly=True, copy=False)
    category = fields.Selection(related='product_id.category', store=True)
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)
    user_ids = fields.Many2many('m365.license.user', 'm365_license_user_sku_rel',
                                'sku_id', 'user_id', string="Assigned Users")
    user_count = fields.Integer(compute='_compute_user_count', string="Users")
    snapshot_ids = fields.One2many('m365.inventory.snapshot', 'sku_id', string="History")

    _tenant_sku_uniq = models.Constraint(
        'unique(tenant_id, sku_id)', 'This subscription already exists for the tenant.')

    @api.depends('product_id.name', 'sku_part_number')
    def _compute_name(self):
        for rec in self:
            rec.name = rec.product_id.name or rec.sku_part_number

    @api.depends('enabled_units', 'consumed_units')
    def _compute_available(self):
        for rec in self:
            rec.available_units = max(rec.enabled_units - rec.consumed_units, 0)
            rec.utilization = (rec.consumed_units * 100.0 / rec.enabled_units
                               if rec.enabled_units else 0.0)

    @api.depends('user_ids')
    def _compute_user_count(self):
        for rec in self:
            rec.user_count = len(rec.user_ids)

    def _record_snapshot(self, now=None):
        """Store a history point when values changed or the last one is > 1 day old."""
        Snapshot = self.env['m365.inventory.snapshot'].sudo()
        now = now or fields.Datetime.now()
        for sku in self:
            last = Snapshot.search([('sku_id', '=', sku.id)], order='snapshot_date desc', limit=1)
            if (not last
                    or last.enabled_units != sku.enabled_units
                    or last.consumed_units != sku.consumed_units
                    or last.suspended_units != sku.suspended_units
                    or last.warning_units != sku.warning_units
                    or last.snapshot_date < now - timedelta(days=1)):
                Snapshot.create({
                    'sku_id': sku.id,
                    'snapshot_date': now,
                    'enabled_units': sku.enabled_units,
                    'consumed_units': sku.consumed_units,
                    'suspended_units': sku.suspended_units,
                    'warning_units': sku.warning_units,
                })

    def action_view_users(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_license_user')
        action['domain'] = [('sku_ids', 'in', self.id)]
        return action
