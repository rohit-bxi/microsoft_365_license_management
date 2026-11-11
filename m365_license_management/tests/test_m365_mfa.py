# -*- coding: utf-8 -*-
import base64
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from ..models.m365_tenant_config import SECURITY_DEFAULTS_PATH
from .common import (ADMIN_GROUP, GRAPH_PATH, USER_GROUP, USER_ID, FakeGraph, M365Common,
                     map_tenants)

METHODS_PATH = '/users/%s/authentication/methods' % USER_ID
BETA_REQUIREMENTS = ('https://graph.microsoft.com/beta/users/%s/authentication/requirements'
                     % USER_ID)
PASSWORD = {'@odata.type': '#microsoft.graph.passwordAuthenticationMethod', 'id': 'pwd-1'}
PHONE = {'@odata.type': '#microsoft.graph.phoneAuthenticationMethod', 'id': 'phone-1',
         'phoneNumber': '+91 9876543210', 'phoneType': 'mobile'}
AUTHENTICATOR = {'@odata.type': '#microsoft.graph.microsoftAuthenticatorAuthenticationMethod',
                 'id': 'app-1', 'displayName': 'Pixel 8'}
EMAIL = {'@odata.type': '#microsoft.graph.emailAuthenticationMethod', 'id': 'email-1',
         'emailAddress': 'adele@example.com'}
QR_CODE = {'@odata.type': '#microsoft.graph.qrCodePinAuthenticationMethod', 'id': 'qr-1'}
HARDWARE = {'@odata.type': '#microsoft.graph.hardwareOathAuthenticationMethod', 'id': 'hw-1'}
EXTERNAL = {'@odata.type': '#microsoft.graph.externalAuthenticationMethod', 'id': 'ext-1',
            'displayName': 'Duo'}
ALT_PHONE = {'@odata.type': '#microsoft.graph.phoneAuthenticationMethod',
             'id': 'b6332ec1-7057-4abe-9331-3d72feddfe41', 'phoneNumber': '+91 9000000001',
             'phoneType': 'alternateMobile'}
BETA = 'https://graph.microsoft.com/beta/users/%s/authentication/' % USER_ID
TAP_POLICY = ('/policies/authenticationMethodsPolicy/authenticationMethodConfigurations/'
              'TemporaryAccessPass')


def delete_path(segment, method_id):
    return '/users/%s/authentication/%s/%s' % (USER_ID, segment, method_id)


@tagged('post_install', '-at_install')
class TestM365Mfa(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_mfa_admin', groups=ADMIN_GROUP)
        cls.user = map_tenants(new_test_user(cls.env, 'm365_mfa_user', groups=USER_GROUP),
                               cls.tenant)
        cls.tenant.write({'allow_security_operations': True, 'auto_approve_admins': True})

    def _request(self, request_type='mfa_reset', user=None, **vals):
        return self.env['m365.license.request'].with_user(user or self.user).create(dict({
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'request_type': request_type,
            'reason': 'Lost phone, ticket 4711'}, **vals))

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    def test_request_rules(self):
        Request = self.env['m365.license.request']
        with self.assertRaisesRegex(ValidationError, 'Select the license'):
            Request.create({'tenant_id': self.tenant.id, 'user_id': self.adele.id,
                            'request_type': 'assign'})
        with self.assertRaisesRegex(ValidationError, 'An MFA request has no license'):
            self._request(sku_id=self.sku.id)
        with self.assertRaisesRegex(UserError, 'business justification'):
            self._request(reason=False).action_submit()
        self.tenant.allow_security_operations = False
        with self.assertRaisesRegex(UserError, 'Security operations are disabled'):
            self._request().action_submit()
        self.tenant.allow_security_operations = True
        # An administrator's MFA request still waits for approval
        request = self._request(user=self.admin)
        with patch(GRAPH_PATH, side_effect=FakeGraph()) as graph:
            request.action_submit()
        graph.assert_not_called()
        self.assertEqual(request.state, 'submitted')
        self.assertIn('Reset MFA - Adele Vance', str(request.sudo().activity_ids[:1].note))
        with self.assertRaisesRegex(UserError, 'already pending'):
            self._request().action_submit()
        with self.assertRaises(AccessError):
            request.with_user(self.user).action_approve()

    def test_reset_mfa(self):
        attempts = {'phone': 0}

        def delete_phone(_body):
            attempts['phone'] += 1
            if attempts['phone'] == 1:  # the default method, while others exist
                raise M365GraphError("HTTP 400 - badRequest: The default method cannot be "
                                     "deleted.")
            return {}

        graph = FakeGraph({
            ('GET', METHODS_PATH): [PASSWORD, PHONE, AUTHENTICATOR, EMAIL],
            ('DELETE', delete_path('phoneMethods', 'phone-1')): delete_phone,
        })
        request = self._request()
        request.action_submit()
        with patch(GRAPH_PATH, side_effect=graph):
            request.with_user(self.admin).action_approve()
        self.assertEqual(request.state, 'done', request.error_message)
        self.assertEqual([p for m, p, _b in graph.calls if m == 'DELETE'], [
            delete_path('phoneMethods', 'phone-1'),
            delete_path('microsoftAuthenticatorMethods', 'app-1'),
            delete_path('emailMethods', 'email-1'),
            delete_path('phoneMethods', 'phone-1'),
        ])
        log = self._logs('mfa_reset')
        self.assertEqual(log.status, 'success')
        self.assertIn('Phone mobile *********210', log.message)
        self.assertIn('Email a***@example.com', log.message)
        self.assertNotIn('9876543', log.message)
        self.assertEqual(log.request_id, request)

    def test_reset_mfa_partial(self):
        graph = FakeGraph({('GET', METHODS_PATH): [PASSWORD, AUTHENTICATOR, HARDWARE]})
        request = self._request()
        request.action_submit()
        with patch(GRAPH_PATH, side_effect=graph):
            request.with_user(self.admin).action_approve()
        self.assertEqual(request.state, 'failed')
        self.assertIn('Removed: Microsoft Authenticator Pixel 8', request.error_message)
        self.assertIn('hardwareOathAuthenticationMethod', request.error_message)
        self.assertEqual(self._logs('mfa_reset').status, 'failed')
        # Turned off since the approval: nothing is done
        self.tenant.allow_security_operations = False
        with patch(GRAPH_PATH, side_effect=graph) as mock:
            request.with_user(self.admin).action_retry()
        self.assertIn('Security operations are disabled', request.error_message)
        mock.assert_not_called()

    def test_turn_off_per_user_mfa(self):
        graph = FakeGraph({('PATCH', BETA_REQUIREMENTS): {}})
        request = self._request('mfa_disable')
        request.action_submit()
        with patch(GRAPH_PATH, side_effect=graph):
            request.with_user(self.admin).action_approve()
        self.assertEqual(request.state, 'done', request.error_message)
        self.assertEqual(graph.bodies('PATCH', BETA_REQUIREMENTS),
                         [{'perUserMfaState': 'disabled'}])
        self.assertEqual(self._logs('mfa_disable').status, 'success')
        self.tenant.graph_url = 'https://graph.microsoft.us/v1.0/'
        self.assertEqual(self.tenant._graph_beta_url('/users/x'),
                         'https://graph.microsoft.us/beta/users/x')

    # ------------------------------------------------------------------
    # Security Defaults, MFA view, Temporary Access Pass
    # ------------------------------------------------------------------
    def _defaults_wizard(self, enable, user=None):
        action = self.tenant.with_user(user or self.admin).with_context(
            m365_enable=enable).action_security_defaults_wizard()
        return self.env[action['res_model']].with_user(user or self.admin).with_context(
            action['context']).create({'reason': 'Customer request, ticket 4711'})

    def test_security_defaults(self):
        graph = FakeGraph({('PATCH', SECURITY_DEFAULTS_PATH): {}})
        with self.assertRaises(AccessError):
            self.tenant.with_user(self.user).action_security_defaults_wizard()
        self.tenant.allow_security_operations = False
        wizard = self._defaults_wizard(False)
        with patch(GRAPH_PATH, side_effect=graph):
            action = wizard.action_apply()
        # A notification: the audit log line of the refusal is kept
        self.assertEqual(action['params']['type'], 'danger')
        self.assertIn('Security operations are disabled', action['params']['message'])
        self.tenant.allow_security_operations = True
        with patch(GRAPH_PATH, side_effect=graph):
            self._defaults_wizard(False).action_apply()
        self.assertEqual(self.tenant.security_defaults_state, 'off')
        self.assertTrue(any('ticket 4711' in (m.body or '') for m in self.tenant.message_ids))
        # Turning them on needs no security operations
        self.tenant.allow_security_operations = False
        with patch(GRAPH_PATH, side_effect=graph):
            self._defaults_wizard(True).action_apply()
        self.assertEqual(graph.bodies('PATCH', SECURITY_DEFAULTS_PATH),
                         [{'isEnabled': False}, {'isEnabled': True}])
        self.assertEqual(self.tenant.security_defaults_state, 'on')
        self.assertEqual(self._logs('security_defaults').mapped('status'),
                         ['success', 'success', 'failed'])

    def test_mfa_view_and_temporary_access_pass(self):
        with self.assertRaises(AccessError):
            self.adele.with_user(self.user).action_view_mfa()
        graph = FakeGraph({
            ('GET', METHODS_PATH): [PASSWORD, PHONE, QR_CODE],
            ('GET', BETA_REQUIREMENTS): {'perUserMfaState': 'enforced'},
            ('POST', '/users/%s/authentication/temporaryAccessPassMethods' % USER_ID):
                {'temporaryAccessPass': 'TAP-CODE-123', 'lifetimeInMinutes': 60,
                 'startDateTime': '2026-11-06T10:00:00Z'},
        })
        with patch(GRAPH_PATH, side_effect=graph):
            action = self.adele.with_user(self.admin).action_view_mfa()
            wizard = self.env['m365.user.mfa.wizard'].with_user(self.admin).browse(
                action['res_id'])
            self.assertEqual(wizard.per_user_mfa_state, 'enforced')
            self.assertEqual([(m.method_type, m.detail, m.removable) for m in wizard.method_ids], [
                ('Password', False, False),
                ('Phone', 'mobile *********210', True),
                ('QR code and PIN', False, True),
            ])
            wizard.tap_lifetime = 5
            with self.assertRaisesRegex(M365GraphError, '10 minutes to 30 days'):
                wizard.action_issue_tap()
            wizard.tap_lifetime = 60
            wizard.action_issue_tap()
        self.assertEqual(wizard.tap_code, 'TAP-CODE-123')
        self.assertEqual(graph.bodies('POST', '/users/%s/authentication/temporaryAccessPassMethods'
                                      % USER_ID), [{'lifetimeInMinutes': 60, 'isUsableOnce': True}])
        self.assertNotIn('TAP-CODE', self._logs('tap').message)
        wizard.action_done()
        self.assertFalse(wizard.tap_code)
        self.tenant.allow_security_operations = False
        with patch(GRAPH_PATH, side_effect=graph), \
                self.assertRaisesRegex(M365GraphError, 'Security operations are disabled'):
            self.tenant.graph_create_temporary_access_pass(USER_ID)

    def test_mfa_view_errors(self):
        denied = M365GraphError('HTTP 403 - accessDenied: Request Authorization failed.')
        with patch(GRAPH_PATH, side_effect=FakeGraph({METHODS_PATH: denied,
                                                      BETA_REQUIREMENTS: denied})):
            action = self.adele.with_user(self.admin).action_view_mfa()
        wizard = self.env['m365.user.mfa.wizard'].browse(action['res_id'])
        self.assertIn('Methods: HTTP 403', wizard.error)
        self.assertIn('Per-user MFA: HTTP 403', wizard.error)

    # ------------------------------------------------------------------
    # Bulk MFA requests
    # ------------------------------------------------------------------
    def _bulk(self, rows, request_type='mfa_reset'):
        text = "\n".join(",".join(row) for row in [('Tenant', 'User', 'License',
                                                     'Business Justification')] + rows)
        return self.env['m365.license.bulk.wizard'].with_user(self.admin).create({
            'request_type': request_type, 'file': base64.b64encode(text.encode()),
            'filename': 'mfa.csv'})

    def test_bulk_mfa_requests(self):
        wizard = self._bulk([('tid-contoso', 'adele.vance@contoso.com', '', ''),
                             ('tid-contoso', 'megan.bowen@contoso.com', 'SPE_E3', 'Lost phone')])
        wizard.action_verify()
        self.assertEqual(wizard.line_ids.mapped('status'), ['error', 'error'])
        self.assertIn('Business Justification is required', wizard.line_ids[0].message)
        self.assertIn('has no license', wizard.line_ids[1].message)

        wizard = self._bulk([('tid-contoso', 'adele.vance@contoso.com', '', 'Lost phone'),
                             ('Contoso', 'megan.bowen@contoso.com', '', 'New phone')])
        with patch(GRAPH_PATH, side_effect=FakeGraph()) as graph:
            wizard.action_verify()
            wizard.action_execute()
        graph.assert_not_called()  # every request waits for approval
        self.assertEqual(wizard.line_ids.mapped('status'), ['submitted', 'submitted'])
        self.assertEqual(wizard.line_ids.request_id.mapped('request_type'),
                         ['mfa_reset', 'mfa_reset'])

        self.tenant.allow_security_operations = False
        wizard = self._bulk([('tid-contoso', 'adele.vance@contoso.com', '', 'Again')])
        wizard.action_verify()
        self.assertIn('Security operations are disabled', wizard.line_ids.message)


@tagged('post_install', '-at_install')
class TestM365MfaGaps(M365Common):
    """Default phone method, method types of Graph v1.0 / beta, Temporary Access Pass
    policy."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_mfa_gap_admin', groups=ADMIN_GROUP)
        cls.tenant.allow_security_operations = True

    def test_reset_moves_default_off_phone(self):
        graph = FakeGraph({
            ('GET', METHODS_PATH): [PASSWORD, PHONE, ALT_PHONE, AUTHENTICATOR, QR_CODE,
                                    EXTERNAL],
            ('GET', BETA + 'signInPreferences'): {
                'isSystemPreferredAuthenticationMethodEnabled': False,
                'userPreferredMethodForSecondaryAuthentication': 'sms'},
        })
        with patch(GRAPH_PATH, side_effect=graph):
            removed, left = self.tenant.graph_reset_mfa(USER_ID)
        self.assertFalse(left)
        self.assertEqual(graph.bodies('PATCH', BETA + 'signInPreferences'),
                         [{'userPreferredMethodForSecondaryAuthentication': 'push'}])
        deletes = [p for m, p, _b in graph.calls if m == 'DELETE']
        self.assertEqual(deletes, [
            # Phones first, the alternate mobile before the mobile
            delete_path('phoneMethods', 'b6332ec1-7057-4abe-9331-3d72feddfe41'),
            delete_path('phoneMethods', 'phone-1'),
            delete_path('microsoftAuthenticatorMethods', 'app-1'),
            BETA + 'qrCodePinMethod',
            delete_path('externalAuthenticationMethods', 'ext-1') + '/$ref',
        ])
        self.assertEqual(len(removed), 5)

    def test_default_phone_alone(self):
        refused = M365GraphError("HTTP 400 - badRequest: The default method cannot be deleted.")
        graph = FakeGraph({
            ('GET', METHODS_PATH): [PASSWORD, PHONE],
            ('GET', BETA + 'signInPreferences'): {
                'userPreferredMethodForSecondaryAuthentication': 'voiceMobile'},
            ('DELETE', delete_path('phoneMethods', 'phone-1')): refused,
        })
        with patch(GRAPH_PATH, side_effect=graph):
            removed, left = self.tenant.graph_reset_mfa(USER_ID)
        self.assertFalse(removed)
        self.assertFalse(graph.bodies('PATCH'))  # no other method can be the default
        self.assertIn('the user changes the default method', left[0][1])

    def _mfa_wizard(self, graph):
        with patch(GRAPH_PATH, side_effect=graph):
            action = self.adele.with_user(self.admin).action_view_mfa()
        return self.env['m365.user.mfa.wizard'].with_user(self.admin).browse(action['res_id'])

    def test_tap_policy(self):
        tap_path = '/users/%s/authentication/temporaryAccessPassMethods' % USER_ID
        graph = FakeGraph({
            ('GET', METHODS_PATH): [PASSWORD],
            ('GET', TAP_POLICY): {'state': 'disabled', 'isUsableOnce': True,
                                  'defaultLifetimeInMinutes': 120,
                                  'minimumLifetimeInMinutes': 60,
                                  'maximumLifetimeInMinutes': 480},
            ('POST', tap_path): {'temporaryAccessPass': 'TAP-1', 'lifetimeInMinutes': 120},
        })
        wizard = self._mfa_wizard(graph)
        self.assertEqual((wizard.tap_policy_state, wizard.tap_lifetime, wizard.tap_usable_once),
                         ('disabled', 120, True))
        with patch(GRAPH_PATH, side_effect=graph), \
                self.assertRaisesRegex(M365GraphError, 'disabled in the tenant'):
            wizard.action_issue_tap()
        graph.routes[('GET', TAP_POLICY)] = dict(graph.routes[('GET', TAP_POLICY)],
                                                 state='enabled')
        with patch(GRAPH_PATH, side_effect=graph):
            wizard.action_enable_tap()
        self.assertEqual(graph.bodies('PATCH', TAP_POLICY), [{
            '@odata.type': '#microsoft.graph.temporaryAccessPassAuthenticationMethodConfiguration',
            'state': 'enabled'}])
        self.assertEqual(wizard.tap_policy_state, 'enabled')
        for vals, error in (({'tap_lifetime': 30}, '60 to 480 minutes'),
                            ({'tap_lifetime': 120, 'tap_usable_once': False}, 'one-time')):
            wizard.write(vals)
            with patch(GRAPH_PATH, side_effect=graph), \
                    self.assertRaisesRegex(M365GraphError, error):
                wizard.action_issue_tap()
        wizard.tap_usable_once = True
        graph.routes[('POST', tap_path)] = M365GraphError('HTTP 400 - badRequest: Policy.')
        with patch(GRAPH_PATH, side_effect=graph):
            wizard.action_issue_tap()
        # Shown in the wizard: the audit log line is kept
        self.assertIn('not issued', wizard.error)
        self.assertEqual(self.env['m365.audit.log'].search(
            [('tenant_id', '=', self.tenant.id), ('action', '=', 'tap')], limit=1).status,
            'failed')
