# -*- coding: utf-8 -*-
"""Microsoft 365 subscriptions bought through Partner Center (new commerce).

=========================================  ===============================================
Step                                       Partner Center call (app-only)
=========================================  ===============================================
Products of the new commerce catalog       GET  /v1/products?country=&targetView=OnlineServicesNCE
SKUs of a product                          GET  /v1/products/{product}/skus?country=
Terms of a SKU (catalog item ID)           GET  /v1/products/{product}/skus/{sku}/availabilities
Cart                                       POST /v1/customers/{customer}/carts
Checkout (idempotent per cart)             POST /v1/customers/{customer}/carts/{cart}/checkout
Order status and subscription IDs          GET  /v1/customers/{customer}/orders/{order}
=========================================  ===============================================

::

    Draft --Submit--> Waiting Approval --Approve--> Ordered --Partner Center completed and
           licenses seen in Graph--> Done            |
                                                     +--error--> Failed --Retry (same cart)

* No duplicate order: the cart ID is saved before the checkout, which Partner Center
  makes idempotent per cart: Retry checks out the same cart again.
* The SKU of the catalog carries the Graph skuId (``provisioningId``): Odoo sees the
  bought licenses in the tenant without any product mapping.
* Microsoft requires the customer's acceptance of the Microsoft Customer Agreement; its
  confirmation API needs App+User sign-in, so the customer accepts it in the Microsoft 365
  admin center and Odoo records who and when (Purchasing tab).

Reference: https://learn.microsoft.com/en-us/partner-center/developer/create-a-cart
"""
import logging
import urllib.parse
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from .m365_tenant import M365GraphError

_logger = logging.getLogger(__name__)

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
NCE_VIEW = 'OnlineServicesNCE'
CART_VALIDITY = timedelta(days=6)   # Partner Center carts expire after 7 days
MAX_PAGES = 50


class M365PartnerAccountPurchase(models.Model):
    _inherit = 'm365.partner.account'

    purchase_channel = fields.Selection([
        ('partner_center', 'Partner Center'),
        ('ingram', 'Ingram Micro'),
    ], string="Buy Licenses Through", default='partner_center', required=True,
        tracking=True,
        help="Partner Center: direct bill partners and indirect providers. Ingram Micro: "
             "indirect resellers, who cannot order in Partner Center.")
    catalog_country = fields.Char(string="Catalog Country", default='IN', size=2,
                                  help="Country of the Partner Center catalog and prices.")
    catalog_filter = fields.Char(
        string="Catalog Products", default='Microsoft 365, Office 365, Exchange Online, '
                                           'Teams, Microsoft Entra, Intune, Defender, Power BI, '
                                           'Visio, Project',
        help="Products whose title contains one of these words (comma separated) are read; "
             "the catalog has hundreds of products.")
    require_mca_pc = fields.Boolean(
        string="Require Customer Agreement", default=True,
        help="A Partner Center order can be submitted only for tenants whose acceptance of "
             "the Microsoft Customer Agreement is recorded.")
    default_offer_id = fields.Many2one(
        'm365.pc.offer', string="Default Offer",
        domain="[('account_id', '=', id)]",
        help="Offer bought when the Subscriptions of a customer give a quantity only, and "
             "license \"default\" of the users file, e.g. Microsoft 365 Business Basic "
             "(no Teams), monthly. The same offer is taken in the catalog of the country of "
             "each customer.")
    offer_ids = fields.One2many('m365.pc.offer', 'account_id', string="Offers")
    offer_count = fields.Integer(compute='_compute_offer_count')
    last_offer_sync = fields.Datetime(string="Last Catalog Sync", readonly=True, copy=False)

    def _compute_offer_count(self):
        for rec in self:
            rec.offer_count = len(rec.offer_ids)

    def _pc_items(self, path):
        """Items of a Partner Center collection, following its next links."""
        items = []
        for _page in range(MAX_PAGES):
            _status, data = self._pc_request('GET', path)
            items += [i for i in data.get('items') or [] if isinstance(i, dict)]
            nxt = ((data.get('links') or {}).get('next') or {}).get('uri')
            if not nxt:
                break
            path = nxt if nxt.startswith('/v1') else '/v1' + nxt
        return items

    def _catalog_countries(self):
        """Catalog country of the account, then the countries of its tenants."""
        self.ensure_one()
        countries = [(self.catalog_country or 'IN').strip().upper()]
        tenants = self.env['m365.tenant'].sudo().search([('partner_account_id', '=', self.id),
                                                         ('country_id', '!=', False)])
        for code in tenants.mapped('country_id.code'):
            if code and code.upper() not in countries:
                countries.append(code.upper())
        return countries

    def _sync_offers(self):
        """Read the new commerce offers of the catalog, in the country of the account and
        of each of its tenants. :return: number of offers"""
        self.ensure_one()
        Offer = self.env['m365.pc.offer'].sudo().with_context(active_test=False)
        existing = {o._key(): o for o in Offer.search([('account_id', '=', self.id)])}
        seen, now = set(), fields.Datetime.now()
        for country in self._catalog_countries():
            self._sync_country_offers(country, Offer, existing, seen, now)
        Offer.browse([o.id for k, o in existing.items() if k not in seen]).write(
            {'active': False})
        self.write({'last_offer_sync': now})
        return len(seen)

    def _sync_country_offers(self, country, Offer, existing, seen, now):
        words = [w.strip().lower() for w in (self.catalog_filter or '').split(',') if w.strip()]
        products = self._pc_items('/v1/products?country=%s&targetView=%s' % (country, NCE_VIEW))
        for product in products:
            title = product.get('title') or ''
            if words and not any(w in title.lower() for w in words):
                continue
            pid = urllib.parse.quote(product.get('id') or '', safe='')
            for sku in self._pc_items('/v1/products/%s/skus?country=%s' % (pid, country)):
                sid = urllib.parse.quote(sku.get('id') or '', safe='')
                attrs = sku.get('dynamicAttributes') or {}
                for avail in self._pc_items('/v1/products/%s/skus/%s/availabilities?country=%s'
                                            % (pid, sid, country)):
                    if avail.get('isPurchasable') is False or not avail.get('catalogItemId'):
                        continue
                    terms = avail.get('terms') or [{}]
                    for term in terms:
                        vals = {
                            'name': title or product.get('id'),
                            'sku_title': sku.get('title') or False,
                            'product_code': product.get('id'),
                            'sku_code': sku.get('id'),
                            'catalog_item_id': avail['catalogItemId'],
                            'term_duration': term.get('duration') or False,
                            'billing_cycle': term.get('billingCycle') or False,
                            'provisioning_id': (attrs.get('provisioningId') or '').lower()
                            or False,
                            'min_quantity': sku.get('minimumQuantity') or 0,
                            'max_quantity': sku.get('maximumQuantity') or 0,
                            'active': True,
                            'last_refresh': now,
                            'country': country,
                        }
                        key = (country, vals['catalog_item_id'], vals['term_duration'] or '',
                               vals['billing_cycle'] or '')
                        seen.add(key)
                        if key in existing:
                            existing[key].write(vals)
                        else:
                            existing[key] = Offer.create(dict(vals, account_id=self.id))

    def action_sync_offers(self):
        self.env['m365.tenant']._check_admin()
        for rec in self:
            try:
                count = rec._sync_offers()
            except M365GraphError as exc:
                return self.env['m365.tenant']._notify(
                    _("Catalog not read: %s", exc), 'danger')
            rec.message_post(body=_("%s offer(s) read from the Partner Center catalog.",
                                    count))
        return self.env['m365.tenant']._notify(_("Partner Center catalog synchronised."))

    def action_view_offers(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_pc_offer')
        action['domain'] = [('account_id', '=', self.id)]
        return action


class M365PcOffer(models.Model):
    _name = 'm365.pc.offer'
    _description = 'Partner Center Offer'
    _order = 'name, sku_title, term_duration, billing_cycle'

    account_id = fields.Many2one('m365.partner.account', string="Partner Center Account",
                                 required=True, ondelete='cascade', index=True)
    name = fields.Char(string="Product", required=True)
    sku_title = fields.Char(string="SKU")
    product_code = fields.Char(string="Product ID")
    sku_code = fields.Char(string="SKU ID")
    catalog_item_id = fields.Char(string="Catalog Item ID", required=True, index=True)
    term_duration = fields.Char(string="Term", help="P1M: one month, P1Y: one year.")
    billing_cycle = fields.Char(string="Billing")
    provisioning_id = fields.Char(string="Graph SKU ID", index=True,
                                  help="skuId of the licenses in Microsoft Graph.")
    country = fields.Char(string="Country", size=2, index=True,
                          help="Country of the catalog the offer was read from.")
    min_quantity = fields.Integer(string="Minimum")
    max_quantity = fields.Integer(string="Maximum")
    active = fields.Boolean(default=True)
    last_refresh = fields.Datetime(readonly=True)

    @api.depends('name', 'sku_title', 'term_duration', 'billing_cycle', 'country')
    def _compute_display_name(self):
        for rec in self:
            title = rec.name if not rec.sku_title or rec.sku_title == rec.name else \
                "%s - %s" % (rec.name, rec.sku_title)
            rec.display_name = "%s [%s %s%s]" % (title, rec.term_duration or '-',
                                                 rec.billing_cycle or '-',
                                                 ' %s' % rec.country if rec.country else '')

    def _key(self):
        # An offer without country comes from the catalog country of its account
        country = self.country or (self.account_id.catalog_country or 'IN').upper()
        return (country, self.catalog_item_id, self.term_duration or '',
                self.billing_cycle or '')


class M365PcOrder(models.Model):
    _name = 'm365.pc.order'
    _description = 'Microsoft 365 Subscription Order (Partner Center)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="Reference", default=lambda self: _('New'), readonly=True,
                       copy=False, index=True)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True, tracking=True,
                                index=True,
                                domain="['|', ('tenant_type', '=', 'existing'), "
                                       "('provision_state', '=', 'done')]")
    account_id = fields.Many2one('m365.partner.account', string="Partner Center Account",
                                 required=True, tracking=True,
                                 default=lambda self: self.env['m365.tenant']
                                 ._default_partner_account())
    customer_id = fields.Many2one(related='tenant_id.tenant_customer_id', store=True,
                                  string="Customer")
    line_ids = fields.One2many('m365.pc.order.line', 'order_id', string="Lines", copy=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Waiting Approval'),
        ('ordered', 'Ordered'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], string="Status", default='draft', required=True, tracking=True, copy=False,
        index=True)
    reason = fields.Text(string="Business Justification")
    reject_reason = fields.Text(string="Rejection Reason", copy=False)
    requested_by = fields.Many2one('res.users', string="Requested By", readonly=True,
                                   default=lambda self: self.env.user, copy=False)
    approver_id = fields.Many2one('res.users', string="Approved / Rejected By", readonly=True,
                                  copy=False)
    attestation_accepted = fields.Boolean(
        string="Partner of Record Attestation", default=True,
        help="Sent as PartnerOnRecordAttestationAccepted: you confirm being the partner of "
             "record of the purchase.")
    cart_id = fields.Char(string="Cart ID", readonly=True, copy=False)
    cart_date = fields.Datetime(string="Cart Created On", readonly=True, copy=False)
    pc_order_ids = fields.Char(string="Partner Center Order(s)", readonly=True, copy=False)
    sent_date = fields.Datetime(string="Ordered On", readonly=True, copy=False)
    done_date = fields.Datetime(string="Done On", readonly=True, copy=False)
    error_message = fields.Text(string="Error", readonly=True, copy=False)
    amount_total = fields.Float(string="Total", readonly=True, copy=False, digits=(16, 2))
    currency = fields.Char(readonly=True, copy=False)
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)
    is_admin = fields.Boolean(compute='_compute_is_admin')
    catalog_country = fields.Char(compute='_compute_catalog_country',
                                  help="Country of the catalog of the customer.")

    @api.depends('tenant_id.country_id', 'account_id.catalog_country')
    def _compute_catalog_country(self):
        for rec in self:
            rec.catalog_country = (rec.tenant_id.country_id.code or rec.account_id.catalog_country
                                   or 'IN').upper()

    @api.depends_context('uid')
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group(ADMIN_GROUP)
        for rec in self:
            rec.is_admin = is_admin

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = (self.env['ir.sequence'].next_by_code('m365.pc.order')
                                or _('New'))
        return super().create(vals_list)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_ordered(self):
        if self.filtered(lambda o: o.cart_id or o.state not in ('draft', 'cancelled',
                                                                 'rejected')):
            raise UserError(_("An order sent to Partner Center cannot be deleted."))

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    def _commit(self):
        if not config['test_enable']:
            self.env.cr.commit()

    def _log(self, status, message):
        self.ensure_one()
        return self.tenant_id._log('pc_order', status, "%s: %s" % (self.name, message))

    def _customer_ms_id(self):
        """Partner Center customer ID: given at creation, else the directory ID."""
        tenant = self.tenant_id.sudo()
        return (tenant.customer_id or tenant.tenant_id or '').strip()

    def _schedule_admin_activity(self, summary, note):
        if self.sudo().activity_ids.filtered(lambda a: a.summary == summary):
            return
        users = self.env['res.users'].sudo().search([('share', '=', False)])
        for admin in users.filtered(lambda u: u.has_group(ADMIN_GROUP)):
            self.sudo().activity_schedule('mail.mail_activity_data_todo', user_id=admin.id,
                                          summary=summary, note=note)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _validate(self):
        self.ensure_one()
        tenant = self.tenant_id
        if not self.line_ids:
            raise UserError(_("Add the subscriptions to buy."))
        if not tenant.filtered_domain(tenant._live_domain()):
            raise UserError(_("Create the tenant in Microsoft 365 first."))
        if not self._customer_ms_id():
            raise UserError(_("The Partner Center customer ID of %s is not known.",
                              tenant.name))
        if not self.attestation_accepted:
            raise UserError(_("Partner Center requires the partner of record attestation."))
        if self.account_id.require_mca_pc and not tenant._mca_recorded():
            raise UserError(_(
                "Record first who accepted the Microsoft Customer Agreement for tenant %s, "
                "and when (Purchasing tab of the tenant).", tenant.name))
        for line in self.line_ids:
            offer = line.offer_id
            if offer.account_id != self.account_id:
                raise UserError(_("%s is not an offer of this Partner Center account.",
                                  offer.display_name))
            if offer.country and offer.country != self.catalog_country:
                raise UserError(_("%(offer)s is an offer of the catalog of %(offer_country)s; "
                                  "the customer buys in %(country)s.",
                                  offer=offer.display_name, offer_country=offer.country,
                                  country=self.catalog_country))
            if offer.min_quantity and line.quantity < offer.min_quantity or \
                    offer.max_quantity and line.quantity > offer.max_quantity:
                raise UserError(_("%(offer)s: %(min)s to %(max)s licenses.",
                                  offer=offer.display_name, min=offer.min_quantity or 1,
                                  max=offer.max_quantity or '-'))

    def action_submit(self):
        for rec in self.filtered(lambda o: o.state == 'draft'):
            rec._validate()
            rec.write({'state': 'submitted', 'requested_by': self.env.uid})
            rec._log('info', _("submitted by %s.", self.env.user.name))
            rec._schedule_admin_activity(
                _("Approve subscription order %s", rec.name),
                _("%(tenant)s: %(lines)s", tenant=rec.tenant_id.name, lines=", ".join(
                    "%s x %s" % (l.quantity, l.offer_id.display_name) for l in rec.line_ids)))
        return True

    def action_approve(self):
        self._check_admin()
        for rec in self.filtered(lambda o: o.state == 'submitted'):
            rec._validate()
            rec.write({'approver_id': self.env.uid})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec._log('info', _("approved by %s.", self.env.user.name))
            rec._send()
        return True

    def action_reject(self):
        self._check_admin()
        for rec in self.filtered(lambda o: o.state == 'submitted'):
            rec.write({'state': 'rejected', 'approver_id': self.env.uid})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec._log('info', _("rejected by %(user)s. %(reason)s", user=self.env.user.name,
                               reason=rec.reject_reason or ''))
        return True

    def action_retry(self):
        self._check_admin()
        for rec in self.filtered(lambda o: o.state == 'failed'):
            rec._validate()
            rec._send()
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state not in ('draft', 'submitted', 'failed'):
                raise UserError(_("An order placed in Partner Center cannot be cancelled "
                                  "from Odoo."))
            if rec.state != 'draft':
                rec._check_admin()
            rec.write({'state': 'cancelled'})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
        return True

    def action_reset_draft(self):
        self.filtered(lambda o: o.state in ('rejected', 'cancelled')).write(
            {'state': 'draft', 'approver_id': False, 'error_message': False,
             'cart_id': False, 'cart_date': False})
        return True

    # ------------------------------------------------------------------
    # Partner Center
    # ------------------------------------------------------------------
    def _cart_body(self):
        self.ensure_one()
        return {
            'PartnerOnRecordAttestationAccepted': bool(self.attestation_accepted),
            'lineItems': [{
                'id': index,
                'catalogItemId': line.offer_id.catalog_item_id,
                'quantity': line.quantity,
                'billingCycle': line.offer_id.billing_cycle or 'monthly',
                'termDuration': line.offer_id.term_duration or 'P1M',
            } for index, line in enumerate(self.line_ids)],
        }

    def _send(self):
        """Create the cart (once) and check it out. Failures are stored, never raised."""
        self.ensure_one()
        account = self.account_id.sudo()
        customer = urllib.parse.quote(self._customer_ms_id(), safe='')
        now = fields.Datetime.now()
        try:
            if not self.cart_id or not self.cart_date or self.cart_date < now - CART_VALIDITY:
                _status, cart = account._pc_request(
                    'POST', '/v1/customers/%s/carts' % customer, json_body=self._cart_body())
                # Saved before the checkout: a retry checks out the same cart
                self.write({'cart_id': cart.get('id'), 'cart_date': now})
                self._commit()
                for item in cart.get('lineItems') or []:
                    if item.get('error'):
                        raise M365GraphError(_("Cart line %(line)s: %(err)s",
                                               line=item.get('id'), err=item['error']))
            for line in self.line_ids:
                line.baseline_units = self.tenant_id._sku_units(line.offer_id.provisioning_id)
            _status, result = account._pc_request(
                'POST', '/v1/customers/%s/carts/%s/checkout'
                % (customer, urllib.parse.quote(self.cart_id, safe='')))
        except M365GraphError as exc:
            self.write({'state': 'failed', 'error_message': str(exc)})
            self._log('failed', str(exc))
            self._commit()
            return False
        errors = ["%s: %s" % (e.get('code') or '-', e.get('description') or e)
                  for e in result.get('orderErrors') or [] if isinstance(e, dict)]
        orders = [o for o in result.get('orders') or [] if isinstance(o, dict)]
        if not orders:
            message = "; ".join(errors) or _("Partner Center returned no order.")
            self.write({'state': 'failed', 'error_message': message})
            self._log('failed', message)
            self._commit()
            return False
        self.write({
            'state': 'ordered', 'sent_date': now, 'error_message': "; ".join(errors) or False,
            'pc_order_ids': ", ".join(o.get('id') for o in orders if o.get('id')),
            'amount_total': sum(float(o.get('totalPrice') or 0) for o in orders),
            'currency': orders[0].get('currencyCode') or False,
        })
        self._apply_orders(orders)
        message = _("Ordered in Partner Center: %s.", self.pc_order_ids)
        if errors:
            message += " " + _("Errors: %s", "; ".join(errors))
        self.message_post(body=Markup("<p>%s</p>") % escape(message))
        self._log('failed' if errors else 'success', message)
        self._commit()
        return True

    def _apply_orders(self, orders):
        """Subscription IDs and status of the lines from Partner Center orders."""
        self.ensure_one()
        by_item = {}
        for order in orders:
            for item in order.get('lineItems') or []:
                by_item.setdefault(item.get('offerId'), []).append((order, item))
        for line in self.line_ids:
            for order, item in by_item.get(line.offer_id.catalog_item_id, [])[:1]:
                vals = {'pc_status': order.get('status') or False}
                if item.get('subscriptionId'):
                    vals['subscription_id'] = item['subscriptionId']
                line.write(vals)

    def _check_progress(self):
        """Order status in Partner Center, then the licenses in the tenant."""
        account = self.account_id.sudo()
        for rec in self.filtered(lambda o: o.state == 'ordered'):
            customer = urllib.parse.quote(rec._customer_ms_id(), safe='')
            orders = []
            for order_id in [o for o in (rec.pc_order_ids or '').split(', ') if o]:
                try:
                    _status, data = account._pc_request(
                        'GET', '/v1/customers/%s/orders/%s'
                        % (customer, urllib.parse.quote(order_id, safe='')))
                except M365GraphError as exc:
                    _logger.info("Partner Center order %s not read: %s", order_id, exc)
                    continue
                orders.append(data)
            if orders:
                rec._apply_orders(orders)
            if any(line.pc_status not in ('completed',) for line in rec.line_ids):
                continue
            tenant = rec.tenant_id
            if tenant.state in ('connected', 'error'):
                try:
                    tenant._sync_inventory()
                except M365GraphError:
                    continue
                for line in rec.line_ids:
                    line.delivered = (not line.offer_id.provisioning_id or
                                      tenant._sku_units(line.offer_id.provisioning_id)
                                      >= line.baseline_units + line.quantity)
                if not all(rec.line_ids.mapped('delivered')):
                    continue
            rec.write({'state': 'done', 'done_date': fields.Datetime.now()})
            rec.message_post(body=_("The subscriptions are provisioned."))
            rec._log('success', _("provisioned: %s.", ", ".join(
                filter(None, rec.line_ids.mapped('subscription_id'))) or '-'))

    @api.model
    def _cron_check_progress(self):
        orders = self.search([('state', '=', 'ordered')])
        for order in orders:
            order._check_progress()
            self._commit()


class M365PcOrderLine(models.Model):
    _name = 'm365.pc.order.line'
    _description = 'Partner Center Subscription Order Line'
    _order = 'order_id, sequence, id'

    order_id = fields.Many2one('m365.pc.order', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    account_id = fields.Many2one(related='order_id.account_id')
    tenant_id = fields.Many2one(related='order_id.tenant_id', store=True)
    catalog_country = fields.Char(related='order_id.catalog_country')
    offer_id = fields.Many2one('m365.pc.offer', string="Offer", required=True,
                               domain="[('account_id', '=', account_id), "
                                      "('country', 'in', [catalog_country, False])]")
    quantity = fields.Integer(required=True, default=1)
    subscription_id = fields.Char(string="Subscription ID", readonly=True, copy=False)
    pc_status = fields.Char(string="Partner Center Status", readonly=True, copy=False)
    baseline_units = fields.Integer(string="Licenses Before", readonly=True, copy=False)
    delivered = fields.Boolean(readonly=True, copy=False)

    _quantity_positive = models.Constraint('CHECK(quantity > 0)',
                                           'The quantity must be positive.')


class M365TenantPurchase(models.Model):
    _inherit = 'm365.tenant'

    pc_order_ids = fields.One2many('m365.pc.order', 'tenant_id', string="Subscription Orders")
    pc_order_count = fields.Integer(compute='_compute_pc_order_count')

    @api.depends('pc_order_ids')
    def _compute_pc_order_count(self):
        for rec in self:
            rec.pc_order_count = len(rec.pc_order_ids)

    def _sku_units(self, graph_sku_id):
        """Licenses (total) of a Graph skuId in the tenant."""
        self.ensure_one()
        if not graph_sku_id:
            return 0
        skus = self.env['m365.sku'].sudo().search([('tenant_id', '=', self.id),
                                                   ('sku_id', '=ilike', graph_sku_id)])
        return sum(skus.filtered(lambda s: s.status != 'Deleted').mapped('enabled_units'))

    def _auto_product_name(self, part, graph_sku_id):
        """Name of a license unknown to the product catalogue: the Partner Center offer
        with the same Graph skuId, when there is one."""
        offer = self.env['m365.pc.offer'].sudo().with_context(active_test=False).search(
            [('provisioning_id', '=ilike', graph_sku_id)], limit=1)
        if offer:
            return offer.sku_title or offer.name
        return super()._auto_product_name(part, graph_sku_id)

    def _purchase_channel(self):
        account = self.sudo().partner_account_id or self._default_partner_account()
        return account.purchase_channel if account else 'ingram'

    def _buy_action(self, product=None):
        """Buy Licenses: through Partner Center or Ingram Micro (Partner Center account)."""
        self.ensure_one()
        if self._purchase_channel() != 'partner_center':
            return super()._buy_action(product)
        self._check_admin()
        account = self.sudo().partner_account_id or self._default_partner_account()
        context = {'default_tenant_id': self.id, 'default_account_id': account.id}
        if product:
            graph_ids = self.env['m365.sku'].search([('tenant_id', '=', self.id),
                                                     ('product_id', '=', product.id)]).mapped(
                'sku_id')
            offer = self.env['m365.pc.offer'].search(
                [('account_id', '=', account.id),
                 ('country', 'in', [self._catalog_country(account), False]),
                 ('provisioning_id', 'in', [g.lower() for g in graph_ids])],
                order='term_duration desc, billing_cycle desc', limit=1) if graph_ids else False
            if offer:
                context['default_line_ids'] = [(0, 0, {'offer_id': offer.id, 'quantity': max(
                    offer.min_quantity, 1)})]
        return {
            'type': 'ir.actions.act_window',
            'name': _("Buy Licenses"),
            'res_model': 'm365.pc.order',
            'view_mode': 'form',
            'target': 'current',
            'context': context,
        }

    def action_view_pc_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_pc_order')
        action['domain'] = [('tenant_id', '=', self.id)]
        action['context'] = {'default_tenant_id': self.id}
        return action


class M365DashboardPurchase(models.AbstractModel):
    _inherit = 'm365.dashboard'

    @api.model
    def _get_alerts(self, tenants, skus):
        """Partner Center channel: the alerts of products running out lead to Buy
        Licenses as well."""
        alerts = super()._get_alerts(tenants, skus)
        if self.env.user.has_group(ADMIN_GROUP) and self.env['m365.partner.account'].search_count(
                [('purchase_channel', '=', 'partner_center')]):
            for alert in alerts:
                if alert.get('kind') in ('sku_empty', 'sku_low') and alert.get('sku_id'):
                    alert['buy_sku_id'] = alert['sku_id']
        return alerts
