# -*- coding: utf-8 -*-
"""Sign-in of a partner user (App+User), for the Partner Center calls that Microsoft
accepts only with a user token (multi-factor authenticated):

=========================================  ===============================================
Use                                        Call
=========================================  ===============================================
Microsoft Customer Agreement acceptance    POST /v1/customers/{id}/agreements
Install the shared app in a customer       POST /v1/customers/{id}/applicationconsents
Application permissions in the customer    Graph (customer tenant, through GDAP)
                                           POST /servicePrincipals/{id}/appRoleAssignedTo
=========================================  ===============================================

A system administrator signs in once, as the dedicated partner service user (Admin Agent,
MFA), with the authorization code flow + PKCE of the shared app. Odoo keeps the refresh
token (system administrators only, never shown nor logged) and exchanges it for access
tokens: Partner Center, or Microsoft Graph in a customer tenant. A scheduled action uses
it every week so that it does not expire (90 days without use).

Reference: https://learn.microsoft.com/en-us/partner-center/developer/enable-secure-app-model
"""
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from datetime import timedelta, timezone

import requests
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import consteq, hash_sign, verify_hash_signed

from .m365_tenant import M365GraphError

_logger = logging.getLogger(__name__)

SYSTEM_GROUP = 'base.group_system'
SIGNIN_CALLBACK_PATH = '/m365/partner/signin/callback'
SIGNIN_STATE_SCOPE = 'm365_license_management.partner_signin'
SIGNIN_VALIDITY = timedelta(minutes=15)
# Asked at the sign-in; the refresh token then also gives Graph tokens in the customers
SIGNIN_SCOPE = ('https://api.partnercenter.microsoft.com/user_impersonation '
                'offline_access openid profile')
PC_USER_SCOPE = 'https://api.partnercenter.microsoft.com/.default offline_access'
GRAPH_USER_SCOPE = 'https://graph.microsoft.com/.default offline_access'
GRAPH_URL = 'https://graph.microsoft.com/v1.0'
# Reminder to sign in again before the refresh token can expire (90 days)
RENEW_REMINDER = timedelta(days=76)
GUID_RE = re.compile(r'^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$', re.I)
# Changing one of them: the partner user must sign in again
SIGNIN_FIELDS = {'graph_client_id', 'graph_client_secret', 'partner_tenant_id',
                 'authority_url'}


def jwt_claims(token):
    """Claims of a JWT, unverified: only for tokens received directly from the
    Microsoft identity platform over TLS."""
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (IndexError, ValueError, binascii.Error, AttributeError):
        return {}


class M365PartnerAccountSignin(models.Model):
    _inherit = 'm365.partner.account'

    pc_refresh_token = fields.Char(copy=False, groups=SYSTEM_GROUP)
    pc_user_token = fields.Char(copy=False, groups=SYSTEM_GROUP)
    pc_user_token_expiry = fields.Datetime(copy=False, groups=SYSTEM_GROUP)
    pc_signin_nonce = fields.Char(copy=False, groups=SYSTEM_GROUP)
    pc_signin_verifier = fields.Char(copy=False, groups=SYSTEM_GROUP)
    pc_signin_expiry = fields.Datetime(copy=False, groups=SYSTEM_GROUP)
    pc_signin_user_id = fields.Many2one('res.users', copy=False, groups=SYSTEM_GROUP)
    pc_user_state = fields.Selection([
        ('none', 'Not Signed In'),
        ('signed_in', 'Signed In'),
        ('expired', 'Sign In Again'),
    ], string="Partner User", default='none', required=True, readonly=True, copy=False,
        tracking=True,
        help="Signed-in partner user (App+User): needed to accept the Microsoft Customer "
             "Agreement and to install the shared app in the customer tenants.")
    pc_user_name = fields.Char(string="Signed In As", readonly=True, copy=False)
    pc_user_signin_date = fields.Datetime(string="Signed In On", readonly=True, copy=False)
    pc_user_error = fields.Char(string="Sign-in Error", readonly=True, copy=False)
    pc_signin_reminded = fields.Boolean(copy=False, readonly=True)
    signin_redirect_uri = fields.Char(
        string="Partner Sign-in Redirect URI", compute='_compute_signin_redirect_uri',
        help="Add this address to the redirect URIs (platform Web) of the shared app.")

    def _compute_signin_redirect_uri(self):
        uri = self.env['m365.tenant']._public_base_url() + SIGNIN_CALLBACK_PATH
        for rec in self:
            rec.signin_redirect_uri = uri

    def write(self, vals):
        res = super().write(vals)
        if SIGNIN_FIELDS & set(vals):
            self.filtered(lambda a: a.pc_user_state != 'none')._clear_partner_signin()
        return res

    def _clear_partner_signin(self):
        self.sudo().write({
            'pc_refresh_token': False, 'pc_user_token': False, 'pc_user_token_expiry': False,
            'pc_signin_nonce': False, 'pc_signin_verifier': False, 'pc_signin_expiry': False,
            'pc_user_state': 'none', 'pc_user_name': False, 'pc_user_signin_date': False,
            'pc_signin_reminded': False,
        })

    def _check_system(self):
        if not self.env.user.has_group(SYSTEM_GROUP):
            raise AccessError(_("Only system administrators can sign in the partner user."))

    def _partner_signed_in(self):
        """The App+User calls can be made."""
        self.ensure_one()
        return self.pc_user_state == 'signed_in'

    def _log_account(self, action, status, message):
        return self.env['m365.audit.log'].sudo().create({
            'action': action, 'status': status, 'message': "%s: %s" % (self.name, message),
            'user_id': self.env.user.id})

    # ------------------------------------------------------------------
    # Sign-in (authorization code + PKCE)
    # ------------------------------------------------------------------
    def _signin_state(self):
        self.ensure_one()
        rec = self.sudo()
        return hash_sign(rec.env, SIGNIN_STATE_SCOPE,
                         {'account': rec.id, 'nonce': rec.pc_signin_nonce},
                         expiration=rec.pc_signin_expiry.replace(tzinfo=timezone.utc))

    def action_partner_signin(self):
        """Open the Microsoft sign-in page for the partner service user."""
        self.ensure_one()
        self._check_system()
        rec = self.sudo()
        client_id, _secret = rec._shared_app_credentials()
        if not rec.partner_tenant_id:
            raise UserError(_("Enter the Partner Directory (Tenant) ID first."))
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        rec.write({
            'pc_signin_nonce': secrets.token_urlsafe(24),
            'pc_signin_verifier': verifier,
            'pc_signin_expiry': fields.Datetime.now() + SIGNIN_VALIDITY,
            'pc_signin_user_id': self.env.uid,
        })
        query = urllib.parse.urlencode({
            'client_id': client_id,
            'response_type': 'code',
            'redirect_uri': rec.signin_redirect_uri,
            'response_mode': 'query',
            'scope': SIGNIN_SCOPE,
            'state': rec._signin_state(),
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'prompt': 'login',
        })
        url = "%s/%s/oauth2/v2.0/authorize?%s" % (rec.authority_url.rstrip('/'),
                                                   rec.partner_tenant_id.strip(), query)
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'self'}

    def action_partner_signout(self):
        self._check_system()
        for rec in self:
            rec._clear_partner_signin()
            rec._log_account('partner_signin', 'info', _("Partner user signed out."))
        return self.env['m365.tenant']._notify(_("The partner user is signed out."))

    @api.model
    def _signin_account_from_state(self, state):
        Account = self.sudo().browse()
        try:
            values = verify_hash_signed(self.sudo().env, SIGNIN_STATE_SCOPE, state or '')
        except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
            return Account
        if not isinstance(values, dict) or not isinstance(values.get('account'), int):
            return Account
        account = Account.with_context(active_test=False).browse(values['account']).exists()
        if not (account and account.pc_signin_nonce and account.pc_signin_expiry
                and account.pc_signin_expiry > fields.Datetime.now()
                and consteq(str(values.get('nonce') or ''), account.pc_signin_nonce)):
            return Account
        return account

    @api.model
    def _partner_signin_callback(self, params):
        """Redirect of the Microsoft sign-in page. Runs as the logged-in Odoo user, who
        must be the system administrator who opened the sign-in.

        :return: ``{'status': 'success' | 'failed' | 'invalid', 'message', 'account'}``
        """
        account = self._signin_account_from_state(params.get('state'))
        if not account or account.pc_signin_user_id != self.env.user \
                or not self.env.user.has_group(SYSTEM_GROUP):
            return {'status': 'invalid', 'account': account.browse(), 'message': _(
                "This sign-in is not valid or has expired. Start it again from the Partner "
                "Center account in Odoo.")}
        verifier = account.pc_signin_verifier
        account.write({'pc_signin_nonce': False, 'pc_signin_verifier': False,
                       'pc_signin_expiry': False})

        def failed(message):
            account.write({'pc_user_error': message[:250]})
            account._log_account('partner_signin', 'failed', message)
            return {'status': 'failed', 'account': account, 'message': message}

        if params.get('error') or not params.get('code'):
            return failed(_("Sign-in refused: %s", ("%s %s" % (
                params.get('error') or '', params.get('error_description') or '')).strip()
                or _("no code")))
        client_id, secret = account._shared_app_credentials()
        try:
            resp = requests.post(account._signin_token_url(), data={
                'client_id': client_id, 'client_secret': secret,
                'grant_type': 'authorization_code', 'code': params['code'],
                'redirect_uri': account.signin_redirect_uri, 'code_verifier': verifier,
                'scope': SIGNIN_SCOPE}, timeout=30)
        except requests.RequestException as exc:
            return failed(_("Unable to reach the Microsoft identity platform: %s", exc))
        if resp.status_code != 200:
            return failed(_("Sign-in failed. %s", self.env['m365.tenant']._format_error(resp)))
        payload = resp.json()
        claims = jwt_claims(payload.get('access_token'))
        if 'mfa' not in (claims.get('amr') or []):
            return failed(_("The sign-in has no multi-factor authentication: Partner Center "
                            "requires it. Register MFA for the partner user and sign in "
                            "again."))
        expected = (account.partner_tenant_id or '').strip().lower()
        if GUID_RE.match(expected) and (claims.get('tid') or '').lower() != expected:
            return failed(_("The user does not belong to the partner tenant %s.", expected))
        if not payload.get('refresh_token'):
            return failed(_("Microsoft returned no refresh token (offline_access)."))
        user_name = (claims.get('upn') or claims.get('unique_name')
                     or jwt_claims(payload.get('id_token')).get('preferred_username') or '')
        now = fields.Datetime.now()
        account.write({
            'pc_refresh_token': payload['refresh_token'],
            'pc_user_token': payload['access_token'],
            'pc_user_token_expiry': now + timedelta(seconds=int(payload.get('expires_in', 3600))),
            'pc_user_state': 'signed_in', 'pc_user_name': user_name or False,
            'pc_user_signin_date': now, 'pc_user_error': False, 'pc_signin_reminded': False,
        })
        account.message_post(body=_("Partner user %s signed in (App+User).", user_name or '-'))
        account._log_account('partner_signin', 'success',
                             _("Partner user %s signed in.", user_name or '-'))
        return {'status': 'success', 'account': account, 'message': _(
            "Signed in as %s. Odoo can now accept the Microsoft Customer Agreement and "
            "install the shared app in the customer tenants.", user_name or '-')}

    # ------------------------------------------------------------------
    # Tokens of the partner user
    # ------------------------------------------------------------------
    def _signin_token_url(self, tenant=None):
        rec = self.sudo()
        return "%s/%s/oauth2/v2.0/token" % (rec.authority_url.rstrip('/'),
                                            (tenant or rec.partner_tenant_id).strip())

    def _user_token(self, scope, tenant=None, force=False):
        """Access token of the partner user for ``scope``, in the partner tenant or in a
        customer ``tenant`` (GDAP). The Partner Center token is cached."""
        self.ensure_one()
        rec = self.sudo()
        if not rec.pc_refresh_token or rec.pc_user_state == 'none':
            raise M365GraphError(_(
                "Sign in the partner user first (Partner Center account %s, Sign In as "
                "Partner User).", rec.name))
        cached = scope == PC_USER_SCOPE and not tenant
        now = fields.Datetime.now()
        if (cached and not force and rec.pc_user_token and rec.pc_user_token_expiry
                and rec.pc_user_token_expiry > now + timedelta(minutes=5)):
            return rec.pc_user_token
        client_id, secret = rec._shared_app_credentials()
        try:
            resp = requests.post(rec._signin_token_url(tenant), data={
                'client_id': client_id, 'client_secret': secret,
                'grant_type': 'refresh_token', 'refresh_token': rec.pc_refresh_token,
                'scope': scope}, timeout=30)
        except requests.RequestException as exc:
            raise M365GraphError(_("Unable to reach the Microsoft identity platform: %s",
                                   exc)) from None
        if resp.status_code != 200:
            error = self.env['m365.tenant']._format_error(resp)
            try:
                code = resp.json().get('error')
            except ValueError:
                code = None
            if code == 'invalid_grant' and not tenant:
                # Revoked or expired: a new sign-in is needed
                rec.write({'pc_user_state': 'expired', 'pc_user_error': error[:250],
                           'pc_user_token': False, 'pc_user_token_expiry': False})
            raise M365GraphError(_("Partner user token refused. %s", error))
        payload = resp.json()
        vals = {'pc_user_error': False}
        if payload.get('refresh_token'):
            vals['pc_refresh_token'] = payload['refresh_token']  # rotated
        if cached:
            vals.update(pc_user_token=payload['access_token'], pc_user_token_expiry=now
                        + timedelta(seconds=int(payload.get('expires_in', 3600))))
        rec.write(vals)
        return payload['access_token']

    def _pc_user_request(self, method, path, json_body=None, expect=()):
        """Partner Center call as the signed-in partner user (App+User)."""
        self.ensure_one()
        return self._pc_send(lambda force=False: self._user_token(PC_USER_SCOPE, force=force),
                             method, path, json_body=json_body, expect=expect)

    def _customer_graph_request(self, tenant, method, path, params=None, json_body=None,
                                paginate=False):
        """Microsoft Graph in a customer tenant as the partner user (through GDAP)."""
        self.ensure_one()
        url = path if path.startswith('http') else GRAPH_URL + path
        token = self._user_token(GRAPH_USER_SCOPE, tenant=tenant)
        results, retries, refreshed = [], 0, False
        while url:
            try:
                resp = requests.request(method, url, params=params, json=json_body, timeout=60,
                                        headers={'Authorization': 'Bearer %s' % token,
                                                 'Content-Type': 'application/json'})
            except requests.RequestException as exc:
                raise M365GraphError(_("Microsoft Graph request failed: %s", exc)) from None
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                token = self._user_token(GRAPH_USER_SCOPE, tenant=tenant)
                continue
            if resp.status_code in (429, 503, 504) and retries < 4:
                retries += 1
                try:
                    wait = int(resp.headers.get('Retry-After', 2))
                except ValueError:
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                continue
            if resp.status_code >= 400:
                raise M365GraphError(self.env['m365.tenant']._format_error(resp))
            data = resp.json() if resp.content else {}
            if not paginate:
                return data
            results.extend(data.get('value', []))
            url, params = data.get('@odata.nextLink'), None
        return results

    # ------------------------------------------------------------------
    # Scheduled action
    # ------------------------------------------------------------------
    @api.model
    def _cron_partner_signin(self):
        """Weekly: use the refresh token so that it does not expire; remind the
        administrators to sign in again before 90 days."""
        accounts = self.sudo().search([('pc_user_state', '=', 'signed_in')])
        partners = self.env['m365.tenant']._admin_partners()
        now = fields.Datetime.now()
        for account in accounts:
            try:
                account._user_token(PC_USER_SCOPE, force=True)
            except M365GraphError as exc:
                _logger.warning("Partner user token of %s not renewed: %s", account.name, exc)
                if account.pc_user_state == 'expired':
                    account.message_post(
                        body=Markup("<p>%s</p>") % escape(_(
                            "The partner user sign-in has expired: sign in again (Sign In as "
                            "Partner User). %s", exc)),
                        partner_ids=partners.ids, subtype_xmlid='mail.mt_comment')
                continue
            if (not account.pc_signin_reminded and account.pc_user_signin_date
                    and account.pc_user_signin_date + RENEW_REMINDER < now):
                account.message_post(
                    body=Markup("<p>%s</p>") % escape(_(
                        "The partner user signed in on %s. Sign in again (Sign In as Partner "
                        "User) within two weeks so that the automatic provisioning keeps "
                        "working.", fields.Datetime.to_string(account.pc_user_signin_date))),
                    partner_ids=partners.ids, subtype_xmlid='mail.mt_comment')
                account.pc_signin_reminded = True
