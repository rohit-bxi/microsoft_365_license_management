# -*- coding: utf-8 -*-
"""Connection of the tenants through the shared multi-tenant app of the partner.

Instead of an app registration in each tenant, a tenant in *Shared App* mode uses the
multi-tenant app of its Partner Center account. A Global Administrator of the tenant
grants that app admin consent once::

    Grant Consent --> Microsoft admin consent page --> /m365/consent/callback
                      (Global Administrator signs in)  --verified--> Shared App, Test Connection

* The link carries a signed state (tenant, nonce, expiry). Only the last link of a
  tenant is valid, until it is used or expires.
* The tenant switches to the shared app only once Odoo has read it with that app, so a
  forged or replayed callback changes nothing. Microsoft can take a moment to apply a
  new consent: *Check Consent* retries later.
* *Own App* (an app registration in the tenant, entered on the tenant) still works and
  stays the default.

Reference: https://learn.microsoft.com/en-us/entra/identity-platform/v2-admin-consent
"""
import binascii
import re
import secrets
import time
import urllib.parse
from datetime import timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import consteq, hash_sign, verify_hash_signed

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
CONSENT_CALLBACK_PATH = '/m365/consent/callback'
CONSENT_SCOPE = 'm365_license_management.admin_consent'
CONSENT_VALIDITY = timedelta(days=7)
# The callback tries a few times: Microsoft can take a few seconds to apply a consent
CALLBACK_ATTEMPTS = 3
CALLBACK_WAIT = 5  # seconds
GUID_RE = re.compile(r'^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$', re.I)


class M365TenantConsent(models.Model):
    _inherit = 'm365.tenant'

    connection_mode = fields.Selection([
        ('own_app', 'Own App Registration'),
        ('shared_app', 'Shared App'),
    ], string="Connection", default='own_app', required=True, tracking=True,
        help="Own App Registration: an app registered in this tenant, with its client ID "
             "and secret entered here.\n"
             "Shared App: the multi-tenant app (Configuration > Tenants > Shared App), after a "
             "Global Administrator of the tenant granted it consent (Grant Consent).")
    consent_nonce = fields.Char(copy=False, groups=ADMIN_GROUP)
    consent_expiry = fields.Datetime(string="Consent Link Valid Until", readonly=True,
                                     copy=False)
    consent_requested_by = fields.Many2one('res.users', string="Consent Requested By",
                                           readonly=True, copy=False)
    consent_date = fields.Datetime(string="Consent Granted On", readonly=True, copy=False)
    consent_url = fields.Char(
        string="Consent Link", compute='_compute_consent_url', groups=ADMIN_GROUP,
        help="Send this link to a Global Administrator of the tenant, or open it and sign "
             "in as one. Valid once, until the date above.")

    @api.depends('consent_nonce', 'consent_expiry', 'tenant_id', 'authority_url',
                 'graph_scope', 'partner_account_id')
    def _compute_consent_url(self):
        now = fields.Datetime.now()
        for rec in self:
            sudo = rec.sudo()
            pending = (sudo.consent_nonce and sudo.consent_expiry and sudo.consent_expiry > now
                       and sudo.tenant_id and sudo.partner_account_id.graph_client_id)
            rec.consent_url = sudo._consent_url() if pending else False

    @api.constrains('tenant_type', 'tenant_id', 'client_id', 'connection_mode')
    def _check_connection_ids(self):
        shared = self.filtered(lambda t: t.connection_mode == 'shared_app')
        super(M365TenantConsent, self - shared)._check_connection_ids()
        for rec in shared.filtered(lambda t: t.tenant_type == 'existing' and not t.tenant_id):
            raise UserError(_("The Directory (Tenant) ID is required for the tenant %s.",
                              rec.name))

    def write(self, vals):
        # The token belongs to the app: another app needs another token
        changed = self.browse()
        if 'connection_mode' in vals:
            changed = self.filtered(lambda t: t.connection_mode != vals['connection_mode'])
        elif 'partner_account_id' in vals:
            changed = self.sudo().filtered(
                lambda t: t.connection_mode == 'shared_app'
                and t.partner_account_id.id != vals['partner_account_id'])
        res = super().write(vals)
        if changed:
            changed.with_env(self.env).write({'state': 'draft'})
            changed._clear_token()
        return res

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    def _get_app_credentials(self):
        self.ensure_one()
        if self.connection_mode != 'shared_app':
            return super()._get_app_credentials()
        rec = self.sudo()
        if not rec.tenant_id:
            raise M365GraphError(_("The Directory (Tenant) ID is required."))
        if not rec.partner_account_id:
            raise M365GraphError(_("Select the shared app of the tenant %s.", rec.name))
        return rec.partner_account_id._shared_app_credentials()

    # ------------------------------------------------------------------
    # Consent link
    # ------------------------------------------------------------------
    @api.model
    def _public_base_url(self):
        """Public address of Odoo, used by Microsoft for redirects and notifications."""
        ICP = self.env['ir.config_parameter'].sudo()
        return (ICP.get_param('m365.webhook_base_url') or ICP.get_param('web.base.url')
                or '').rstrip('/')

    @api.model
    def _consent_redirect_uri(self):
        return self._public_base_url() + CONSENT_CALLBACK_PATH

    def _consent_state(self):
        """Signed state of the consent link: tenant, nonce and expiry."""
        self.ensure_one()
        rec = self.sudo()
        # Odoo datetimes are naive UTC; hash_sign() needs the real point in time
        expiry = rec.consent_expiry.replace(tzinfo=timezone.utc)
        return hash_sign(rec.env, CONSENT_SCOPE, {'tenant': rec.id, 'nonce': rec.consent_nonce},
                         expiration=expiry)

    def _consent_url(self):
        """Admin consent page of Microsoft for the shared app in this tenant."""
        self.ensure_one()
        rec = self.sudo()
        query = urllib.parse.urlencode({
            'client_id': rec.partner_account_id.graph_client_id.strip(),
            'scope': rec.graph_scope.strip(),
            'redirect_uri': self._consent_redirect_uri(),
            'state': rec._consent_state(),
        })
        return "%s/%s/v2.0/adminconsent?%s" % (rec.authority_url.rstrip('/'),
                                               rec.tenant_id.strip(), query)

    def action_grant_consent(self):
        """Create a consent link of the shared app and open it."""
        self._check_admin()
        self.ensure_one()
        if not self.filtered_domain(self._live_domain()):
            raise UserError(_("Create the tenant in Microsoft 365 first."))
        if not self.tenant_id:
            raise UserError(_("Enter the Directory (Tenant) ID of the tenant first."))
        rec = self.sudo()
        account = rec.partner_account_id or rec._default_partner_account()
        if not account:
            raise UserError(_("Add the shared app first (Configuration > Tenants > Shared "
                              "App)."))
        account._shared_app_credentials()  # configured?
        vals = {
            'consent_nonce': secrets.token_urlsafe(24),
            'consent_expiry': fields.Datetime.now() + CONSENT_VALIDITY,
            'consent_requested_by': self.env.uid,
        }
        if not rec.partner_account_id:
            vals['partner_account_id'] = account.id
        rec.write(vals)
        self._log('consent', 'info', _(
            "Consent link of the shared app %(account)s created, valid until %(date)s (UTC).",
            account=account.name, date=fields.Datetime.to_string(vals['consent_expiry'])))
        return {'type': 'ir.actions.act_url', 'url': rec._consent_url(), 'target': 'new'}

    def action_check_consent(self):
        """Connect through the shared app if the tenant has granted it consent."""
        self._check_admin()
        self.ensure_one()
        error = self._activate_shared_app()
        if error:
            return self._notify(_("The shared app cannot read the tenant yet: %s If consent "
                                  "was just granted, try again in a few minutes.", error),
                                'danger')
        action = self.action_test_connection()
        self._after_shared_app_connected()
        return action

    def _after_shared_app_connected(self):
        """Hook: the tenant was just connected through the shared app (consent granted)."""
        self.ensure_one()

    # ------------------------------------------------------------------
    # Callback of Microsoft
    # ------------------------------------------------------------------
    @api.model
    def _consent_tenant_from_state(self, state):
        """Tenant of a valid, unused and unexpired consent state, else an empty record."""
        Tenant = self.sudo().browse()
        try:
            values = verify_hash_signed(self.sudo().env, CONSENT_SCOPE, state or '')
        except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
            return Tenant
        if not isinstance(values, dict) or not isinstance(values.get('tenant'), int):
            return Tenant
        tenant = Tenant.with_context(active_test=False).browse(values['tenant']).exists()
        if not (tenant and tenant.consent_nonce and tenant.consent_expiry
                and tenant.consent_expiry > fields.Datetime.now()
                and consteq(str(values.get('nonce') or ''), tenant.consent_nonce)):
            return Tenant
        return tenant

    @api.model
    def _consent_callback(self, params):
        """Process the redirect of the Microsoft admin consent page.

        :param params: query parameters (``state``, ``tenant``, ``admin_consent`` or
            ``error`` and ``error_description``)
        :return: ``{'status': 'success' | 'failed' | 'invalid', 'message', 'tenant'}``
        """
        tenant = self._consent_tenant_from_state(params.get('state'))
        if not tenant:
            return {'status': 'invalid', 'tenant': tenant, 'message': _(
                "This consent link is not valid or has expired. Ask for a new link.")}
        user = tenant.consent_requested_by
        if user:
            # Chatter and audit log in the name of the administrator who sent the link,
            # not of the public user of the redirect
            tenant = tenant.with_user(user).sudo()

        def failed(message, log):
            tenant._log('consent', 'failed', log, user=user)
            return {'status': 'failed', 'tenant': tenant, 'message': message}

        if params.get('error') or str(params.get('admin_consent')).lower() != 'true':
            error = ("%s: %s" % (params.get('error') or _("not granted"),
                                 params.get('error_description') or '')).strip(': ')
            return failed(_("Consent was not granted: %s", error),
                          _("Consent not granted: %s", error))
        granted = (params.get('tenant') or '').strip()
        expected = (tenant.tenant_id or '').strip()
        if GUID_RE.match(expected) and granted.lower() != expected.lower():
            return failed(
                _("Consent was granted for another organisation. Sign in with a Global "
                  "Administrator of %s.", tenant.name),
                _("Consent granted in another directory (%s).", granted or '-'))
        error = tenant._activate_shared_app(attempts=CALLBACK_ATTEMPTS)
        if error:
            return {'status': 'failed', 'tenant': tenant, 'message': _(
                "Consent was granted, but Odoo cannot read the tenant yet: %s Microsoft can "
                "take a few minutes: then use Check Consent on the tenant in Odoo.", error)}
        tenant.action_test_connection()
        tenant._after_shared_app_connected()
        return {'status': 'success', 'tenant': tenant, 'message': _(
            "Consent granted: %s is now connected to Odoo through the shared app.",
            tenant.name)}

    def _activate_shared_app(self, attempts=1):
        """Switch to the shared app if it can read the tenant, else change nothing.

        :return: the error, or False once switched
        """
        self.ensure_one()
        rec = self.sudo()
        account = rec.partner_account_id or rec._default_partner_account()
        if not account:
            return _("No shared app.")
        vals = {'connection_mode': 'shared_app'}
        if not rec.partner_account_id:
            vals['partner_account_id'] = account.id
        error = False
        for attempt in range(attempts):
            if attempt:
                time.sleep(CALLBACK_WAIT)
            try:
                with self.env.cr.savepoint():  # rolled back if the app cannot read it
                    rec.write(vals)
                    rec._clear_token()  # a new consent needs a new token
                    rec._graph_request('GET', '/organization',
                                       params={'$select': 'id,displayName'}, paginate=True)
            except M365GraphError as exc:
                error = str(exc)
                continue
            error = False
            break
        user = rec.consent_requested_by or self.env.user
        if error:
            rec._log('consent', 'failed', _("The shared app cannot read the tenant: %s",
                                            error), user=user)
            return error
        rec.write({'consent_nonce': False, 'consent_expiry': False,
                   'consent_date': fields.Datetime.now()})
        rec._log('consent', 'success', _(
            "Admin consent granted: connected through the shared app of %s.", account.name),
            user=user)
        return False
