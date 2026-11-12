# -*- coding: utf-8 -*-
"""Ingram Micro Reseller API account: Microsoft subscriptions bought from Ingram Micro.

=========================================  ===============================================
Step                                       Ingram Micro endpoint
=========================================  ===============================================
Access token                               GET  /oauth/oauth20/token (client credentials)
Catalog of the Microsoft subscriptions     GET  /resellers/v6/catalog?type=IM::subscription
Part numbers of a plan                     GET  /resellers/v6/catalog/details?planId=
Prices                                     POST /resellers/v6/catalog/priceandavailability
Order                                      POST /resellers/v7/orders (result by webhook)
Order lookup by PO number                  GET  /resellers/v6/orders/search
Cancel an order not yet released           DELETE /resellers/v6/orders/{orderNumber}
Invoices                                   GET  /resellers/v6/invoices, v6.1/invoices/{n}
Renewals                                   POST /resellers/v6/renewals/search
Vendor fields of a quote                   GET  /resellers/v6/q2o/validatequote
=========================================  ===============================================

Every call sends ``IM-CustomerNumber``, ``IM-CountryCode``, ``IM-SenderID`` (and
``IM-ApplicationID``, its name in the invoice calls) and a new ``IM-CorrelationID``. The fields Microsoft needs on an order (tenant, Customer Agreement
acceptance, ...) are not in the public specification: Ingram Micro gives their names,
entered as *Vendor Fields* on the account.

Reference: https://developer.ingrammicro.com/reseller
"""
import re
import urllib.parse
import time
import uuid
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
CATALOG_PAGE_SIZE = 100
CATALOG_MAX_PAGES = 100
PLAN_SUFFIX_RE = re.compile(r'\s*\([^)]*\)\s*$')
TERM_KEYS = [('P1M', '1 Month'), ('P1Y', '1 Year'), ('P3Y', '3 Years')]
# "<plan> 1 Year", "<plan> 12 months", "<plan> P1Y": the period at the end of a plan text
TERM_SUFFIX_RE = re.compile(r'^(?P<name>.+?)\s+(?:(?P<iso>P\d+[MY])|(?P<n>\d+)\s*'
                            r'(?P<unit>months?|years?|mth|yr))\s*$', re.I)


def term_key(period, unit):
    """ISO key of a subscription period: (1, 'YEARS') -> 'P1Y'; False when unknown."""
    unit = (unit or '').strip().lower()
    letter = 'Y' if unit.startswith(('y', 'ann')) else 'M' if unit.startswith('m') else ''
    try:
        number = int(float(period))
    except (TypeError, ValueError):
        return False
    if letter == 'M' and number % 12 == 0:
        number, letter = number // 12, 'Y'
    return 'P%s%s' % (number, letter) if letter and number > 0 else False


class M365IngramError(M365GraphError):
    """Authentication or call of the Ingram Micro API failed."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# Apigee (gateway of the Ingram Micro API): the client ID is valid but its app has no API
# product for the path called
NO_API_PRODUCT = 'InvalidAPICallAsNoApiProductMatchFound'


class M365IngramTimeout(M365IngramError):
    """Ingram Micro did not answer: the call may have been executed."""


class M365IngramAccount(models.Model):
    _name = 'm365.ingram.account'
    _description = 'Ingram Micro Account'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string="Company",
                                 default=lambda self: self.env.company)
    api_url = fields.Char(string="API URL", required=True,
                          default='https://api.ingrammicro.com:443',
                          help="Production: https://api.ingrammicro.com:443\n"
                               "Sandbox app (API products marked Sandbox): "
                               "https://api.ingrammicro.com:443/sandbox\n"
                               "The token is always requested at the root of the host.")
    client_id = fields.Char(string="Client ID", required=True, copy=False, tracking=True)
    client_secret = fields.Char(string="Client Secret", copy=False, groups=ADMIN_GROUP)
    customer_number = fields.Char(string="Customer Number", required=True, tracking=True,
                                  help="Your Ingram Micro customer (reseller) number: "
                                       "IM-CustomerNumber.")
    country_code = fields.Char(string="Country Code", required=True, size=2, default='IN',
                               help="Two letter ISO country of your Ingram Micro account: "
                                    "IM-CountryCode.")
    sender_id = fields.Char(string="Sender ID", required=True, default='Odoo Microsoft 365',
                            help="Name of the application sending the calls: IM-SenderID.")
    default_plan_id = fields.Many2one(
        'm365.ingram.plan', string="Default Plan", domain="[('account_id', '=', id)]",
        help="Plan bought when the Subscriptions of a customer give a quantity only, and "
             "license \"default\" of the users, e.g. Microsoft 365 Business Basic (no Teams) "
             "(NCE COM MTH).")
    default_term = fields.Selection(
        TERM_KEYS, string="Default Subscription Period", default='P1Y',
        help="Period of the order lines when the plan can be bought for several periods "
             "and none is given.")
    vendor_name = fields.Char(string="Vendor", required=True, default='Microsoft',
                              help="Vendor of the subscriptions read from the catalog.")
    catalog_type = fields.Char(
        string="Catalog Type", default='IM::subscription',
        help="Value of the type parameter of the catalog calls. The specification lists "
             "IM::physical, IM::digital and IM::any, while its subscription examples use "
             "IM::subscription. Leave empty not to send it.")
    require_relationship = fields.Boolean(
        string="Require Ingram Micro Relationship", default=True,
        help="An order can be submitted only for tenants whose relationship with Ingram "
             "Micro (indirect provider) is confirmed.")
    require_mca = fields.Boolean(
        string="Require Customer Agreement", default=True,
        help="An order can be submitted only for tenants whose acceptance of the Microsoft "
             "Customer Agreement is recorded (who and when).")
    last_invoice_sync = fields.Datetime(string="Last Invoice Sync", readonly=True, copy=False)
    last_renewal_sync = fields.Datetime(string="Last Renewal Sync", readonly=True, copy=False)
    bill_to_address_id = fields.Char(string="Bill-to Address ID",
                                     help="Suffix of your billing address at Ingram Micro, "
                                          "if you have several.")
    webhook_secret = fields.Char(string="Webhook Secret Key", copy=False, groups=ADMIN_GROUP,
                                 help="Secret key of the order webhook, from the Ingram Micro "
                                      "developer portal. Notifications without a valid "
                                      "signature are refused.")
    webhook_url = fields.Char(string="Webhook URL", compute='_compute_webhook_url',
                              help="Register this URL for the order events in the Ingram "
                                   "Micro developer portal.")
    access_token = fields.Char(copy=False, groups='base.group_system')
    token_expiry = fields.Datetime(copy=False, groups='base.group_system')
    state = fields.Selection([
        ('draft', 'Not Tested'),
        ('connected', 'Connected'),
        ('error', 'Connection Error'),
    ], string="Status", default='draft', readonly=True, copy=False, tracking=True)
    last_error = fields.Text(string="Last Error", readonly=True, copy=False)
    last_catalog_sync = fields.Datetime(string="Last Catalog Sync", readonly=True, copy=False)
    vmf_attribute_ids = fields.One2many('m365.ingram.vmf.attribute', 'account_id',
                                        string="Vendor Fields", copy=True)
    plan_ids = fields.One2many('m365.ingram.plan', 'account_id', string="Plans")
    plan_count = fields.Integer(compute='_compute_counts')
    order_count = fields.Integer(compute='_compute_counts')

    def _compute_counts(self):
        Order = self.env['m365.ingram.order']
        for rec in self:
            rec.plan_count = len(rec.plan_ids)
            rec.order_count = Order.search_count([('account_id', '=', rec.id)])

    @api.constrains('customer_number', 'country_code', 'sender_id')
    def _check_header_values(self):
        """Limits of the IM-* headers in the specification."""
        for rec in self:
            if len((rec.customer_number or '').strip()) > 10:
                raise ValidationError(_("The Ingram Micro customer number has at most 10 "
                                        "characters."))
            if not re.match(r'^[A-Za-z]{2}$', (rec.country_code or '').strip()):
                raise ValidationError(_("The country code is the two letter ISO code, e.g. "
                                        "IN."))
            if len((rec.sender_id or '').strip()) > 32:
                raise ValidationError(_("The sender ID has at most 32 characters."))

    def _catalog_params(self, **params):
        params = dict(params, vendor=self.vendor_name)
        if (self.catalog_type or '').strip():
            params['type'] = self.catalog_type.strip()
        return params

    def _compute_webhook_url(self):
        base = self.env['m365.tenant']._public_base_url()
        for rec in self:
            rec.webhook_url = "%s/m365/ingram/webhook/%s" % (base, rec.id) if rec.id else False

    def write(self, vals):
        reset = bool({'api_url', 'client_id', 'client_secret'} & set(vals))
        if reset:
            vals = dict(vals, state='draft')
        res = super().write(vals)
        if reset:
            self.sudo().write({'access_token': False, 'token_expiry': False})
        return res

    @api.model
    def _get_default(self):
        return self.search([], limit=1)

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------
    def _format_error(self, resp):
        """Ingram errors: ``{"errors": [{"type", "message", "fields": [...]}]}``."""
        try:
            data = resp.json()
        except ValueError:
            return "HTTP %s: %s" % (resp.status_code, (resp.text or '')[:300])
        errors = data.get('errors') if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            parts = []
            for err in errors:
                detail = "; ".join("%s: %s" % (f.get('field'), f.get('message'))
                                   for f in err.get('fields') or [] if isinstance(f, dict))
                text = err.get('message') or err.get('type') or ''
                parts.append("%s (%s)" % (text, detail) if detail else text)
            return "HTTP %s: %s" % (resp.status_code, " ".join(parts))
        fault = data.get('fault') if isinstance(data, dict) else None
        if isinstance(fault, dict) and NO_API_PRODUCT in str(
                (fault.get('detail') or {}).get('errorcode') or ''):
            return self.env._(
                "HTTP %(status)s: Ingram Micro refused the call: the app of this Client ID "
                "has no API product for %(path)s (%(fault)s). The credentials are valid. "
                "Check that the Client ID and Secret are the ones of the app in the Ingram "
                "Micro developer portal; that the API URL matches the app (a Sandbox app: "
                "https://api.ingrammicro.com:443/sandbox, a production app: "
                "https://api.ingrammicro.com:443); and that the app is approved for its API "
                "products (adding products needs a new approval by Ingram Micro). Then press "
                "Test Connection again.", status=resp.status_code,
                path=urllib.parse.urlparse(resp.url or '').path or '-',
                fault=fault.get('faultstring') or NO_API_PRODUCT)
        if isinstance(data, dict) and (data.get('error') or data.get('fault')):
            fault = data.get('fault') or {}
            return "HTTP %s: %s" % (resp.status_code, data.get('error_description')
                                    or data.get('error') or fault.get('faultstring') or '')
        return "HTTP %s: %s" % (resp.status_code, str(data)[:300])

    def _token_url(self):
        """OAuth token endpoint: at the root of the host, also for the sandbox, whose API
        URL ends with /sandbox (``/sandbox/oauth/...`` does not exist)."""
        self.ensure_one()
        url = urllib.parse.urlparse(self.api_url.strip())
        return "%s://%s/oauth/oauth20/token" % (url.scheme or 'https', url.netloc)

    def _get_access_token(self, force=False):
        """Cached client-credentials token of the Ingram Micro API."""
        self.ensure_one()
        rec = self.sudo()
        now = fields.Datetime.now()
        if (not force and rec.access_token and rec.token_expiry
                and rec.token_expiry > now + timedelta(minutes=2)):
            return rec.access_token
        if not (rec.client_id and rec.client_secret):
            raise M365IngramError(_("The client ID and secret of the Ingram Micro account "
                                    "are required."))
        try:
            # As specified by Ingram Micro: GET with the credentials as query parameters
            resp = requests.get(rec._token_url(), params={
                'grant_type': 'client_credentials',
                'client_id': rec.client_id.strip(),
                'client_secret': rec.client_secret.strip(),
            }, timeout=30)
        except requests.RequestException as exc:
            raise M365IngramError(_("Unable to reach Ingram Micro: %s", exc)) from None
        if resp.status_code != 200:
            raise M365IngramError(_("Ingram Micro authentication failed. %s",
                                    self._format_error(resp)), resp.status_code)
        payload = resp.json()
        try:
            expires_in = int(float(payload.get('expires_in') or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        rec.write({'access_token': payload['access_token'],
                   'token_expiry': now + timedelta(seconds=expires_in)})
        return rec.access_token

    def _im_request(self, method, path, params=None, json_body=None, expect=()):
        """Call the Ingram Micro API.

        :param expect: error statuses returned instead of raised
        :return: (HTTP status, JSON body or ``{}``)
        :raise M365IngramTimeout: no answer: the call may have been executed
        """
        self.ensure_one()
        url = "%s/%s" % (self.api_url.rstrip('/'), path.lstrip('/'))
        retries, refreshed = 0, False
        while True:
            headers = {
                'Authorization': 'Bearer %s' % self._get_access_token(),
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'IM-CustomerNumber': self.customer_number.strip(),
                'IM-CountryCode': self.country_code.strip().upper(),
                'IM-SenderID': self.sender_id.strip(),
                'IM-ApplicationID': self.sender_id.strip(),  # name in the invoice calls
                'IM-CorrelationID': uuid.uuid4().hex,  # 32 characters at most
            }
            try:
                resp = requests.request(method, url, headers=headers, params=params,
                                        json=json_body, timeout=90)
            except requests.RequestException as exc:
                raise M365IngramTimeout(_("Ingram Micro did not answer: %s", exc)) from None
            if resp.status_code == 401 and not refreshed and NO_API_PRODUCT not in resp.text:
                # Expired token; a missing API product is not solved by a new token
                refreshed = True
                self._get_access_token(force=True)
                continue
            if resp.status_code in (429, 503) and retries < 3:
                retries += 1
                try:
                    wait = int(resp.headers.get('Retry-After', 2))
                except ValueError:
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                continue
            if resp.status_code >= 400 and resp.status_code not in expect:
                raise M365IngramError(self._format_error(resp), resp.status_code)
            try:
                data = resp.json() if resp.content else {}
            except ValueError:
                data = {}
            return resp.status_code, data

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _check_admin(self):
        self.env['m365.tenant']._check_admin()

    def action_test_connection(self):
        self._check_admin()
        self.ensure_one()
        try:
            _status, data = self._im_request('GET', '/resellers/v6/catalog',
                                             params=self._catalog_params(pageSize=1,
                                                                         pageNumber=1))
        except M365IngramError as exc:
            self.write({'state': 'error', 'last_error': str(exc)})
            return self.env['m365.tenant']._notify(
                _("Ingram Micro connection failed: %s", exc), 'danger')
        self.write({'state': 'connected', 'last_error': False})
        return self.env['m365.tenant']._notify(_(
            "Connected to Ingram Micro: %s %s subscription record(s) in the catalog.",
            data.get('recordsFound', '?'), self.vendor_name))

    def action_sync_plans(self):
        self._check_admin()
        for rec in self:
            count = rec._sync_plans()
            rec.message_post(body=_("%s plan(s) read from the Ingram Micro catalog.", count))
        return self.env['m365.tenant']._notify(_("Catalog synchronised."))

    def action_view_plans(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_ingram_plan')
        action['domain'] = [('account_id', '=', self.id)]
        action['context'] = {'default_account_id': self.id}
        return action

    def action_import_vmf_wizard(self):
        self._check_admin()
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Import Vendor Fields from a Quote"),
            'res_model': 'm365.ingram.vmf.import.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_account_id': self.id},
        }

    def _import_vmf_from_quote(self, quote_number):
        """Vendor fields required by Microsoft, from Ingram's validation of a quote.

        :return: number of fields added
        """
        self.ensure_one()
        _status, data = self._im_request('GET', '/resellers/v6/q2o/validatequote',
                                         params={'quoteNumber': quote_number.strip()})
        known = {(a.level, a.name) for a in self.vmf_attribute_ids}
        Attr = self.env['m365.ingram.vmf.attribute']
        added = 0
        fields_found = [('header', a) for a in data.get('vmfAdditionalAttributes') or []]
        for line in data.get('lines') or []:
            fields_found += [('line', a) for a in line.get('vmfAdditionalAttributesLines') or []]
        for level, attr in fields_found:
            name = (attr.get('attributeName') or '').strip() if isinstance(attr, dict) else ''
            if not name or (level, name) in known:
                continue
            known.add((level, name))
            Attr.create({'account_id': self.id, 'level': level, 'name': name,
                         'description': attr.get('attributeDescription') or False,
                         'source': 'fixed', 'value': attr.get('attributeValue') or False})
            added += 1
        return added

    def action_view_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_ingram_order')
        action['domain'] = [('account_id', '=', self.id)]
        action['context'] = {'default_account_id': self.id}
        return action

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------
    @staticmethod
    def _catalog_plans(data):
        """(group name, plan) of a catalog page, with or without the group level."""
        for entry in data.get('subscriptionCatalog') or []:
            if not isinstance(entry, dict):
                continue
            plans = entry.get('plans')
            if plans is None and entry.get('planId'):
                plans = [entry]
            for plan in plans or []:
                if isinstance(plan, dict) and plan.get('planId'):
                    yield entry.get('groupName') or plan.get('groupName') or False, plan

    @staticmethod
    def _plan_term(plan):
        periods = plan.get('subscriptionPeriodSummary') or plan.get('subscriptionPeriod') or []
        return ", ".join("%s %s" % (p.get('subscriptionPeriod') or '',
                                    (p.get('subscriptionPeriodUnit') or '').lower())
                         for p in periods if isinstance(p, dict)).strip() or False

    @staticmethod
    def _base_name(name):
        """``Microsoft 365 E3 (NCE COM MTH)`` -> ``microsoft 365 e3``"""
        return PLAN_SUFFIX_RE.sub('', name or '').strip().lower()

    def _match_plan(self, text):
        """Plan named by a text of the customer file: plan ID, vendor or Ingram part number
        (e.g. CFQ7TTC0LH18:000P), plan name, or plan name without its (NCE ...) suffix,
        optionally followed by the period (\"... 1 Year\", \"... P1M\").

        :return: (plan, period or empty, error or False)
        """
        self.ensure_one()
        Plan, Term = self.env['m365.ingram.plan'], self.env['m365.ingram.plan.term']
        text, key = (text or '').strip(), False
        match = TERM_SUFFIX_RE.match(text)
        if match:
            key = (match.group('iso').upper() if match.group('iso')
                   else term_key(match.group('n'), match.group('unit')))
            text = match.group('name').strip()
        domain = [('account_id', '=', self.id)]
        plans = Plan.search(domain + ['|', '|', ('plan_id', '=', text),
                                      ('vendor_part_number', '=ilike', text),
                                      ('ingram_part_number', '=ilike', text)])
        if not plans:
            plans = Plan.search(domain + [('name', '=ilike', text)])
        if not plans:
            # Name without the (NCE ...) suffix; the text may end in brackets itself, e.g.
            # "Microsoft 365 Business Basic (no Teams)"
            candidates = Plan.search(domain)
            for wanted_name in (text.lower(), self._base_name(text)):
                plans = candidates.filtered(lambda p: self._base_name(p.name) == wanted_name)
                if plans:
                    break
        if not plans:
            return Plan, Term, _("Plan \"%s\" not found in the Ingram Micro catalog.", text)
        wanted = key or self.default_term
        if len(plans) > 1:
            # One plan per period (e.g. NCE COM MTH and NCE COM ANN): the period chooses
            by_term = plans.filtered(lambda p: wanted in p.term_ids.mapped('key'))
            plans = by_term if len(by_term) == 1 else plans
        if len(plans) > 1:
            return Plan, Term, _("\"%(plan)s\" matches several plans: %(list)s. Use the plan "
                                 "ID or the part number.", plan=text, list="; ".join(
                                     "%s (%s)" % (p.display_name, p.plan_id) for p in plans))
        term = plans.term_ids.filtered(lambda t: t.key == wanted)[:1]
        if key and not term and plans.term_ids:
            return Plan, Term, _("%(plan)s is not sold for %(period)s.", plan=plans.name,
                                 period=dict(TERM_KEYS).get(key, key))
        return plans, term or plans._default_term(), False

    def _sync_plans(self):
        """Read the Microsoft subscriptions of the catalog. :return: number of plans"""
        self.ensure_one()
        Plan = self.env['m365.ingram.plan'].sudo().with_context(active_test=False)
        existing = {p.plan_id: p for p in Plan.search([('account_id', '=', self.id)])}
        products = {}
        for product in self.env['m365.product'].sudo().search([]):
            products.setdefault(self._base_name(product.name), product)
        now = fields.Datetime.now()
        seen, complete = set(), False
        for page in range(1, CATALOG_MAX_PAGES + 1):
            _status, data = self._im_request('GET', '/resellers/v6/catalog',
                                             params=self._catalog_params(
                                                 pageSize=CATALOG_PAGE_SIZE, pageNumber=page,
                                                 showGroupInfo='false'))
            entries = data.get('subscriptionCatalog') or []
            for group, item in self._catalog_plans(data):
                plan_id = str(item['planId'])
                seen.add(plan_id)
                vals = {
                    'name': item.get('planName') or plan_id,
                    'description': item.get('planDescription') or False,
                    'group_name': group,
                    'term': self._plan_term(item),
                    'active': True,
                    'last_refresh': now,
                }
                plan = existing.get(plan_id)
                if plan:
                    plan.write(vals)
                else:
                    plan = existing[plan_id] = Plan.create(
                        dict(vals, account_id=self.id, plan_id=plan_id))
                plan._set_terms(item.get('subscriptionPeriodSummary')
                                or item.get('subscriptionPeriod'))
                if not plan.product_id:
                    product = products.get(self._base_name(plan.name))
                    if product:
                        plan.write({'product_id': product.id, 'product_suggested': True})
            total = data.get('recordsFound') or 0
            if len(entries) < CATALOG_PAGE_SIZE or page * CATALOG_PAGE_SIZE >= total:
                complete = True
                break
        if complete:  # plans no longer sold
            Plan.browse([p.id for pid, p in existing.items() if pid not in seen]).write(
                {'active': False})
        self.write({'last_catalog_sync': now, 'state': 'connected', 'last_error': False})
        return len(seen)


class M365IngramVmfAttribute(models.Model):
    """Field required by Microsoft on the orders (vendor mandatory field), as named by
    Ingram Micro, and where Odoo takes its value."""
    _name = 'm365.ingram.vmf.attribute'
    _description = 'Ingram Micro Vendor Field'
    _order = 'level, sequence, id'

    account_id = fields.Many2one('m365.ingram.account', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    level = fields.Selection([('header', 'Order'), ('line', 'Order Line')], required=True,
                             default='header')
    name = fields.Char(string="Attribute Name", required=True,
                       help="attributeName expected by Ingram Micro.")
    description = fields.Char(help="Description given by Ingram Micro.")
    source = fields.Selection([
        ('tenant_id', 'Tenant Directory ID'),
        ('domain', 'Tenant Domain'),
        ('customer_name', 'Customer Name'),
        ('contact_first_name', 'Contact First Name'),
        ('contact_last_name', 'Contact Last Name'),
        ('contact_email', 'Contact Email'),
        ('contact_phone', 'Contact Phone'),
        ('order_date', 'Order Date (YYYY-MM-DD)'),
        ('mca_first_name', 'Customer Agreement: First Name'),
        ('mca_last_name', 'Customer Agreement: Last Name'),
        ('mca_email', 'Customer Agreement: Email'),
        ('mca_phone', 'Customer Agreement: Phone'),
        ('mca_date', 'Customer Agreement: Date (YYYY-MM-DD)'),
        ('plan_id', 'Plan ID (lines)'),
        ('quantity', 'Quantity (lines)'),
        ('term', 'Subscription Period, as in the catalog (lines)'),
        ('term_months', 'Subscription Period in Months (lines)'),
        ('term_key', 'Subscription Period, ISO e.g. P1Y (lines)'),
        ('fixed', 'Fixed Value'),
    ], string="Value", required=True, default='fixed')
    value = fields.Char(string="Fixed Value")

    def _payload(self, order, line=None):
        self.ensure_one()
        return {'attributeName': self.name,
                'attributeValue': str(order._vmf_value(self.source, self.value, line) or '')}


class M365IngramVmfImportWizard(models.TransientModel):
    _name = 'm365.ingram.vmf.import.wizard'
    _description = 'Import Ingram Micro Vendor Fields from a Quote'

    account_id = fields.Many2one('m365.ingram.account', required=True, readonly=True)
    quote_number = fields.Char(string="Quote Number", required=True,
                               help="An Ingram Micro quote of Microsoft subscriptions: its "
                                    "validation lists the fields Microsoft requires.")

    def action_import(self):
        self.ensure_one()
        self.account_id._check_admin()
        try:
            added = self.account_id._import_vmf_from_quote(self.quote_number)
        except M365IngramError as exc:
            return self.env['m365.tenant']._notify(
                _("The quote could not be validated: %s", exc), 'danger')
        return self.env['m365.tenant']._notify(
            _("%s vendor field(s) added: choose where Odoo takes their values.", added))
