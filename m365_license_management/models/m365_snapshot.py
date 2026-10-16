# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models

SNAPSHOT_RETENTION_DAYS = 400


class M365InventorySnapshot(models.Model):
    """History point of a subscription; feeds trend ("vs last month") figures."""
    _name = 'm365.inventory.snapshot'
    _description = 'Microsoft 365 Inventory Snapshot'
    _order = 'snapshot_date desc, id desc'

    sku_id = fields.Many2one('m365.sku', string="Subscription", required=True,
                             ondelete='cascade', index=True)
    tenant_id = fields.Many2one(related='sku_id.tenant_id', store=True, index=True)
    sku_part_number = fields.Char(related='sku_id.sku_part_number', store=True)
    snapshot_date = fields.Datetime(string="Date", required=True, default=fields.Datetime.now,
                                    index=True)
    enabled_units = fields.Integer(string="Total")
    consumed_units = fields.Integer(string="Assigned")
    suspended_units = fields.Integer(string="Suspended")
    warning_units = fields.Integer(string="Expiring")
    available_units = fields.Integer(string="Available", compute='_compute_available',
                                     store=True)

    @api.depends('enabled_units', 'consumed_units')
    def _compute_available(self):
        for rec in self:
            rec.available_units = max(rec.enabled_units - rec.consumed_units, 0)

    @api.model
    def _cron_cleanup(self):
        limit = fields.Datetime.now() - timedelta(days=SNAPSHOT_RETENTION_DAYS)
        self.sudo().search([('snapshot_date', '<', limit)]).unlink()
