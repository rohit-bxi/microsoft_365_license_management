# -*- coding: utf-8 -*-
"""Tenant customers each user can see.

Users of the *Microsoft 365 / User* group only see the customers mapped to them here,
with the contacts of these customers; a user without mapping sees none. They also see
the tenants of these customers, with their subscriptions, users, license requests,
audit logs and inventory history, and no other tenant. Administrators see every
customer and tenant. The record rules are in ``security/security.xml``; they are
evaluated as superuser, so the users need no access to this model.
"""
from odoo import api, fields, models

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
USER_GROUP = 'm365_license_management.group_m365_user'
RELATION = 'm365_user_customer_rel'


class M365UserCustomerMapping(models.Model):
    _name = 'm365.user.customer.mapping'
    _description = 'Microsoft 365 User Customer Mapping'
    _inherit = ['mail.thread']
    _order = 'user_id'
    _rec_name = 'user_id'

    user_id = fields.Many2one(
        'res.users', string="User", required=True, ondelete='cascade', index=True,
        tracking=True,
        domain=lambda self: [('share', '=', False),
                             ('all_group_ids', 'in', self.env.ref(USER_GROUP).ids)])
    customer_ids = fields.Many2many(
        'customer.customer', RELATION, 'mapping_id', 'customer_id', string="Customers",
        tracking=True, domain=[('parent_id', '=', False)],
        help="The user sees these customers and their contacts only, and the tenants of "
             "these customers.")
    customer_count = fields.Integer(string="Customer Count", compute='_compute_customer_count')

    _user_uniq = models.Constraint(
        'unique(user_id)', 'This user already has a customer mapping.')

    @api.depends('customer_ids')
    def _compute_customer_count(self):
        for rec in self:
            rec.customer_count = len(rec.customer_ids)


class CustomerCustomer(models.Model):
    _inherit = 'customer.customer'

    user_mapping_ids = fields.Many2many(
        'm365.user.customer.mapping', RELATION, 'customer_id', 'mapping_id',
        string="Users with Access", copy=False, groups=ADMIN_GROUP,
        help="Users who can see this customer and its tenants (User Customer Mapping). "
             "Administrators see every customer.")


class M365Tenant(models.Model):
    _inherit = 'm365.tenant'

    customer_user_mapping_ids = fields.Many2many(
        related='tenant_customer_id.user_mapping_ids', string="Users with Access",
        groups=ADMIN_GROUP,
        help="Users who see this tenant through its customer (User Customer Mapping). "
             "Administrators see every tenant.")

    @api.model
    def _dashboard_domain(self):
        """Tenants of the dashboard: the live tenants of the user's mapping.

        The record rules already restrict users to their mapping; an administrator
        with a mapping sees the mapped tenants as well, without mapping all customers.
        """
        domain = [('active', '=', True)] + self._live_domain()
        tenants = self._mapped_tenants()
        if tenants is not None:
            domain.append(('id', 'in', tenants.ids))
        return domain

    @api.model
    def _mapped_tenants(self):
        """Tenants of the customers mapped to the current user, None if the user has no
        mapping."""
        mapping = self.env['m365.user.customer.mapping'].sudo().search(
            [('user_id', '=', self.env.uid)])
        return mapping.customer_ids.tenant_ids if mapping else None
