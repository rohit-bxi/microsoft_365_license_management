# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .m365_tenant import M365GraphError


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

    # ------------------------------------------------------------------
    # Microsoft Graph actions
    # ------------------------------------------------------------------
    def _check_admin(self):
        if not self.env.user.has_group('m365_license_management.group_m365_admin'):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    def action_refresh_from_graph(self):
        """Re-read the user and the assigned licenses from Microsoft Graph."""
        self._check_admin()
        for rec in self:
            try:
                rec.tenant_id._refresh_user(rec)
                rec.tenant_id._log('refresh_user', 'success',
                                   _("%s refreshed from Microsoft 365.", rec.name))
            except M365GraphError as exc:
                rec.tenant_id._log('refresh_user', 'failed', "%s: %s" % (rec.name, exc))
                raise
        return True

    def get_live_license_details(self):
        """Licenses and service plan states straight from Microsoft Graph."""
        self.ensure_one()
        return self.tenant_id.graph_get_license_details(self.graph_id)

    def set_usage_location(self, country_code):
        """Push the usage location (ISO 3166-1 alpha-2) to Microsoft 365."""
        self._check_admin()
        code = (country_code or '').strip().upper()
        if not self.env['res.country'].sudo().search_count([('code', '=', code)]):
            raise UserError(_("%s is not a valid ISO 3166-1 alpha-2 country code.", country_code))
        for rec in self:
            try:
                rec.tenant_id.graph_update_user(rec.graph_id, {'usageLocation': code})
            except M365GraphError as exc:
                rec.tenant_id._log('update_user', 'failed', "%s: %s" % (rec.name, exc))
                raise
            rec.sudo().usage_location = code
            rec.tenant_id._log('update_user', 'success',
                               _("Usage location of %(user)s set to %(code)s.",
                                 user=rec.name, code=code))
        return True

    def action_set_usage_location(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Set Usage Location"),
            'res_model': 'm365.usage.location.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_user_id': self.id},
        }


class M365UsageLocationWizard(models.TransientModel):
    _name = 'm365.usage.location.wizard'
    _description = 'Set Microsoft 365 Usage Location'

    user_id = fields.Many2one('m365.license.user', string="User", required=True,
                              readonly=True)
    country_id = fields.Many2one('res.country', string="Usage Location", required=True)

    def action_apply(self):
        self.ensure_one()
        self.user_id.set_usage_location(self.country_id.code)
        return {'type': 'ir.actions.act_window_close'}
