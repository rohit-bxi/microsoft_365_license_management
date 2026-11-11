# -*- coding: utf-8 -*-
"""MFA operations of a tenant (Microsoft Graph).

=========================================  ===============================================
Operation                                  Graph call
=========================================  ===============================================
MFA methods of a user                      GET    /users/{id}/authentication/methods
Reset MFA (user registers again)           DELETE /users/{id}/authentication/{type}Methods/{id}
Per-user MFA state                         GET / PATCH beta /users/{id}/authentication/requirements
Default MFA method (before the reset)      GET / PATCH beta /users/{id}/authentication/signInPreferences
Temporary Access Pass                      POST   /users/{id}/authentication/temporaryAccessPassMethods
Temporary Access Pass policy               GET / PATCH /policies/authenticationMethodsPolicy/
                                                       authenticationMethodConfigurations/TemporaryAccessPass
Security Defaults (tenant)                 PATCH  /policies/identitySecurityDefaultsEnforcementPolicy
=========================================  ===============================================

Microsoft does not delete the phone that is the default MFA method of the user: Reset MFA
first makes another method of the user (Microsoft Authenticator or a software token) the
default, and deletes the phones first.

Every operation that removes MFA needs **Enable Security Operations** on the tenant.
The user operations go through the approval of the requests (``m365.license.request``,
types *Reset MFA* and *Turn Off Per-user MFA*), never approved automatically.

Reference: https://learn.microsoft.com/en-us/graph/api/resources/authenticationmethods-overview
"""
import re
import urllib.parse

from markupsafe import Markup, escape

from odoo import _, fields, models
from odoo.exceptions import UserError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
PASSWORD_METHOD = '#microsoft.graph.passwordAuthenticationMethod'
# Graph type of a method -> (URL segment to delete it, label)
AUTH_METHODS = {
    '#microsoft.graph.microsoftAuthenticatorAuthenticationMethod':
        ('microsoftAuthenticatorMethods', 'Microsoft Authenticator'),
    '#microsoft.graph.phoneAuthenticationMethod': ('phoneMethods', 'Phone'),
    '#microsoft.graph.softwareOathAuthenticationMethod':
        ('softwareOathMethods', 'Software OATH token'),
    '#microsoft.graph.fido2AuthenticationMethod': ('fido2Methods', 'FIDO2 security key'),
    '#microsoft.graph.emailAuthenticationMethod': ('emailMethods', 'Email'),
    '#microsoft.graph.windowsHelloForBusinessAuthenticationMethod':
        ('windowsHelloForBusinessMethods', 'Windows Hello for Business'),
    '#microsoft.graph.temporaryAccessPassAuthenticationMethod':
        ('temporaryAccessPassMethods', 'Temporary Access Pass'),
    '#microsoft.graph.platformCredentialAuthenticationMethod':
        ('platformCredentialMethods', 'Platform credential'),
    # Reference to the external MFA provider: deleted through /$ref
    '#microsoft.graph.externalAuthenticationMethod':
        ('externalAuthenticationMethods', 'External MFA'),
    # One per user, beta only: DELETE beta /users/{id}/authentication/qrCodePinMethod
    '#microsoft.graph.qrCodePinAuthenticationMethod': ('qrCodePinMethod', 'QR code and PIN'),
}
PHONE_METHOD = '#microsoft.graph.phoneAuthenticationMethod'
# Default MFA method (signInPreferences) of the phones, and of the methods that can
# replace a phone as default
PHONE_PREFERENCES = ('sms', 'voiceMobile', 'voiceAlternateMobile', 'voiceOffice')
REPLACEMENT_PREFERENCES = (
    ('#microsoft.graph.microsoftAuthenticatorAuthenticationMethod', 'push'),
    ('#microsoft.graph.softwareOathAuthenticationMethod', 'oath'),
)
# A mobile number cannot be removed while an alternate mobile exists
PHONE_ORDER = {'alternateMobile': 0, 'office': 1, 'mobile': 2}
TAP_POLICY_PATH = ('/policies/authenticationMethodsPolicy/authenticationMethodConfigurations/'
                   'TemporaryAccessPass')
PER_USER_MFA_STATES = ('disabled', 'enabled', 'enforced')


def mask_phone(number):
    digits = re.sub(r'\D', '', number or '')
    return ("*" * max(len(digits) - 3, 0) + digits[-3:]) if digits else ''


def mask_email(address):
    local, _sep, domain = (address or '').partition('@')
    return ("%s***@%s" % (local[:1], domain)) if domain else ''


class M365TenantSecurity(models.Model):
    _inherit = 'm365.tenant'

    allow_security_operations = fields.Boolean(
        string="Enable Security Operations", default=False, tracking=True,
        help="Allow the operations that remove MFA: reset the MFA methods of a user, turn "
             "off per-user MFA, turn off Security Defaults, issue Temporary Access Passes. "
             "Requires the UserAuthenticationMethod.ReadWrite.All, "
             "Policy.ReadWrite.AuthenticationMethod and Policy.ReadWrite.ConditionalAccess "
             "permissions.")

    def _check_security_operations_allowed(self):
        self.ensure_one()
        if not self.allow_security_operations:
            raise M365GraphError(
                _("Security operations are disabled for tenant %s.", self.name))

    def _graph_beta_url(self, path):
        """Absolute URL of the beta version of Graph (per-user MFA is beta only)."""
        base = (self.graph_url or '').rstrip('/')
        if base.endswith('/v1.0'):
            base = base[:-len('/v1.0')] + '/beta'
        elif not base.endswith('/beta'):
            base += '/beta'
        return "%s/%s" % (base, path.lstrip('/'))

    # ------------------------------------------------------------------
    # Authentication methods
    # ------------------------------------------------------------------
    @staticmethod
    def _auth_method_info(item):
        odata = item.get('@odata.type') or ''
        segment, label = AUTH_METHODS.get(odata, (None, odata.rsplit('.', 1)[-1] or 'Unknown'))
        if odata == PASSWORD_METHOD:
            label = 'Password'
        if 'phoneNumber' in item:
            detail = "%s %s" % (item.get('phoneType') or '', mask_phone(item['phoneNumber']))
        elif 'emailAddress' in item:
            detail = mask_email(item['emailAddress'])
        else:
            detail = item.get('displayName') or item.get('model') or item.get('deviceTag') or ''
        return {
            'id': item.get('id'), 'odata_type': odata, 'segment': segment, 'type': label,
            'detail': detail.strip(), 'created': item.get('createdDateTime'),
            'is_password': odata == PASSWORD_METHOD, 'phone_type': item.get('phoneType'),
        }

    def _auth_method_delete_url(self, user_key, method):
        """Path (or beta URL) of the DELETE of a method."""
        segment = method['segment']
        if segment == 'qrCodePinMethod':
            return self._graph_beta_url('/users/%s/authentication/qrCodePinMethod' % user_key)
        path = '/users/%s/authentication/%s/%s' % (
            user_key, segment, urllib.parse.quote(method['id'] or '', safe=''))
        if segment == 'externalAuthenticationMethods':
            path += '/$ref'
        return path

    def graph_get_sign_in_preferences(self, graph_id):
        """Default MFA method of the user (Graph beta)."""
        self.ensure_one()
        return self._graph_request('GET', self._graph_beta_url(
            '/users/%s/authentication/signInPreferences' % self._graph_key(graph_id)))

    def _move_default_off_phone(self, graph_id, methods):
        """When a phone is the default MFA method, make another method of the user the
        default, so that the phone can be deleted. :return: the new default or False"""
        try:
            prefs = self.graph_get_sign_in_preferences(graph_id)
        except M365GraphError:
            return False  # beta / permission: try the deletion anyway
        if prefs.get('userPreferredMethodForSecondaryAuthentication') not in PHONE_PREFERENCES:
            return False
        types = {m['odata_type'] for m in methods}
        for odata, preference in REPLACEMENT_PREFERENCES:
            if odata in types:
                try:
                    self._graph_request('PATCH', self._graph_beta_url(
                        '/users/%s/authentication/signInPreferences'
                        % self._graph_key(graph_id)),
                        json_body={'userPreferredMethodForSecondaryAuthentication': preference})
                except M365GraphError:
                    return False
                return preference
        return False

    def graph_get_auth_methods(self, graph_id):
        """MFA (and password) methods of a user; phone numbers and emails masked."""
        self.ensure_one()
        items = self._graph_request(
            'GET', '/users/%s/authentication/methods' % self._graph_key(graph_id),
            paginate=True)
        return [self._auth_method_info(item) for item in items]

    def graph_reset_mfa(self, graph_id):
        """Delete every method of the user except the password: the user registers MFA
        again at the next sign-in.

        Microsoft may refuse to delete the default method while others exist, so the
        methods that failed are tried once more after the others are deleted.

        :return: (removed methods, [(method, error)] left)
        """
        self.ensure_one()
        self._check_security_operations_allowed()
        user_key = self._graph_key(graph_id)
        methods = [m for m in self.graph_get_auth_methods(graph_id) if not m['is_password']]
        unsupported = [(m, _("Odoo cannot remove this type of method."))
                       for m in methods if not m['segment']]
        pending = [m for m in methods if m['segment']]
        if any(m['odata_type'] == PHONE_METHOD for m in pending):
            self._move_default_off_phone(graph_id, pending)
        # Phones first (alternate before mobile), while another method can be the default
        pending.sort(key=lambda m: (m['odata_type'] != PHONE_METHOD,
                                    PHONE_ORDER.get(m.get('phone_type'), 1)))
        removed, failed = [], []
        for _attempt in range(2):
            failed = []
            for method in pending:
                try:
                    self._graph_request('DELETE', self._auth_method_delete_url(user_key, method))
                except M365GraphError as exc:
                    error = str(exc)
                    if method['odata_type'] == PHONE_METHOD:
                        error += " " + _("Microsoft does not remove the phone that is the "
                                         "default MFA method: the user changes the default "
                                         "method, or replace the number in the Entra admin "
                                         "center.")
                    failed.append((method, error))
                    continue
                removed.append(method)
            if not failed or len(failed) == len(pending):  # done, or nothing more to gain
                break
            pending = [method for method, _err in failed]
        return removed, unsupported + failed

    def graph_get_per_user_mfa(self, graph_id):
        self.ensure_one()
        data = self._graph_request('GET', self._graph_beta_url(
            '/users/%s/authentication/requirements' % self._graph_key(graph_id)))
        return data.get('perUserMfaState')

    def graph_set_per_user_mfa(self, graph_id, state):
        """Per-user MFA state: disabled, enabled or enforced (Graph beta)."""
        self.ensure_one()
        if state not in PER_USER_MFA_STATES:
            raise UserError(_("Unknown per-user MFA state %s.", state))
        if state == 'disabled':
            self._check_security_operations_allowed()
        self._graph_request('PATCH', self._graph_beta_url(
            '/users/%s/authentication/requirements' % self._graph_key(graph_id)),
            json_body={'perUserMfaState': state})

    def graph_get_tap_policy(self):
        """Temporary Access Pass policy of the tenant: state, lifetimes, one-time use."""
        self.ensure_one()
        return self._graph_request('GET', TAP_POLICY_PATH)

    def graph_enable_tap_policy(self):
        """Enable the Temporary Access Pass method (Policy.ReadWrite.AuthenticationMethod).
        The users it applies to stay as configured in Microsoft Entra."""
        self.ensure_one()
        self._check_security_operations_allowed()
        self._graph_request('PATCH', TAP_POLICY_PATH, json_body={
            '@odata.type': '#microsoft.graph.temporaryAccessPassAuthenticationMethodConfiguration',
            'state': 'enabled'})

    def graph_create_temporary_access_pass(self, graph_id, lifetime=60, usable_once=True):
        """Temporary Access Pass: lets the user sign in once to register MFA again.

        :return: Graph item (``temporaryAccessPass`` is the code, shown once)
        """
        self.ensure_one()
        self._check_security_operations_allowed()
        return self._graph_request(
            'POST', '/users/%s/authentication/temporaryAccessPassMethods'
            % self._graph_key(graph_id),
            json_body={'lifetimeInMinutes': int(lifetime), 'isUsableOnce': bool(usable_once)})

    # ------------------------------------------------------------------
    # Security Defaults (tenant)
    # ------------------------------------------------------------------
    def _set_security_defaults(self, enabled, reason):
        """Turn Security Defaults on or off, with the reason in the chatter and the log.

        :return: the error (kept in the audit log), or False
        """
        self.ensure_one()
        self._check_admin()
        if not (reason or '').strip():
            raise UserError(_("Give the reason of the change."))
        try:
            self.graph_set_security_defaults(enabled)
        except M365GraphError as exc:
            self._log('security_defaults', 'failed', _(
                "Security Defaults not changed: %s", exc))
            return str(exc)
        message = (_("Security Defaults turned on. Reason: %s", reason) if enabled
                   else _("Security Defaults turned off: MFA is no longer required in the "
                          "tenant. Reason: %s", reason))
        self._log('security_defaults', 'success', message)
        self.message_post(body=Markup("<p>%s</p>") % escape(message))
        return False

    def action_security_defaults_wizard(self):
        self._check_admin()
        self.ensure_one()
        enable = self.env.context.get('m365_enable', False)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Turn On Security Defaults") if enable
            else _("Turn Off Security Defaults"),
            'res_model': 'm365.security.defaults.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_tenant_id': self.id, 'default_enable': enable},
        }


class M365SecurityDefaultsWizard(models.TransientModel):
    _name = 'm365.security.defaults.wizard'
    _description = 'Turn Security Defaults On or Off'

    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True, readonly=True)
    enable = fields.Boolean(string="Turn On", readonly=True)
    reason = fields.Text(string="Reason", required=True)

    def action_apply(self):
        self.ensure_one()
        error = self.tenant_id._set_security_defaults(self.enable, self.reason)
        if error:  # a notification, not an error: the audit log line is kept
            return self.tenant_id._notify(_("Security Defaults not changed: %s", error),
                                          'danger')
        return self.tenant_id._notify(
            _("Security Defaults turned on.") if self.enable
            else _("Security Defaults turned off."))
