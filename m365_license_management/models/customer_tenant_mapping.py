# -*- coding: utf-8 -*-
"""Tenants of each customer (Configuration > Customer Tenants Mapping).

The tenant keeps its customer in ``tenant_customer_id``: the tenants of a mapping are
the tenants of its customer, adding or removing one sets or clears the customer of the
tenant. Every customer of a tenant has a mapping, whether the customer is selected here,
on the tenant or on the customer. Deleting a mapping removes the customer of its tenants.
"""
from odoo import api, fields, models


class CustomerTenantMapping(models.Model):
    _name = 'customer.tenant.mapping'
    _description = 'Customer Tenants Mapping'
    _inherit = ['mail.thread']
    _order = 'customer_id'
    _rec_name = 'customer_id'

    customer_id = fields.Many2one(
        'customer.customer', string="Customer", required=True, ondelete='cascade',
        index=True, tracking=True, domain=[('parent_id', '=', False)])
    tenant_ids = fields.One2many(
        related='customer_id.tenant_ids', readonly=False, string="Tenants")
    tenant_count = fields.Integer(related='customer_id.tenant_count', string="Tenant Count")

    _customer_uniq = models.Constraint(
        'unique(customer_id)', 'This customer already has a tenant mapping.')

    def unlink(self):
        self.env['m365.tenant'].with_context(active_test=False).search(
            [('tenant_customer_id', 'in', self.customer_id.ids)]).write(
            {'tenant_customer_id': False})
        return super().unlink()

    @api.model
    def _create_missing(self, customers):
        """Create the mapping of these customers if they have none."""
        mappings = self.sudo().search([('customer_id', 'in', customers.ids)])
        self.sudo().create([{'customer_id': customer.id}
                            for customer in customers - mappings.customer_id])

    @api.model
    def _create_missing_all(self):
        """Mappings of the customers of the existing tenants (module installation and
        update)."""
        tenants = self.env['m365.tenant'].sudo().with_context(active_test=False).search(
            [('tenant_customer_id', '!=', False)])
        self._create_missing(tenants.tenant_customer_id)


class M365Tenant(models.Model):
    _inherit = 'm365.tenant'

    @api.model_create_multi
    def create(self, vals_list):
        tenants = super().create(vals_list)
        self.env['customer.tenant.mapping']._create_missing(tenants.tenant_customer_id)
        return tenants

    def write(self, vals):
        res = super().write(vals)
        if vals.get('tenant_customer_id'):
            self.env['customer.tenant.mapping']._create_missing(self.tenant_customer_id)
        return res
