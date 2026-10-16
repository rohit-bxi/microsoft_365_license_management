# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class M365LicenseUser(models.Model):
    """Directory user of a tenant with the licenses currently assigned."""
    _name = 'm365.license.user'
    _description = 'Microsoft 365 User'
    _order = 'name'

    name = fields.Char(string="Name", required=True, index=True)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True,
                                ondelete='cascade', index=True)
    graph_id = fields.Char(string="Object ID", required=True, index=True, copy=False)
    user_principal_name = fields.Char(string="User Principal Name", index=True)
    mail = fields.Char(string="Email")
    account_enabled = fields.Boolean(string="Account Enabled", default=True)
    department = fields.Char()
    job_title = fields.Char()
    usage_location = fields.Char(string="Usage Location",
                                 help="Required by Microsoft before a license can be assigned.")
    active = fields.Boolean(default=True)
    last_sync = fields.Datetime(string="Last Sync", readonly=True)
    sku_ids = fields.Many2many('m365.sku', 'm365_license_user_sku_rel', 'user_id', 'sku_id',
                               string="Assigned Licenses")
    license_count = fields.Integer(compute='_compute_license_count', string="Licenses",
                                   store=True)
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)

    _tenant_graph_uniq = models.Constraint(
        'unique(tenant_id, graph_id)', 'This user already exists for the tenant.')

    @api.depends('sku_ids')
    def _compute_license_count(self):
        for rec in self:
            rec.license_count = len(rec.sku_ids)

    def _action_request(self, request_type):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("New License Request"),
            'res_model': 'm365.license.request',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_tenant_id': self.tenant_id.id,
                'default_user_id': self.id,
                'default_request_type': request_type,
            },
        }

    def action_request_assign(self):
        return self._action_request('assign')

    def action_request_remove(self):
        return self._action_request('remove')
