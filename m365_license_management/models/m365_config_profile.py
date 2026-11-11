# -*- coding: utf-8 -*-
"""Tenant configuration profiles (Configuration > Tenant Configuration Profiles).

A profile is the baseline of the tenants: *Apply Configuration* on a tenant brings it to
the profile, and a tenant connected through the shared app (admin consent) gets it
applied automatically. See ``models/m365_tenant_config.py``.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_normalize


class M365ConfigProfile(models.Model):
    _name = 'm365.config.profile'
    _description = 'Microsoft 365 Tenant Configuration Profile'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string="Company",
                                 default=lambda self: self.env.company)
    is_default = fields.Boolean(
        string="Default for New Tenants", tracking=True,
        help="Selected on the tenants added from now on. Only one profile is the default.")
    security_defaults = fields.Selection([
        ('keep', 'Keep as it is'),
        ('enable', 'Turn On'),
        ('disable', 'Turn Off'),
    ], string="Security Defaults", default='keep', required=True, tracking=True,
        help="Security Defaults make every user of the tenant register and use MFA. "
             "Turning them off removes this requirement for the whole tenant; Odoo does it "
             "only for tenants with Enable Security Operations.")
    password_never_expires = fields.Boolean(
        string="Passwords Never Expire", tracking=True,
        help="Sets the password expiry of every verified, managed domain of the tenant to "
             "never.")
    usage_location_id = fields.Many2one(
        'res.country', string="Default Usage Location",
        help="Usage location of the users created from Odoo. Microsoft requires one before "
             "a license can be assigned.")
    technical_email = fields.Char(
        string="Technical Notification Email", tracking=True,
        help="Added to the technical contacts of the tenant, who receive the service "
             "notifications of Microsoft.")
    tenant_ids = fields.One2many('m365.tenant', 'config_profile_id', string="Tenants")
    tenant_count = fields.Integer(compute='_compute_tenant_count')

    @api.depends('tenant_ids')
    def _compute_tenant_count(self):
        for rec in self:
            rec.tenant_count = len(rec.tenant_ids)

    @api.constrains('technical_email')
    def _check_technical_email(self):
        for rec in self:
            if rec.technical_email and not email_normalize(rec.technical_email):
                raise UserError(_("%s is not a valid email address.", rec.technical_email))

    @api.model_create_multi
    def create(self, vals_list):
        profiles = super().create(vals_list)
        profiles.filtered('is_default')[-1:]._make_only_default()
        return profiles

    def write(self, vals):
        res = super().write(vals)
        if vals.get('is_default'):
            self[-1:]._make_only_default()
        return res

    def _make_only_default(self):
        if self:
            self.search([('is_default', '=', True), ('id', '!=', self.id)]).write(
                {'is_default': False})

    @api.model
    def _get_default(self):
        return self.search([('is_default', '=', True)], limit=1)

    def action_view_tenants(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_tenant')
        action['domain'] = [('config_profile_id', '=', self.id)]
        action['context'] = {'default_config_profile_id': self.id}
        return action
