# -*- coding: utf-8 -*-
"""Customers owning the Microsoft 365 tenants (Configuration > Tenant Customers).

A customer master with the fields of a contact, independent of ``res.partner``:
customers with their contacts, address, tax ID, salesperson and tags. A tenant
belongs to a customer, not to one of its contacts. Users only see the customers mapped
to them (User Customer Mapping); the record rules are in ``security/security.xml``.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import email_normalize


def _lang_get(self):
    return self.env['res.lang'].get_installed()


class CustomerTag(models.Model):
    _name = 'customer.tag'
    _description = 'Tenant Customer Tag'
    _order = 'name'

    name = fields.Char(string="Tag Name", required=True, translate=True)
    color = fields.Integer(string="Color Index")
    active = fields.Boolean(default=True)

    _name_uniq = models.Constraint('unique(name)', 'This tag already exists.')


class CustomerCustomer(models.Model):
    _name = 'customer.customer'
    _description = 'Tenant Customer'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'avatar.mixin']
    _order = 'name, id'
    _rec_names_search = ['name', 'ref', 'email', 'vat']

    name = fields.Char(required=True, index=True, tracking=True)
    parent_id = fields.Many2one('customer.customer', string="Related Company", index=True,
                                tracking=True)
    child_ids = fields.One2many('customer.customer', 'parent_id', string="Contacts",
                                domain=[('active', '=', True)])
    function = fields.Char(string="Job Position")
    email = fields.Char(tracking=True)
    phone = fields.Char(tracking=True)
    website = fields.Char()
    lang = fields.Selection(_lang_get, string="Language",
                            default=lambda self: self.env.lang)

    # Address
    street = fields.Char()
    street2 = fields.Char()
    city = fields.Char()
    state_id = fields.Many2one('res.country.state', string="State", ondelete='restrict',
                               domain="[('country_id', '=?', country_id)]")
    zip = fields.Char(string="ZIP")
    country_id = fields.Many2one('res.country', string="Country", ondelete='restrict')

    # Sales & Purchase
    vat = fields.Char(string="Tax ID", index=True, tracking=True)
    company_registry = fields.Char(string="Company Registry")
    ref = fields.Char(string="Reference", index=True)
    industry_id = fields.Many2one('res.partner.industry', string="Industry")
    user_id = fields.Many2one('res.users', string="Salesperson", tracking=True,
                              domain=[('share', '=', False)])
    category_ids = fields.Many2many('customer.tag', string="Tags")
    comment = fields.Html(string="Notes")
    company_id = fields.Many2one('res.company', string="Company",
                                 default=lambda self: self.env.company)
    active = fields.Boolean(default=True, tracking=True)
    color = fields.Integer(string="Color Index")

    portal_user_id = fields.Many2one(
        'res.users', string="Portal User", readonly=True, copy=False, tracking=True,
        help="Portal user of the customer, see Action > Grant portal access.")

    # Microsoft 365
    tenant_ids = fields.One2many('m365.tenant', 'tenant_customer_id', string="Tenants")
    tenant_count = fields.Integer(string="Tenant Count", compute='_compute_tenant_count')

    @api.depends('name', 'parent_id.name')
    def _compute_display_name(self):
        """``Contoso Ltd, Megan Bowen`` for the contacts of a company."""
        for rec in self:
            rec.display_name = ("%s, %s" % (rec.parent_id.name, rec.name)
                                if rec.parent_id and rec.name else rec.name)

    @api.depends('tenant_ids')
    def _compute_tenant_count(self):
        for rec in self:
            rec.tenant_count = len(rec.tenant_ids)

    @api.constrains('email')
    def _check_email(self):
        for rec in self:
            if rec.email and not email_normalize(rec.email):
                raise UserError(_("The email address %(email)s of %(name)s is not valid.",
                                  email=rec.email, name=rec.name))

    @api.constrains('parent_id')
    def _check_parent_id(self):
        if self._has_cycle():
            raise UserError(_("A customer cannot be a contact of itself or of its own "
                              "contacts."))

    @api.onchange('country_id')
    def _onchange_country_id(self):
        if self.country_id and self.country_id != self.state_id.country_id:
            self.state_id = False

    @api.onchange('state_id')
    def _onchange_state(self):
        if self.state_id.country_id and self.country_id != self.state_id.country_id:
            self.country_id = self.state_id.country_id

    @api.ondelete(at_uninstall=False)
    def _unlink_except_tenant(self):
        for rec in self.sudo().with_context(active_test=False):
            if rec.tenant_ids:
                raise UserError(_("%(name)s cannot be deleted: it is the customer of the "
                                  "tenant(s) %(tenants)s. Archive it instead.",
                                  name=rec.name,
                                  tenants=", ".join(rec.tenant_ids.mapped('name'))))

    def action_view_tenants(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_tenant')
        action['domain'] = [('tenant_customer_id', '=', self.id)]
        action['context'] = {'default_tenant_customer_id': self.id}
        return action


class M365Tenant(models.Model):
    _inherit = 'm365.tenant'

    tenant_customer_id = fields.Many2one(
        'customer.customer', string="Customer", index=True, tracking=True,
        ondelete='restrict', domain=[('parent_id', '=', False)],
        help="Customer owning this tenant (Configuration > Tenant Customers).")
