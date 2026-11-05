# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models


class M365AuditLog(models.Model):
    _name = 'm365.audit.log'
    _description = 'Microsoft 365 Audit Log'
    _order = 'date desc, id desc'
    _rec_name = 'action'

    date = fields.Datetime(string="Date", default=fields.Datetime.now, readonly=True, index=True)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", readonly=True,
                                ondelete='cascade', index=True)
    action = fields.Selection([
        ('test_connection', 'Test Connection'),
        ('sync_inventory', 'Inventory Synchronisation'),
        ('sync_users', 'User Synchronisation'),
        ('assign_license', 'Assign License'),
        ('remove_license', 'Remove License'),
        ('refresh_user', 'User Refresh'),
        ('update_user', 'Update User'),
        ('webhook', 'Webhook'),
        ('request', 'License Request'),
        ('notification', 'Notification'),
        ('provision_tenant', 'Tenant Creation'),
    ], string="Operation", readonly=True, required=True)
    status = fields.Selection([
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('info', 'Info'),
    ], string="Result", readonly=True, required=True)
    message = fields.Text(string="Details", readonly=True)
    request_id = fields.Many2one('m365.license.request', string="Request",
                                 readonly=True, ondelete='set null')
    user_id = fields.Many2one('res.users', string="Performed By", readonly=True,
                              default=lambda self: self.env.user)
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)

    @api.model
    def _cron_cleanup(self):
        """Purge entries older than ``m365.audit_retention_days`` (default 365)."""
        days = int(self.env['ir.config_parameter'].sudo().get_param(
            'm365.audit_retention_days', 365))
        if days <= 0:
            return  # 0 = keep forever
        self.sudo().search([('date', '<', fields.Datetime.now() - timedelta(days=days))]).unlink()
