# -*- coding: utf-8 -*-
import urllib.parse
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools.misc import hash_sign

from ..models.m365_tenant import M365GraphError
from ..models.m365_tenant_consent import CONSENT_SCOPE
from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_A, GUID_B, MODELS, USER_GROUP, M365Common,
                     graph_stub, http_response, map_tenants)

TOKEN_POST = MODELS + '.m365_tenant.requests.post'
SLEEP = MODELS + '.m365_tenant_consent.time.sleep'
TENANT_URL = '/odoo/action-m365_license_management.action_m365_tenant/%s'


def graph_ok(method, path, params=None, json_body=None, paginate=False):
    """Microsoft Graph answering the shared app."""
    if path == '/organization':
        return [{'id': 'tid-contoso', 'displayName': 'Contoso'}]
    return graph_stub(method, path, params, json_body, paginate)


def query_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class ConsentCommon(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['m365.partner.account'].create({
            'name': 'BXI CSP', 'partner_tenant_id': 'partner-tid', 'client_id': 'pc-cid',
            'client_secret': 'pc-secret', 'graph_client_id': 'shared-cid',
            'graph_client_secret': 'shared-secret',
        })
        cls.admin = new_test_user(cls.env, 'm365_consent_admin', groups=ADMIN_GROUP)
        ICP = cls.env['ir.config_parameter']
        ICP.set_param('web.base.url', 'https://odoo.example.com')
        ICP.set_param('m365.webhook_base_url', False)

    def _grant(self, tenant=None):
        """Consent link created by the Microsoft 365 administrator: its state."""
        action = (tenant or self.tenant).with_user(self.admin).action_grant_consent()
        return query_of(action['url'])['state']


@tagged('post_install', '-at_install')
class TestM365TenantConsent(ConsentCommon):

    def _callback(self, state, **params):
        return self.env['m365.tenant']._consent_callback(dict(
            {'admin_consent': 'True', 'tenant': 'tid-contoso', 'state': state}, **params))

    # ------------------------------------------------------------------
    # Connection modes
    # ------------------------------------------------------------------
    def test_shared_app_token(self):
        self.tenant.write({'connection_mode': 'shared_app', 'partner_account_id': self.account.id})
        self.assertEqual(self.tenant.state, 'draft')
        token = http_response(200, {'access_token': 'shared-tok', 'expires_in': 3600})
        with patch(TOKEN_POST, return_value=token) as post:
            self.assertEqual(self.tenant._get_access_token(), 'shared-tok')
        # Token of the shared app, requested in the directory of the customer tenant
        self.assertEqual(post.call_args.args,
                         ('https://login.microsoftonline.com/tid-contoso/oauth2/v2.0/token',))
        self.assertEqual(post.call_args.kwargs['data'], {
            'client_id': 'shared-cid', 'client_secret': 'shared-secret',
            'scope': 'https://graph.microsoft.com/.default', 'grant_type': 'client_credentials',
        })
        # Back to the own app registration: the token of the shared app is forgotten
        self.tenant.connection_mode = 'own_app'
        self.assertFalse(self.tenant.sudo().access_token)
        with patch(TOKEN_POST, return_value=http_response(200, {'access_token': 'own'})) as post:
            self.tenant._get_access_token()
        self.assertEqual(post.call_args.kwargs['data']['client_id'], 'cid')

    def test_shared_app_errors(self):
        self.tenant.connection_mode = 'shared_app'
        with self.assertRaisesRegex(M365GraphError, 'Select the shared app of the tenant'):
            self.tenant._get_access_token()
        self.tenant.partner_account_id = self.account
        self.account.graph_client_secret = False
        with self.assertRaisesRegex(M365GraphError,
                                    'shared app on the Partner Center account BXI CSP'):
            self.tenant._get_access_token()

    def test_connection_ids_per_mode(self):
        Tenant = self.env['m365.tenant']
        shared = Tenant.create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'connection_mode': 'shared_app', 'partner_account_id': self.account.id,
        })
        self.assertFalse(shared.client_id)
        with self.assertRaisesRegex(UserError, r'Directory \(Tenant\) ID is required'):
            Tenant.create({'name': 'Litware', 'domain': 'litware.com',
                           'connection_mode': 'shared_app'})
        with self.assertRaisesRegex(UserError, r'Application \(Client\) ID are required'):
            shared.connection_mode = 'own_app'

    # ------------------------------------------------------------------
    # Consent link
    # ------------------------------------------------------------------
    def test_grant_consent(self):
        self.tenant.partner_account_id = self.account
        user = map_tenants(new_test_user(self.env, 'm365_consent_user', groups=USER_GROUP),
                           self.tenant)
        with self.assertRaises(AccessError):
            self.tenant.with_user(user).action_grant_consent()

        action = self.tenant.with_user(self.admin).action_grant_consent()
        self.assertEqual((action['type'], action['target']), ('ir.actions.act_url', 'new'))
        parts = urllib.parse.urlsplit(action['url'])
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ('https', 'login.microsoftonline.com', '/tid-contoso/v2.0/adminconsent'))
        query = query_of(action['url'])
        self.assertEqual({key: value for key, value in query.items() if key != 'state'}, {
            'client_id': 'shared-cid',
            'scope': 'https://graph.microsoft.com/.default',
            'redirect_uri': 'https://odoo.example.com/m365/consent/callback',
        })
        self.assertEqual(self.env['m365.tenant']._consent_tenant_from_state(query['state']),
                         self.tenant)
        tenant = self.tenant.sudo()
        self.assertTrue(tenant.consent_nonce)
        self.assertEqual(tenant.consent_requested_by, self.admin)
        self.assertAlmostEqual(tenant.consent_expiry, fields.Datetime.now() + timedelta(days=7),
                               delta=timedelta(minutes=1))
        self.assertEqual(self.tenant.with_user(self.admin).consent_url, action['url'])
        # Nothing changes before the consent
        self.assertEqual((tenant.connection_mode, tenant.state), ('own_app', 'connected'))
        self.assertEqual(self._logs('consent').status, 'info')

    def test_grant_consent_default_account(self):
        self.env['m365.partner.account'].search([('id', '!=', self.account.id)]).active = False
        self._grant()
        self.assertEqual(self.tenant.sudo().partner_account_id, self.account)

    def test_grant_consent_requirements(self):
        new = self.env['m365.tenant'].create({
            'name': 'New Co', 'domain': 'newco', 'tenant_type': 'new',
            'partner_account_id': self.account.id,
        })
        with self.assertRaisesRegex(UserError, 'Create the tenant in Microsoft 365 first'):
            new.with_user(self.admin).action_grant_consent()
        self.tenant.partner_account_id = self.account
        self.account.graph_client_secret = False
        with self.assertRaisesRegex(UserError, 'client secret of the shared app'):
            self._grant()

    # ------------------------------------------------------------------
    # Callback
    # ------------------------------------------------------------------
    def test_callback_success(self):
        self.tenant.partner_account_id = self.account
        state = self._grant()
        with patch(GRAPH_PATH, side_effect=graph_ok):
            result = self._callback(state)
        self.assertEqual((result['status'], result['tenant']), ('success', self.tenant))
        tenant = self.tenant.sudo()
        self.assertEqual((tenant.connection_mode, tenant.state), ('shared_app', 'connected'))
        self.assertFalse(tenant.consent_nonce)
        self.assertFalse(tenant.consent_expiry)
        self.assertTrue(tenant.consent_date)
        log = self._logs('consent').filtered(lambda line: line.status == 'success')
        self.assertEqual(log.user_id, self.admin)
        self.assertIn('connected through the shared app of BXI CSP', log.message)
        self.assertEqual(self._logs('test_connection').user_id, self.admin)
        # The link is used up
        with patch(GRAPH_PATH, side_effect=graph_ok) as graph:
            self.assertEqual(self._callback(state)['status'], 'invalid')
        graph.assert_not_called()

    def test_callback_refused_states(self):
        self.tenant.partner_account_id = self.account
        old = self._grant()
        state = self._grant()  # a new link replaces the previous one
        nonce = self.tenant.sudo().consent_nonce
        expired = hash_sign(self.env, CONSENT_SCOPE, {'tenant': self.tenant.id, 'nonce': nonce},
                            expiration=timedelta(seconds=-1))
        forged = hash_sign(self.env, 'another.scope', {'tenant': self.tenant.id, 'nonce': nonce})
        tampered = state[:-3] + ('AAA' if not state.endswith('AAA') else 'BBB')
        with patch(GRAPH_PATH, side_effect=graph_ok) as graph:
            for bad in (old, expired, forged, tampered, 'garbage', '', None):
                self.assertEqual(self._callback(bad)['status'], 'invalid', bad)
            # Expired in Odoo, e.g. after a change of the validity
            self.tenant.sudo().consent_expiry = fields.Datetime.now() - timedelta(minutes=1)
            self.assertEqual(self._callback(state)['status'], 'invalid')
        graph.assert_not_called()
        self.assertEqual(self.tenant.connection_mode, 'own_app')

    def test_callback_not_granted(self):
        self.tenant.partner_account_id = self.account
        self.tenant.tenant_id = GUID_B
        state = self._grant()
        with patch(GRAPH_PATH, side_effect=graph_ok) as graph:
            declined = self._callback(state, admin_consent=None, tenant=None,
                                      error='access_denied',
                                      error_description='AADSTS65004: User declined to consent.')
            other = self._callback(state, tenant=GUID_A)
        graph.assert_not_called()
        self.assertEqual((declined['status'], other['status']), ('failed', 'failed'))
        self.assertIn('access_denied: AADSTS65004', declined['message'])
        self.assertIn('another organisation', other['message'])
        self.assertEqual(len(self._logs('consent').filtered(lambda l: l.status == 'failed')), 2)
        # The link can be used again; the directory ID is compared ignoring case
        self.assertTrue(self.tenant.sudo().consent_nonce)
        with patch(GRAPH_PATH, side_effect=graph_ok):
            self.assertEqual(self._callback(state, tenant=GUID_B.upper())['status'], 'success')

    def test_callback_app_cannot_read_tenant(self):
        """Nothing changes until the shared app reads the tenant; Check Consent later."""
        self.tenant.partner_account_id = self.account
        self.tenant.sudo().write({'access_token': 'own-tok',
                                  'token_expiry': fields.Datetime.now() + timedelta(hours=1)})
        state = self._grant()
        denied = M365GraphError("Authentication failed. HTTP 400 - invalid_client: "
                                "AADSTS700016: Application not found in the directory.")
        with patch(GRAPH_PATH, side_effect=denied) as graph, patch(SLEEP) as sleep:
            result = self._callback(state)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('AADSTS700016', result['message'])
        self.assertEqual((graph.call_count, sleep.call_count), (3, 2))
        tenant = self.tenant.sudo()
        self.assertEqual((tenant.connection_mode, tenant.state, tenant.access_token),
                         ('own_app', 'connected', 'own-tok'))
        self.assertTrue(tenant.consent_nonce)
        self.assertFalse(tenant.consent_date)
        self.assertEqual(self._logs('consent').filtered(lambda l: l.status == 'failed').user_id,
                         self.admin)

        with patch(GRAPH_PATH, side_effect=denied):
            action = self.tenant.with_user(self.admin).action_check_consent()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(tenant.connection_mode, 'own_app')
        # Microsoft applied the consent
        with patch(GRAPH_PATH, side_effect=graph_ok):
            action = self.tenant.with_user(self.admin).action_check_consent()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual((tenant.connection_mode, tenant.state), ('shared_app', 'connected'))
        self.assertFalse(tenant.consent_nonce)

    # ------------------------------------------------------------------
    # Secrets changed by a Microsoft 365 administrator
    # ------------------------------------------------------------------
    def test_secret_changes(self):
        """Microsoft 365 administrators, not system administrators, change secrets; the
        tokens of the changed app only are forgotten."""
        shared = self.env['m365.tenant'].create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'connection_mode': 'shared_app', 'partner_account_id': self.account.id,
            'state': 'connected',
        })
        token = {'access_token': 'tok', 'token_expiry': fields.Datetime.now() + timedelta(hours=1)}
        (self.tenant | shared).sudo().write(token)
        self.account.sudo().write(token)

        self.account.with_user(self.admin).graph_client_secret = 'new-shared-secret'
        self.assertFalse(shared.sudo().access_token)
        self.assertEqual(shared.state, 'connected')
        self.assertEqual(self.tenant.sudo().access_token, 'tok')
        self.assertEqual(self.account.sudo().access_token, 'tok')

        self.account.with_user(self.admin).client_secret = 'new-pc-secret'
        self.assertFalse(self.account.sudo().access_token)
        self.assertEqual(self.account.state, 'draft')

        self.tenant.with_user(self.admin).client_secret = 'new-secret'
        self.assertFalse(self.tenant.sudo().access_token)
        self.assertEqual(self.tenant.state, 'draft')

        # Another account of the shared app: new token, connection to test again
        other = self.account.copy({'name': 'Other CSP', 'partner_tenant_id': 'other-tid',
                                   'client_id': 'other-cid'})
        shared.sudo().write(dict(token, state='connected'))
        shared.with_user(self.admin).partner_account_id = other
        self.assertEqual(shared.state, 'draft')
        self.assertFalse(shared.sudo().access_token)


@tagged('post_install', '-at_install')
class TestM365ConsentCallbackPage(HttpCase, ConsentCommon):

    def _open(self, params):
        # The request runs in another thread on the cursor of the test
        self.env.flush_all()
        resp = self.url_open('/m365/consent/callback?' + urllib.parse.urlencode(params))
        self.env.invalidate_all()
        return resp

    def test_callback_pages(self):
        resp = self._open({'state': 'invalid'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('not valid or has expired', resp.text)
        self.assertNotIn('Tenant:', resp.text)

        self.tenant.partner_account_id = self.account
        state = self._grant()
        with patch(GRAPH_PATH, side_effect=graph_ok):
            resp = self._open({'admin_consent': 'True', 'tenant': 'tid-contoso',
                               'state': state})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('now connected to Odoo through the shared app', resp.text)
        self.assertIn('You can close this window', resp.text)
        self.assertNotIn(TENANT_URL % self.tenant.id, resp.text)
        self.assertEqual(self.tenant.connection_mode, 'shared_app')

    def test_callback_page_of_administrator(self):
        self.tenant.partner_account_id = self.account
        state = self._grant()
        self.authenticate(self.admin.login, self.admin.login)
        with patch(GRAPH_PATH, side_effect=graph_ok):
            resp = self._open({'admin_consent': 'True', 'tenant': 'tid-contoso',
                               'state': state})
        self.assertIn(TENANT_URL % self.tenant.id, resp.text)
