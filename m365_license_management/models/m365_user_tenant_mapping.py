# -*- coding: utf-8 -*-
"""Tenants each user can see.

Users of the *Microsoft 365 / User* group only see the records of the tenants mapped
to them here (tenants, subscriptions, users, license requests, audit logs and
inventory history); a user without mapping sees none. Administrators see every
tenant. The record rules are in ``security/security.xml``; they are evaluated as
superuser, so the users need no access to this model.
"""
from odoo import api, fields, models

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
USER_GROUP = 'm365_license_management.group_m365_user'
RELATION = 'm365_user_tenant_rel'


class M365UserTenantMapping(models.Model):
    _name = 'm365.user.tenant.mapping'
    _description = 'Microsoft 365 User Tenant Mapping'
    _inherit = ['mail.thread']
    _order = 'user_id'
    _rec_name = 'user_id'

    user_id = fields.Many2one(
        'res.users', string="User", required=True, ondelete='cascade', index=True,
        tracking=True,
        domain=lambda self: [('share', '=', False),
                             ('all_group_ids', 'in', self.env.ref(USER_GROUP).ids)])
    tenant_ids = fields.Many2many(
        'm365.tenant', RELATION, 'mapping_id', 'tenant_id', string="Tenants", tracking=True,
        help="The user sees the subscriptions, users, license requests and audit logs of "
             "these tenants only.")
    tenant_count = fields.Integer(string="Tenant Count", compute='_compute_tenant_count')

    _user_uniq = models.Constraint(
        'unique(user_id)', 'This user already has a tenant mapping.')

    @api.depends('tenant_ids')
    def _compute_tenant_count(self):
        for rec in self:
            rec.tenant_count = len(rec.tenant_ids)


class M365Tenant(models.Model):
    _inherit = 'm365.tenant'

    user_mapping_ids = fields.Many2many(
        'm365.user.tenant.mapping', RELATION, 'tenant_id', 'mapping_id',
        string="Users with Access", copy=False, groups=ADMIN_GROUP,
        help="Users who can see this tenant (User Tenant Mapping). Administrators see "
             "every tenant.")

    @api.model
    def _dashboard_domain(self):
        """Tenants of the dashboard: the live tenants of the user's mapping.

        The record rules already restrict users to their mapping; an administrator
        with a mapping sees the mapped tenants as well, without mapping all tenants.
        """
        domain = [('active', '=', True)] + self._live_domain()
        mapping = self.env['m365.user.tenant.mapping'].sudo().search(
            [('user_id', '=', self.env.uid)])
        if mapping:
            domain.append(('id', 'in', mapping.tenant_ids.ids))
        return domain
