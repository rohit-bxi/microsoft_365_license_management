# -*- coding: utf-8 -*-
"""Users of the customer file, created once their tenant is ready.

A user is created (``POST /users``) when its tenant is connected, the domain of its user
name is verified and each of its licenses is in the tenant with free units. Until then it
waits, with the reason. Its licenses are then requested in the name of the administrator
who imported the file, so the approval rules of the tenant apply.

The licenses are named like in the Bulk Create Users file (license name, SKU part number or
SKU ID), or by the Partner Center offer they were bought with, or ``default`` (default offer
of the Partner Center account): the offer gives the Graph ``skuId``, nothing is hardcoded.

The temporary passwords stay in Odoo (administrators only) until they are downloaded
once (*Download Credentials*), then they are removed.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .m365_tenant import M365GraphError
from .m365_tenant_domain import is_client_error

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
LICENSE_SPLIT_RE = re.compile(r'[;,\n]')
DEFAULT_LICENSE = 'default'


class M365TenantLicenses(models.Model):
    _inherit = 'm365.tenant'

    pending_user_ids = fields.One2many('m365.tenant.pending.user', 'tenant_id',
                                       string="Users to Create")
    pending_user_count = fields.Integer(compute='_compute_pending_user_count')

    @api.depends('pending_user_ids')
    def _compute_pending_user_count(self):
        for rec in self:
            rec.pending_user_count = len(rec.pending_user_ids)

    def _match_license(self, name):
        """License of the tenant named ``name``: its name, SKU part number or SKU ID, or a
        Partner Center offer (name, SKU title, catalog item ID, or ``default``) giving its
        Graph skuId. :return: m365.sku (empty when not in the tenant)"""
        self.ensure_one()
        key = (name or '').strip().lower()
        skus = self.env['m365.sku'].sudo().search([('tenant_id', '=', self.id),
                                                   ('status', '!=', 'Deleted')])
        if not key:
            return skus.browse()
        sku = skus.filtered(lambda s: key in ((s.sku_part_number or '').lower(),
                                              (s.name or '').lower(),
                                              (s.sku_id or '').lower()))[:1]
        if sku:
            return sku
        if self._purchase_channel() == 'ingram':
            return self._match_ingram_license(key, skus) or self._match_offer_license(
                key, skus)
        return self._match_offer_license(key, skus) or self._match_ingram_license(key, skus)

    def _match_offer_license(self, key, skus):
        """License of a Partner Center offer (its provisioningId is the Graph skuId)."""
        account = self.sudo().partner_account_id or self._default_partner_account()
        Offer = self.env['m365.pc.offer'].sudo().with_context(active_test=False)
        if key == DEFAULT_LICENSE:
            offers = account.default_offer_id if account else Offer
        else:
            offers = Offer.search([('provisioning_id', '!=', False), '|', '|',
                                   ('name', '=ilike', key), ('sku_title', '=ilike', key),
                                   ('catalog_item_id', '=ilike', key)])
        guids = {(o.provisioning_id or '').lower() for o in offers} - {''}
        return skus.filtered(lambda s: (s.sku_id or '').lower() in guids)[:1]

    def _match_ingram_license(self, key, skus):
        """License of an Ingram Micro plan, through the catalogue product of the plan."""
        account = self.env['m365.ingram.account'].sudo()._get_default()
        if not account:
            return skus.browse()
        if key == DEFAULT_LICENSE:
            plans = account.default_plan_id
        else:
            plans, _term, _error = account._match_plan(key)
        products = plans.product_id
        return skus.filtered(lambda s: s.product_id and s.product_id in products)[:1]

    def _user_domain(self):
        """Domain of the user names without one: the first custom domain, else the
        initial (.onmicrosoft.com) domain."""
        self.ensure_one()
        customs = self._custom_domain_names()
        return customs[0] if customs else (self.domain or '').lower()

    def action_view_pending_users(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_pending_user')
        action['domain'] = [('tenant_id', '=', self.id)]
        action['context'] = {'default_tenant_id': self.id}
        return action

    def action_download_credentials(self):
        return self.pending_user_ids.action_download_credentials()


class M365PendingUser(models.Model):
    _name = 'm365.tenant.pending.user'
    _description = 'Microsoft 365 User to Create'
    _order = 'tenant_id, sequence, id'

    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True,
                                ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    user_name = fields.Char(string="User Name", required=True,
                            help="Part before the @, or the full user principal name.")
    upn = fields.Char(string="User Principal Name", compute='_compute_upn', store=True)
    name = fields.Char(string="Name in Microsoft 365", required=True,
                       help="Display name of the user in Microsoft 365.")
    given_name = fields.Char(string="First Name")
    surname = fields.Char(string="Last Name")
    usage_location = fields.Char(string="Usage Location", size=2,
                                 help="ISO country code; blank: the country of the tenant.")
    licenses = fields.Char(string="Licenses",
                           help="Separated by ;. License name, SKU, Partner Center offer, or "
                                "\"default\".")
    department = fields.Char()
    job_title = fields.Char()
    state = fields.Selection([
        ('waiting', 'Waiting'),
        ('created', 'Created'),
        ('licensed', 'Licenses Requested'),
        ('failed', 'Failed'),
    ], string="Status", default='waiting', required=True, readonly=True, index=True)
    wait_reason = fields.Char(string="Waiting For", readonly=True)
    error = fields.Text(readonly=True)
    user_id = fields.Many2one('m365.license.user', string="Microsoft 365 User", readonly=True,
                              ondelete='set null')
    request_ids = fields.Many2many('m365.license.request', string="License Requests",
                                   readonly=True)
    password = fields.Char(string="Temporary Password", readonly=True, copy=False,
                           groups=ADMIN_GROUP)
    has_password = fields.Boolean(compute='_compute_has_password')
    created_date = fields.Datetime(string="Created On", readonly=True)
    requested_by = fields.Many2one('res.users', string="Requested By", readonly=True,
                                   default=lambda self: self.env.user)
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)

    @api.depends('user_name', 'tenant_id.domain', 'tenant_id.custom_domains')
    def _compute_upn(self):
        for rec in self:
            name = (rec.user_name or '').strip()
            if '@' in name or not rec.tenant_id:
                rec.upn = name or False
            else:
                rec.upn = '%s@%s' % (name, rec.tenant_id._user_domain()) if name else False

    def _compute_has_password(self):
        for rec in self:
            rec.has_password = bool(rec.sudo().password)

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def _lock(self):
        """Lock the row for this transaction, so that two scheduled actions never create
        the same user. :return: False when another transaction has it"""
        self.ensure_one()
        self.env.cr.execute("SELECT id FROM m365_tenant_pending_user WHERE id = %s "
                            "FOR UPDATE SKIP LOCKED", (self.id,))
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset(['state'])
        return self.state == 'waiting'

    def _license_names(self):
        self.ensure_one()
        return [n.strip() for n in LICENSE_SPLIT_RE.split(self.licenses or '') if n.strip()]

    def _readiness(self, reserved):
        """(skus, usage location, reason to wait or False).

        :param reserved: {sku: licenses already taken by the users created in this run}
        """
        self.ensure_one()
        tenant = self.tenant_id
        if tenant.state not in ('connected', 'error'):
            return None, None, _("Connection of the tenant")
        domain = (self.upn or '').rsplit('@', 1)[-1].lower()
        verified = tenant.domain_ids.filtered(lambda d: d.is_verified).mapped('name')
        if domain not in verified:
            if not tenant.domain_ids:
                try:
                    tenant._sync_domains()
                except M365GraphError:
                    pass
                verified = tenant.domain_ids.filtered('is_verified').mapped('name')
            if domain not in verified:
                return None, None, _("Verification of the domain %s", domain)
        skus = self.env['m365.sku']
        for name in self._license_names():
            sku = tenant._match_license(name)
            if not sku:
                return None, None, _("License %s in the tenant", name)
            free = sku.available_units - reserved.get(sku.id, 0)
            if free < 1:
                return None, None, _("A free %s license", sku.name)
            skus |= sku
        usage = (self.usage_location or tenant._default_usage_location() or '').upper()
        if skus and not usage:
            return None, None, _("A usage location (column UsageLocation or country of the "
                                 "tenant)")
        return skus, usage, False

    def _try_create(self, reserved=None):
        """Create the user and request its licenses when the tenant is ready.
        Failures are stored, never raised. :return: the new state"""
        self.ensure_one()
        reserved = {} if reserved is None else reserved
        if self.state != 'waiting' or not self._lock():
            return self.state
        tenant = self.tenant_id
        if not tenant.allow_license_operations:
            self.write({'state': 'failed', 'wait_reason': False, 'error': _(
                "License operations are disabled for tenant %s.", tenant.name)})
            return self.state
        skus, usage, reason = self._readiness(reserved)
        if reason:
            self.write({'wait_reason': reason})
            return self.state
        existing = self.env['m365.license.user'].sudo().with_context(active_test=False).search(
            [('tenant_id', '=', tenant.id), ('user_principal_name', '=ilike', self.upn)],
            limit=1)
        if existing:
            user, password = existing, False
        else:
            errors = tenant._check_new_user(self.upn, self.name, usage)
            if errors:
                self.write({'state': 'failed', 'error': "\n".join(errors), 'wait_reason': False})
                return self.state
            try:
                user, password = tenant._create_user(
                    self.upn, self.name, given_name=self.given_name,
                    surname=self.surname, usage_location=usage, department=self.department,
                    job_title=self.job_title)
            except UserError as exc:  # M365GraphError included
                if isinstance(exc, M365GraphError) and not is_client_error(exc):
                    self.write({'error': str(exc), 'wait_reason': _("Microsoft 365 (retry)")})
                else:
                    self.write({'state': 'failed', 'error': str(exc), 'wait_reason': False})
                return self.state
        self.sudo().write({'user_id': user.id, 'password': password or False,
                           'state': 'created', 'created_date': fields.Datetime.now(),
                           'wait_reason': False, 'error': False})
        if skus:
            for sku in skus:
                reserved[sku.id] = reserved.get(sku.id, 0) + 1
            requester = self.requested_by if self.requested_by.has_group(ADMIN_GROUP) \
                else self.env.user
            results = tenant.with_user(requester)._request_licenses(
                user, skus, _("Provisioning of %s", tenant.name))
            requests = self.env['m365.license.request'].browse(
                [request.id for _sku, request, _msg in results if request])
            errors = ["%s: %s" % (sku.name, msg) for sku, request, msg in results
                      if not request]
            self.write({'request_ids': [(6, 0, requests.ids)], 'error': "\n".join(errors) or False,
                        'state': 'licensed' if requests else 'created'})
        return self.state

    def action_create_now(self):
        """Create the selected users now when their tenant is ready."""
        self._check_admin()
        reserved = {}
        for rec in self.filtered(lambda u: u.state == 'waiting'):
            rec._try_create(reserved)
        return True

    def action_retry(self):
        self._check_admin()
        self.filtered(lambda u: u.state == 'failed').write(
            {'state': 'waiting', 'error': False})
        return self.action_create_now()

    @api.model
    def _cron_create_users(self):
        """Users waiting for their tenant: created as soon as it is ready."""
        tenants = self.sudo().search([('state', '=', 'waiting')]).tenant_id
        for tenant in tenants.filtered(lambda t: t.state in ('connected', 'error')):
            tenant.pending_user_ids._create_waiting()

    def _create_waiting(self):
        reserved = {}
        for rec in self.filtered(lambda u: u.state == 'waiting'):
            rec._try_create(reserved)

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def action_download_credentials(self):
        self._check_admin()
        users = self.filtered(lambda u: u.sudo().password)
        if not users:
            raise UserError(_("No temporary password to download: they are downloaded once "
                              "and then removed from Odoo."))
        return {
            'type': 'ir.actions.act_url',
            'url': '/m365/export/credentials?ids=%s' % ','.join(map(str, users.ids)),
            'target': 'download',
        }
