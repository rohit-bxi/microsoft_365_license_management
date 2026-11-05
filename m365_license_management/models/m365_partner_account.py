# -*- coding: utf-8 -*-
"""Microsoft Partner Center (CSP) account, used to create new customer tenants.

=========================================  ===========================================
Step                                       Partner Center endpoint (app-only auth)
=========================================  ===========================================
Test the connection                        GET  /v1/profiles/legalbusiness
Domain availability                        HEAD /v1/domains/{domain}
Address validation                         POST /v1/validations/address
Create the customer (tenant + admin user)  POST /v1/customers
=========================================  ===========================================

Reference: https://learn.microsoft.com/en-us/partner-center/developer/create-a-customer
"""
import time
import uuid
from datetime import timedelta

import requests

from odoo import _, api, fields, models

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
PC_APPLICATION = 'Odoo Microsoft 365 License Management'


class M365PartnerCenterTimeout(M365GraphError):
    """Partner Center did not answer: retry the call with the same MS-RequestId."""


class M365PartnerAccount(models.Model):
    """Microsoft Partner Center (CSP) account the customer tenants are created with."""
    _name = 'm365.partner.account'
    _description = 'Microsoft Partner Center Account'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string="Company",
                                 default=lambda self: self.env.company)
    partner_tenant_id = fields.Char(
        string="Partner Directory (Tenant) ID", required=True, copy=False, tracking=True,
        help="Directory ID (or primary domain) of your Partner Center (CSP) tenant.")
    client_id = fields.Char(string="Application (Client) ID", required=True, copy=False,
                            tracking=True)
    client_secret = fields.Char(string="Client Secret", copy=False, groups=ADMIN_GROUP)
    authority_url = fields.Char(string="Authority URL", required=True,
                                default='https://login.microsoftonline.com')
    api_url = fields.Char(string="Partner Center API URL", required=True,
                          default='https://api.partnercenter.microsoft.com')
    api_scope = fields.Char(string="Partner Center Scope", required=True,
                            default='https://api.partnercenter.microsoft.com/.default')
    access_token = fields.Char(copy=False, groups='base.group_system')
    token_expiry = fields.Datetime(copy=False, groups='base.group_system')
    state = fields.Selection([
        ('draft', 'Not Tested'),
        ('connected', 'Connected'),
        ('error', 'Connection Error'),
    ], string="Status", default='draft', readonly=True, copy=False, tracking=True)
    partner_name = fields.Char(string="Partner Organisation", readonly=True, copy=False)
    last_error = fields.Text(string="Last Error", readonly=True, copy=False)
    default_culture = fields.Char(string="Default Culture", required=True, default='en-US',
                                  help="Culture of the new customers, e.g. en-US or en-IN.")
    default_language = fields.Char(string="Default Language", required=True, default='en',
                                   help="Two letter language code of the new customers.")
    enable_gdap = fields.Boolean(
        string="Create GDAP Relationship", default=True,
        help="Ask Microsoft to create the default granular delegated admin (GDAP) "
             "relationship with each new customer, so that you can administer it.")
    tenant_ids = fields.One2many('m365.tenant', 'partner_account_id',
                                 string="Customer Tenants")
    tenant_count = fields.Integer(compute='_compute_tenant_count')

    @api.depends('tenant_ids')
    def _compute_tenant_count(self):
        for rec in self:
            rec.tenant_count = len(rec.tenant_ids)

    def write(self, vals):
        # Changing the credentials invalidates the cached token
        if {'partner_tenant_id', 'client_id', 'client_secret', 'authority_url',
                'api_scope'} & set(vals):
            vals = dict(vals, access_token=False, token_expiry=False, state='draft')
        return super().write(vals)

    # ------------------------------------------------------------------
    # Partner Center client
    # ------------------------------------------------------------------
    @staticmethod
    def _format_error(resp):
        """Partner Center errors: ``{"code", "errorName", "description"}``."""
        try:
            data = resp.json()
        except ValueError:
            return "HTTP %s: %s" % (resp.status_code, (resp.text or '')[:300])
        if isinstance(data, dict) and data.get('description'):
            return "HTTP %s - %s (%s): %s" % (resp.status_code, data.get('errorName'),
                                              data.get('code'), data['description'])
        return "HTTP %s: %s" % (resp.status_code, str(data)[:300])

    def _get_access_token(self, force=False):
        """Return a (cached) app-only access token for the Partner Center API."""
        self.ensure_one()
        rec = self.sudo()
        now = fields.Datetime.now()
        if (not force and rec.access_token and rec.token_expiry
                and rec.token_expiry > now + timedelta(minutes=5)):
            return rec.access_token
        if not (rec.partner_tenant_id and rec.client_id and rec.client_secret):
            raise M365GraphError(_("Partner Directory ID, Client ID and Client Secret are "
                                   "required."))
        url = "%s/%s/oauth2/v2.0/token" % (rec.authority_url.rstrip('/'),
                                           rec.partner_tenant_id.strip())
        try:
            resp = requests.post(url, data={
                'client_id': rec.client_id.strip(),
                'client_secret': rec.client_secret.strip(),
                'scope': rec.api_scope.strip(),
                'grant_type': 'client_credentials',
            }, timeout=30)
        except requests.RequestException as exc:
            raise M365GraphError(_("Unable to reach the Microsoft identity platform: %s", exc))
        if resp.status_code != 200:
            raise M365GraphError(_("Authentication failed. %s",
                                   self.env['m365.tenant']._format_error(resp)))
        payload = resp.json()
        rec.write({
            'access_token': payload['access_token'],
            'token_expiry': now + timedelta(seconds=int(payload.get('expires_in', 3600))),
        })
        return rec.access_token

    def _pc_request(self, method, path, json_body=None, request_id=None, expect=()):
        """Call the Partner Center REST API.

        :param request_id: MS-RequestId; send the one of a call that got no answer again
            so that Microsoft does not execute the operation twice
        :param expect: error statuses returned instead of raised (e.g. 404)
        :return: (HTTP status, JSON body or ``{}``)
        :raise M365PartnerCenterTimeout: no answer, retry with the same request ID
        """
        self.ensure_one()
        url = "%s/%s" % (self.api_url.rstrip('/'), path.lstrip('/'))
        request_id = request_id or str(uuid.uuid4())
        retries, refreshed = 0, False
        while True:
            headers = {
                'Authorization': 'Bearer %s' % self._get_access_token(),
                'Accept': 'application/json',
                'MS-RequestId': request_id,
                'MS-CorrelationId': str(uuid.uuid4()),
                'MS-PartnerCenter-Application': PC_APPLICATION,
                'X-Locale': 'en-US',
            }
            try:
                resp = requests.request(method, url, headers=headers, json=json_body,
                                        timeout=120)
            except requests.RequestException as exc:
                raise M365PartnerCenterTimeout(
                    _("Partner Center did not answer: %s", exc)) from None
            if resp.status_code == 401 and not refreshed:
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
                raise M365GraphError(self._format_error(resp))
            return resp.status_code, (resp.json() if resp.content else {})

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            _status, profile = self._pc_request('GET', '/v1/profiles/legalbusiness')
        except M365GraphError as exc:
            self.write({'state': 'error', 'last_error': str(exc)})
            return self.env['m365.tenant']._notify(
                _("Partner Center connection failed: %s", exc), 'danger', reload=True)
        self.write({'state': 'connected', 'last_error': False,
                    'partner_name': profile.get('companyName')})
        return self.env['m365.tenant']._notify(
            _("Connected to Partner Center as %s.", profile.get('companyName') or self.name))

    def action_view_tenants(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_tenant')
        action['domain'] = [('partner_account_id', '=', self.id)]
        action['context'] = {'default_partner_account_id': self.id,
                             'default_tenant_type': 'new'}
        return action
