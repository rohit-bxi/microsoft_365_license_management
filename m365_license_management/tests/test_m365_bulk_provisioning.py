# -*- coding: utf-8 -*-
"""Bulk provisioning without manual steps (19.0.1.8.0): partner user sign-in, Customer
Agreement, shared app installation, GDAP access assignment, automatic domains, users of
the customer file, automatic run, offers per country and the report."""
import base64
import io
import json
import urllib.parse
from datetime import timedelta
import unittest
from unittest.mock import patch

import openpyxl

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from .common import ADMIN_GROUP, GRAPH_PATH, GUID_A, MODELS, FakeGraph, http_response
from .test_m365_provisioning import (CATALOG_ID, CUSTOMER, PARTNER_GRAPH, PC_PATH,
                                     RELATIONSHIPS, FakePartnerCenter, ProvisioningCommon,
                                     skus)

# The purchase through Partner Center and GDAP are disabled: their tests are kept, skipped
PC_DISABLED = "PARTNER CENTER (disabled): licenses are bought from Ingram Micro, no GDAP"
SIGNIN = MODELS + '.m365_partner_signin.M365PartnerAccountSignin'
PC_USER = SIGNIN + '._pc_user_request'
CUSTOMER_GRAPH = SIGNIN + '._customer_graph_request'
GRAPH_APP = '00000003-0000-0000-c000-000000000000'
NEW_USER_ID = 'cccccccc-dddd-eeee-ffff-000000000000'
REL_ROLES = ['fe930be7-5e62-47db-91af-98c3a49a38b1', 'e8611ab8-c189-46e8-94e1-60213ab1f814']


def jwt(claims):
    def enc(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return '%s.%s.sig' % (enc({'alg': 'none'}), enc(claims))


def relationship(status='active', rel_id='rel-1'):
    return {'id': rel_id, 'status': status, 'displayName': 'Default',
            'customer': {'tenantId': 'tid-contoso'},
            'accessDetails': {'unifiedRoles': [{'roleDefinitionId': r} for r in REL_ROLES]}}


class BulkCommon(ProvisioningCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sysadmin = new_test_user(cls.env, 'm365_bulk_sysadmin',
                                     groups='base.group_system,%s' % ADMIN_GROUP)
        cls.env['ir.config_parameter'].set_param('web.base.url', 'https://odoo.example.com')
        # Usage location of the new users and country of the catalog
        cls.tenant.country_id = cls.env.ref('base.in')

    def _sign_in(self):
        self.account.sudo().write({'pc_user_state': 'signed_in', 'pc_refresh_token': 'rt-1',
                                   'pc_user_name': 'automation@partner.example'})


@tagged('post_install', '-at_install')
class TestM365PartnerSignin(BulkCommon):

    def _start(self):
        action = self.account.with_user(self.sysadmin).action_partner_signin()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(action['url']).query)
        return action, query

    def test_signin_flow(self):
        action, query = self._start()
        self.assertIn('/partner-tid/oauth2/v2.0/authorize?', action['url'])
        self.assertEqual(query['client_id'], ['shared-cid'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertIn('user_impersonation', query['scope'][0])
        self.assertEqual(query['redirect_uri'],
                         ['https://odoo.example.com/m365/partner/signin/callback'])
        token = jwt({'amr': ['pwd', 'mfa'], 'upn': 'automation@partner.example',
                     'tid': 'partner-tid'})
        answer = http_response(200, {'access_token': token, 'refresh_token': 'rt-1',
                                     'expires_in': 3600})
        Account = self.env['m365.partner.account'].with_user(self.sysadmin)
        with patch(MODELS + '.m365_partner_signin.requests.post', return_value=answer) as post:
            result = Account._partner_signin_callback({'state': query['state'][0],
                                                       'code': 'auth-code'})
        self.assertEqual(result['status'], 'success', result['message'])
        data = post.call_args.kwargs['data']
        self.assertEqual((data['grant_type'], data['code']), ('authorization_code', 'auth-code'))
        self.assertTrue(data['code_verifier'])
        account = self.account.sudo()
        self.assertEqual((account.pc_user_state, account.pc_user_name, account.pc_refresh_token),
                         ('signed_in', 'automation@partner.example', 'rt-1'))
        # The state is used once
        result = Account._partner_signin_callback({'state': query['state'][0], 'code': 'x'})
        self.assertEqual(result['status'], 'invalid')

    def test_signin_refused(self):
        _action, query = self._start()
        Account = self.env['m365.partner.account']
        # Another user cannot use the sign-in of the administrator
        other = new_test_user(self.env, 'm365_bulk_other', groups='base.group_system')
        result = Account.with_user(other)._partner_signin_callback(
            {'state': query['state'][0], 'code': 'c'})
        self.assertEqual(result['status'], 'invalid')
        # No multi-factor authentication: refused
        answer = http_response(200, {'access_token': jwt({'amr': ['pwd']}),
                                     'refresh_token': 'rt'})
        with patch(MODELS + '.m365_partner_signin.requests.post', return_value=answer):
            result = Account.with_user(self.sysadmin)._partner_signin_callback(
                {'state': query['state'][0], 'code': 'c'})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('multi-factor', result['message'])
        self.assertEqual(self.account.pc_user_state, 'none')
        # Only system administrators sign in
        with self.assertRaises(AccessError):
            self.account.with_user(self.admin).action_partner_signin()

    def test_user_token(self):
        self._sign_in()
        answer = http_response(200, {'access_token': 'graph-tok', 'refresh_token': 'rt-2',
                                     'expires_in': 3600})
        with patch(MODELS + '.m365_partner_signin.requests.post', return_value=answer) as post:
            token = self.account._user_token('https://graph.microsoft.com/.default',
                                             tenant='tid-contoso')
        self.assertEqual(token, 'graph-tok')
        self.assertIn('/tid-contoso/oauth2/v2.0/token', post.call_args.args[0])
        self.assertEqual(self.account.sudo().pc_refresh_token, 'rt-2')  # rotated
        refused = http_response(400, {'error': 'invalid_grant', 'error_description': 'expired'})
        with patch(MODELS + '.m365_partner_signin.requests.post', return_value=refused):
            try:  # not assertRaises: its savepoint would undo the new state
                self.account._pc_user_request('GET', '/v1/agreements')
            except M365GraphError:
                pass
            else:
                self.fail("The refused token must raise")
        self.assertEqual(self.account.pc_user_state, 'expired')
        with self.assertRaisesRegex(M365GraphError, 'Sign in the partner user'):
            self.account.sudo().write({'pc_user_state': 'none', 'pc_refresh_token': False})
            self.account._user_token('x')

    def test_changed_app_signs_out(self):
        self._sign_in()
        self.account.graph_client_id = 'other-app'
        self.assertEqual(self.account.pc_user_state, 'none')
        self.assertFalse(self.account.sudo().pc_refresh_token)


@tagged('post_install', '-at_install')
class TestM365Agreement(BulkCommon):

    def setUp(self):
        super().setUp()
        self._sign_in()
        self.tenant.write({'mca_accepted': False, 'mca_email': False, 'mca_first_name': False,
                           'mca_date': False, 'contact_first_name': 'Megan',
                           'contact_last_name': 'Bowen', 'contact_email': 'megan@contoso.com'})

    def test_attest(self):
        pc = FakePartnerCenter({
            ('GET', '/v1/customers/%s/agreements?agreementType=MicrosoftCustomerAgreement'
             % CUSTOMER): {'items': []},
            ('GET', '/v1/agreements?agreementType=MicrosoftCustomerAgreement'):
                {'items': [{'templateId': 'tpl-1'}]},
        })
        with patch(PC_USER, side_effect=pc):
            with self.assertRaisesRegex(UserError, 'Confirm first'):
                self.tenant._attest_mca()
            self.tenant.mca_customer_confirmed = True
            self.tenant.with_user(self.admin).action_attest_mca()
        body = [b for m, p, b in pc.calls if m == 'POST'][0]
        self.assertEqual(body['templateId'], 'tpl-1')
        self.assertEqual(body['type'], 'MicrosoftCustomerAgreement')
        self.assertEqual(body['primaryContact'], {'firstName': 'Megan', 'lastName': 'Bowen',
                                                  'email': 'megan@contoso.com'})
        self.assertTrue(self.tenant._mca_recorded())
        self.assertTrue(self.tenant.mca_attested)
        self.assertEqual(self.account.mca_template_id, 'tpl-1')

    def test_already_accepted(self):
        pc = FakePartnerCenter({
            ('GET', '/v1/customers/%s/agreements?agreementType=MicrosoftCustomerAgreement'
             % CUSTOMER): {'items': [{'dateAgreed': '2026-10-01T00:00:00Z', 'primaryContact': {
                 'firstName': 'Lee', 'lastName': 'Gu', 'email': 'lee@contoso.com'}}]},
        })
        with patch(PC_USER, side_effect=pc):
            self.assertTrue(self.tenant._attest_mca())
        self.assertEqual(pc.paths('POST'), [])
        self.assertEqual((self.tenant.mca_email, str(self.tenant.mca_date)),
                         ('lee@contoso.com', '2026-10-01'))


@tagged('post_install', '-at_install')
class TestM365AppInstall(BulkCommon):

    def setUp(self):
        super().setUp()
        self._sign_in()
        self.account.install_permissions = 'User.ReadWrite.All, Domain.ReadWrite.All'
        self.tenant.state = 'draft'
        self.calls = []

    def _graph(self, granted=()):
        def call(tenant, method, path, params=None, json_body=None, paginate=False):
            self.calls.append((tenant, method, path, json_body))
            flt = (params or {}).get('$filter', '')
            if path == '/servicePrincipals' and GRAPH_APP in flt:
                return [{'id': 'graph-sp', 'appRoles': [
                    {'value': 'User.ReadWrite.All', 'id': 'role-user',
                     'allowedMemberTypes': ['Application']},
                    {'value': 'Domain.ReadWrite.All', 'id': 'role-domain',
                     'allowedMemberTypes': ['Application']}]}]
            if path == '/servicePrincipals':
                return [{'id': 'our-sp', 'appId': 'shared-cid'}]
            if path == '/servicePrincipals/our-sp/appRoleAssignments':
                return [{'appRoleId': r, 'resourceId': 'graph-sp'} for r in granted]
            return {}
        return call

    def test_install(self):
        pc = FakePartnerCenter()
        with patch(PC_USER, side_effect=pc), \
                patch(CUSTOMER_GRAPH, side_effect=self._graph(granted=['role-user'])), \
                patch(GRAPH_PATH, side_effect=FakeGraph()):
            self.tenant.with_user(self.admin).action_install_shared_app()
        consent = pc.calls[0]
        self.assertEqual(consent[:2], ('POST', '/v1/customers/%s/applicationconsents'
                                       % CUSTOMER))
        self.assertEqual(consent[2]['applicationId'], 'shared-cid')
        self.assertEqual(consent[2]['applicationGrants'][0]['enterpriseApplicationId'],
                         GRAPH_APP)
        posts = [(p, b) for _t, m, p, b in self.calls if m == 'POST']
        # Only the missing role is granted
        self.assertEqual(posts, [('/servicePrincipals/graph-sp/appRoleAssignedTo', {
            'principalId': 'our-sp', 'resourceId': 'graph-sp', 'appRoleId': 'role-domain'})])
        self.assertEqual({t for t, _m, _p, _b in self.calls}, {'tid-contoso'})
        self.assertEqual((self.tenant.app_install_state, self.tenant.connection_mode,
                          self.tenant.state), ('done', 'shared_app', 'connected'))

    def test_unknown_permission(self):
        self.account.install_permissions = 'Nothing.ReadWrite.All'
        with patch(PC_USER, side_effect=FakePartnerCenter()), \
                patch(CUSTOMER_GRAPH, side_effect=self._graph()):
            self.tenant.with_user(self.admin).action_install_shared_app()
        self.assertEqual(self.tenant.app_install_state, 'failed')
        self.assertIn('Nothing.ReadWrite.All', self.tenant.app_install_error)
        self.assertEqual(self.tenant.connection_mode, 'own_app')


@tagged('post_install', '-at_install')
class TestM365GdapAssignment(BulkCommon):

    @unittest.skip(PC_DISABLED)
    def test_assignment(self):
        self.account.write({'gdap_group_id': 'GROUP-1',
                            'gdap_assignment_role_ids': '%s,unknown-role' % REL_ROLES[1]})
        assignments = []

        def partner_graph(method, path, params=None, json_body=None, paginate=False):
            if path == RELATIONSHIPS:
                return [relationship()]
            if path.endswith('/accessAssignments') and method == 'GET':
                return assignments
            if method == 'POST':
                assignments.append({'id': 'as-1', 'status': 'pending',
                                    'accessContainer': {'accessContainerId': 'group-1'}})
                return dict(assignments[0], body=json_body)
            return {}

        with patch(PARTNER_GRAPH, side_effect=partner_graph) as call:
            self.tenant._refresh_gdap()
            self.assertEqual(self.tenant.gdap_assignment_status, 'none')
            self.assertEqual(self.tenant.prov_gdap, 'pending')
            self.assertEqual(self.tenant._ensure_gdap_assignment(), 'pending')
            post = [c for c in call.call_args_list if c.args[0] == 'POST'][0]
            self.assertEqual(post.args[1], RELATIONSHIPS + '/rel-1/accessAssignments')
            self.assertEqual(post.kwargs['json_body']['accessDetails']['unifiedRoles'],
                             [{'roleDefinitionId': REL_ROLES[1]}])
            # Not created twice
            self.assertEqual(self.tenant._ensure_gdap_assignment(), 'pending')
            self.assertEqual(len(assignments), 1)
            assignments[0]['status'] = 'active'
            self.tenant._refresh_gdap()
        self.assertEqual(self.tenant.prov_gdap, 'done')

    @unittest.skip(PC_DISABLED)
    def test_no_group(self):
        with patch(PARTNER_GRAPH, return_value=[relationship()]):
            self.tenant._refresh_gdap()
            self.assertFalse(self.tenant._ensure_gdap_assignment())
        self.assertEqual(self.tenant.gdap_role_ids, ','.join(REL_ROLES))
        self.assertEqual(self.tenant.prov_gdap, 'done')


@tagged('post_install', '-at_install')
class TestM365DomainAutomation(BulkCommon):

    def setUp(self):
        super().setUp()
        self.tenant.write({'custom_domains': 'fabrikam-example.com; fabrikam-example.in',
                           'custom_domain_default': True})
        self.name = 'fabrikam-example.com'
        self.graph = FakeGraph({
            ('POST', '/domains'): lambda body: {'id': body['id'], 'isVerified': False},
            '/domains/%s/verificationDnsRecords' % self.name: [
                {'recordType': 'Txt', 'label': '@', 'text': 'MS=ms1234', 'ttl': 3600}],
            ('POST', '/domains/%s/verify' % self.name): M365GraphError(
                'HTTP 400 - Request_BadRequest: Domain verification failed'),
            ('POST', '/domains/fabrikam-example.in'): M365GraphError('x'),
            '/domains/fabrikam-example.in/verificationDnsRecords': [],
        })

    def test_add_and_verify(self):
        with patch(GRAPH_PATH, side_effect=self.graph):
            domains = self.tenant._ensure_custom_domains()
            self.assertEqual(domains.mapped('state'), ['to_add', 'to_add'])
            self.assertEqual(domains.mapped('make_default'), [True, False])
            domain = domains[0]
            domain._auto_step()
            self.assertEqual((domain.state, domain.txt_record), ('pending', 'MS=ms1234'))
            self.assertEqual(self.tenant.dns_pending_domain_ids, domain)
            # Not verified yet: checked again later
            domain.next_check = fields.Datetime.now() - timedelta(minutes=1)
            self.env['m365.tenant.domain']._cron_auto_domains()
            self.assertEqual((domain.state, domain.verify_attempts), ('pending', 1))
            self.assertGreater(domain.next_check, fields.Datetime.now())
            # Verified: made the default domain
            self.graph.routes[('POST', '/domains/%s/verify' % self.name)] = {
                'id': self.name, 'isVerified': True}
            domain.next_check = fields.Datetime.now() - timedelta(minutes=1)
            self.env['m365.tenant.domain']._cron_auto_domains()
        self.assertEqual((domain.state, domain.is_verified, domain.is_default),
                         ('verified', True, True))
        self.assertIn(('PATCH', '/domains/%s' % self.name, {'isDefault': True}),
                      self.graph.calls)
        self.assertEqual(self.tenant.prov_domains, 'pending')  # the .in one is left

    def test_refused_and_late(self):
        self.graph.routes[('POST', '/domains')] = M365GraphError(
            'HTTP 400 - Request_BadRequest: The domain is already in another tenant')
        with patch(GRAPH_PATH, side_effect=self.graph):
            domains = self.tenant._ensure_custom_domains()
            domains[0]._auto_step()
        self.assertEqual(domains[0].state, 'failed')
        self.assertIn('another tenant', domains[0].error)
        self.assertEqual(self.tenant.prov_domains, 'failed')
        # A domain waiting for its DNS record more than 72 hours needs a person
        domain = domains[1]
        domain.write({'state': 'pending', 'added_date': fields.Datetime.now() - timedelta(
            hours=73), 'txt_record': 'MS=ms99'})
        self.graph.routes[('POST', '/domains/fabrikam-example.in/verify')] = M365GraphError(
            'HTTP 400 - not verified')
        with patch(GRAPH_PATH, side_effect=self.graph):
            domain._auto_step()
        self.assertEqual(domain.state, 'action')
        self.assertTrue(self.tenant.activity_ids.filtered(
            lambda a: 'fabrikam-example.in' in a.summary))
        with patch(GRAPH_PATH, side_effect=self.graph):
            domain.with_user(self.admin).action_retry()
        self.assertEqual(domain.state, 'pending')

    def test_sync_keeps_domains_to_add(self):
        with patch(GRAPH_PATH, side_effect=self.graph):
            domains = self.tenant._ensure_custom_domains()
            self.tenant._sync_domains()  # Microsoft does not know them yet
        self.assertTrue(domains.exists())

    def test_send_dns_records(self):
        domain = self.env['m365.tenant.domain'].create({
            'tenant_id': self.tenant.id, 'name': self.name, 'state': 'pending',
            'txt_record': 'MS=ms1234'})
        action = self.tenant.with_user(self.admin).action_send_dns_records()
        self.assertEqual(action['res_model'], 'mail.compose.message')
        template = self.env.ref('m365_license_management.mail_template_m365_dns_records')
        body = template._render_field('body_html', self.tenant.ids)[self.tenant.id]
        self.assertIn('MS=ms1234', body)
        domain.state = 'verified'
        with self.assertRaises(UserError):
            self.tenant.with_user(self.admin).action_send_dns_records()


@tagged('post_install', '-at_install')
class TestM365PendingUsers(BulkCommon):

    def setUp(self):
        super().setUp()
        self.tenant.custom_domains = 'contoso.com'
        self.env['m365.tenant.domain'].create({
            'tenant_id': self.tenant.id, 'name': 'contoso.com', 'is_verified': True,
            'state': 'verified'})
        self.account.default_offer_id = self.offer
        self.graph = FakeGraph({
            ('POST', '/users'): lambda body: {'id': NEW_USER_ID,
                                              'userPrincipalName': body['userPrincipalName'],
                                              'displayName': body['displayName']},
        })

    def _pending(self, **vals):
        return self.env['m365.tenant.pending.user'].create(dict({
            'tenant_id': self.tenant.id, 'user_name': 'nestor', 'name': 'Nestor W',
            'licenses': 'default', 'requested_by': self.admin.id}, **vals))

    def test_create_with_license(self):
        self.tenant.auto_approve_admins = True
        pending = self._pending()
        self.assertEqual(pending.upn, 'nestor@contoso.com')
        with patch(GRAPH_PATH, side_effect=self.graph):
            self.env['m365.tenant.pending.user']._cron_create_users()
        self.assertEqual(pending.state, 'licensed', pending.error)
        body = self.graph.bodies('POST', '/users')[0]
        self.assertEqual((body['userPrincipalName'], body['usageLocation']),
                         ('nestor@contoso.com', 'IN'))
        self.assertEqual(pending.request_ids.sku_id, self.sku)
        self.assertEqual(pending.request_ids.state, 'done')
        self.assertTrue(pending.sudo().password)
        self.assertTrue(pending.has_password)

    def test_waiting_reasons(self):
        pending = self._pending(user_name='nestor@contoso.in', usage_location='IN')
        self.tenant.state = 'draft'
        pending._try_create()
        self.assertEqual(pending.wait_reason, 'Connection of the tenant')
        self.tenant.state = 'connected'
        pending._try_create()
        self.assertIn('contoso.in', pending.wait_reason)
        pending.user_name = 'nestor'
        pending.licenses = 'Unknown Plan'
        pending._try_create()
        self.assertEqual(pending.wait_reason, 'License Unknown Plan in the tenant')
        pending.licenses = 'Microsoft 365 E3'  # the Partner Center offer name
        self.sku.consumed_units = 10
        pending._try_create()
        self.assertIn('A free', pending.wait_reason)
        self.assertEqual(pending.state, 'waiting')
        self.assertEqual(self.tenant.prov_users, 'pending')

    def test_refused_by_microsoft(self):
        self.graph.routes[('POST', '/users')] = M365GraphError(
            'HTTP 400 - Request_BadRequest: Another object with the same value exists')
        pending = self._pending(licenses=False)
        with patch(GRAPH_PATH, side_effect=self.graph):
            pending.with_user(self.admin).action_create_now()
        self.assertEqual(pending.state, 'failed')
        self.assertEqual(self.tenant.prov_users, 'failed')


@tagged('post_install', '-at_install')
class TestM365PendingUserCredentials(HttpCase, BulkCommon):

    def test_download_once(self):
        pending = self.env['m365.tenant.pending.user'].create({
            'tenant_id': self.tenant.id, 'user_name': 'nestor', 'name': 'Nestor W',
            'state': 'created'})
        pending.sudo().password = 'Secret-123!'
        self.authenticate(self.user.login, self.user.login)
        self.assertEqual(self.url_open('/m365/export/credentials?ids=%s' % pending.id)
                         .status_code, 404)  # administrators only
        self.authenticate(self.admin.login, self.admin.login)
        self.env.flush_all()
        resp = self.url_open('/m365/export/credentials?ids=%s' % pending.id)
        self.assertEqual(resp.status_code, 200)
        rows = list(openpyxl.load_workbook(io.BytesIO(resp.content)).active.iter_rows(
            values_only=True))
        self.assertEqual(rows[1][1:4], ('nestor@contoso.com', 'Nestor W', 'Secret-123!'))
        pending.invalidate_recordset()
        self.assertFalse(pending.sudo().password)
        self.assertEqual(self.url_open('/m365/export/credentials?ids=%s' % pending.id)
                         .status_code, 404)


@tagged('post_install', '-at_install')
class TestM365AutoProvision(BulkCommon):

    def setUp(self):
        super().setUp()
        self.tenant.write({
            'custom_domains': 'contoso-example.com', 'custom_domain_default': True,
            'requested_subscriptions': 'Microsoft 365 E3 = 2', 'prov_approve_orders': True,
            'auto_approve_admins': True, 'auto_provision': True, 'prov_user_id': self.admin.id,
        })
        self.env['m365.tenant.pending.user'].create({
            'tenant_id': self.tenant.id, 'user_name': 'nestor', 'name': 'Nestor W',
            'licenses': 'Microsoft 365 E3', 'requested_by': self.admin.id})
        name = 'contoso-example.com'
        self.graph = FakeGraph({
            '/subscribedSkus': skus(12),
            ('POST', '/domains'): {'id': name, 'isVerified': False},
            '/domains/%s/verificationDnsRecords' % name: [
                {'recordType': 'Txt', 'label': '@', 'text': 'MS=ms42', 'ttl': 3600}],
            ('POST', '/domains/%s/verify' % name): {'id': name, 'isVerified': True},
            ('POST', '/users'): {'id': NEW_USER_ID, 'userPrincipalName': 'nestor@' + name},
            '/users/%s' % NEW_USER_ID: {'id': NEW_USER_ID, 'accountEnabled': True},
            '/users/%s/licenseDetails' % NEW_USER_ID: [
                {'skuId': GUID_A, 'skuPartNumber': 'SPE_E3', 'servicePlans': []}],
        })
        cart = '/v1/customers/%s/carts' % CUSTOMER
        self.pc = FakePartnerCenter({
            ('POST', cart): {'id': 'cart-1', 'lineItems': []},
            ('POST', cart + '/cart-1/checkout'): {'orders': [{
                'id': 'pc-1', 'status': 'pending', 'currencyCode': 'INR', 'lineItems': [
                    {'offerId': CATALOG_ID, 'subscriptionId': 'sub-1'}]}]},
            ('GET', '/v1/customers/%s/orders/pc-1' % CUSTOMER): {
                'id': 'pc-1', 'status': 'completed', 'lineItems': [
                    {'offerId': CATALOG_ID, 'subscriptionId': 'sub-1'}]},
        })

    def _run(self):
        with patch(GRAPH_PATH, side_effect=self.graph), \
                patch(PC_PATH, side_effect=self.pc), \
                patch(PARTNER_GRAPH, return_value=[relationship()]):
            self.env['m365.tenant']._cron_auto_provision()

    @unittest.skip(PC_DISABLED)
    def test_full_run(self):
        self._run()
        tenant = self.tenant
        self.assertEqual(tenant.prov_gdap, 'done')
        self.assertEqual(tenant.pc_order_ids.state, 'done')
        self.assertEqual(tenant.pc_order_ids.approver_id, self.admin)
        self.assertEqual(tenant.prov_domains, 'pending')
        self.assertIn('MS=ms42', tenant.prov_waiting)
        self.assertIn('contoso-example.com', tenant.prov_waiting)
        self.assertGreater(tenant.prov_next_run, fields.Datetime.now())
        self.assertTrue(tenant.auto_provision)
        # The DNS record is there: the domain, the user, its license and the verification
        tenant.domain_ids.next_check = fields.Datetime.now() - timedelta(minutes=1)
        tenant.prov_next_run = fields.Datetime.now() - timedelta(minutes=1)
        self._run()
        self.assertEqual(tenant.pending_user_ids.state, 'licensed',
                         tenant.pending_user_ids.error or tenant.pending_user_ids.wait_reason)
        self.assertEqual(tenant.pending_user_ids.upn, 'nestor@contoso-example.com')
        self.assertEqual(
            [tenant[s] for s in ('prov_tenant', 'prov_gdap', 'prov_agreement',
                                 'prov_relationship', 'prov_connection', 'prov_domains',
                                 'prov_subscriptions', 'prov_users', 'prov_licenses',
                                 'prov_verification')],
            ['done', 'done', 'done', 'na'] + ['done'] * 6)  # Partner Center: no Ingram
        self.assertEqual(tenant.prov_state, 'done')
        self.assertFalse(tenant.auto_provision)
        self.assertTrue(self._logs('auto_provision').filtered(lambda l: l.status == 'success'))

    @unittest.skip(PC_DISABLED)
    def test_errors_and_stop(self):
        def broken(method, path, params=None, json_body=None, paginate=False):
            raise M365GraphError('HTTP 503 - unavailable')

        for attempt in range(1, 6):
            self.tenant.prov_next_run = False
            with patch(PARTNER_GRAPH, side_effect=broken):
                self.env['m365.tenant']._cron_auto_provision()
            if attempt < 5:
                self.assertEqual(self.tenant.prov_attempts, attempt)
                self.assertTrue(self.tenant.auto_provision)
        self.assertFalse(self.tenant.auto_provision)
        self.assertIn('5 errors in a row', self.tenant.prov_error)
        self.assertTrue(self.tenant.activity_ids)

    @unittest.skip(PC_DISABLED)
    def test_needs_a_person(self):
        self.tenant.requested_subscriptions = 'Unknown Offer = 2'
        self._run()
        self.assertFalse(self.tenant.auto_provision)
        self.assertIn('Unknown Offer', self.tenant.prov_error)

    def test_start_and_stop(self):
        self.tenant.write({'auto_provision': False, 'prov_user_id': False})
        self.tenant.with_user(self.admin).action_auto_provision_start()
        self.assertEqual((self.tenant.auto_provision, self.tenant.prov_user_id),
                         (True, self.admin))
        self.tenant.with_user(self.admin).action_auto_provision_stop()
        self.assertFalse(self.tenant.auto_provision)
        with self.assertRaises(AccessError):
            self.tenant.with_user(self.user).action_auto_provision_start()
        # The run user is no longer an administrator: stopped
        self.tenant.write({'auto_provision': True,
                           'prov_user_id': self.user.id})
        self.tenant._auto_provision_run()
        self.assertFalse(self.tenant.auto_provision)


@tagged('post_install', '-at_install')
class TestM365OffersAndImport(BulkCommon):

    def test_default_offer_and_ambiguity(self):
        self.account.default_offer_id = self.offer
        self.tenant.requested_subscriptions = '5'
        lines, errors = self.tenant._parse_requested_subscriptions()
        self.assertEqual((lines, errors), ([(self.offer, 5)], []))
        self.env['m365.pc.offer'].create({
            'account_id': self.account.id, 'name': 'Microsoft 365 E3',
            'sku_title': 'Microsoft 365 E3 (no Teams)', 'catalog_item_id': 'OTHER:0001:X',
            'product_code': 'OTHER', 'sku_code': '0001', 'term_duration': 'P1M'})
        self.tenant.requested_subscriptions = 'Microsoft 365 E3 = 3'
        _lines, errors = self.tenant._parse_requested_subscriptions()
        self.assertIn('matches several offers', errors[0])

    def test_country_catalog(self):
        self.offer.country = 'US'
        self.tenant.country_id = self.env.ref('base.in')
        self.tenant.requested_subscriptions = 'Microsoft 365 E3 = 3'
        _lines, errors = self.tenant._parse_requested_subscriptions()
        self.assertIn('catalog of IN', errors[0])
        self.assertEqual(self.account._catalog_countries(), ['IN'])
        self.tenant.country_id = self.env.ref('base.us')
        self.assertEqual(self.account._catalog_countries(), ['IN', 'US'])

    def test_license_name_from_offer(self):
        self.offer.write({'provisioning_id': 'dddddddd-0000-0000-0000-000000000001',
                          'sku_title': 'Microsoft 365 Business Basic (no Teams)'})
        items = [{'skuId': 'dddddddd-0000-0000-0000-000000000001',
                  'skuPartNumber': 'O365_BUSINESS_ESSENTIALS_NO_TEAMS_X',
                  'capabilityStatus': 'Enabled', 'consumedUnits': 0,
                  'prepaidUnits': {'enabled': 5}}]
        with patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': items})):
            self.tenant._sync_inventory()
        sku = self.tenant.sku_ids.filtered(
            lambda s: s.sku_part_number == 'O365_BUSINESS_ESSENTIALS_NO_TEAMS_X')
        self.assertEqual(sku.name, 'Microsoft 365 Business Basic (no Teams)')

    def _workbook(self, customers, users):
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'Customers'
        for row in customers:
            sheet.append(row)
        sheet = book.create_sheet('Users')
        for row in users:
            sheet.append(row)
        out = io.BytesIO()
        book.save(out)
        return base64.b64encode(out.getvalue())

    def test_import_domains_and_users(self):
        self.env['m365.tenant.domain'].create({'tenant_id': self.tenant.id,
                                               'name': 'taken-example.com'})
        header = ['CompanyName', 'Domain', 'FirstName', 'LastName', 'Email', 'AddressLine1',
                  'City', 'PostalCode', 'Country', 'CustomDomains', 'Subscriptions',
                  'PhoneNumber']
        customers = [header,
                     ['Litware One', 'litware-example.com', 'Lee', 'Gu', 'lee@litware.example',
                      '1 St', 'Austin', '73301', 'US', 'litware-example.net', '10', '1'],
                     ['Litware Two', 'litware-example.org', 'Lee', 'Gu', 'lee@litware.example',
                      '1 St', 'Austin', '73301', 'US', 'taken-example.com', '', '1']]
        users = [['CompanyName', 'UserName', 'DisplayName', 'Licenses'],
                 ['Litware One', 'adele', 'Adele V', 'default'],
                 ['litware-example.net', 'alex@litware-example.net', '', ''],
                 ['Litware One', 'bad user', 'Bad', ''],
                 ['Nobody', 'x', 'X', '']]
        wizard = self.env['m365.tenant.import.wizard'].with_user(self.admin).create({
            'tenant_type': 'new', 'partner_account_id': self.account.id,
            'file': self._workbook(customers, users), 'filename': 'customers.xlsx',
            'auto_provision': True, 'approve_orders': True, 'mca_confirmed': True})
        wizard.action_validate()
        orphans, one, two = wizard.line_ids.sorted('sn')  # the users row is SN 0
        self.assertIn('Users SN. 2', one.message)  # no display name
        self.assertIn('Users SN. 3', one.message)  # invalid user name
        self.assertIn('taken-example.com is already used', two.message)
        self.assertIn('"Nobody" is not in the file', orphans.message)
        with self.assertRaises(UserError):
            wizard.action_import()
        customers[2][9] = ''
        users[2][2], users[3][1] = 'Alex W', 'bad.user'
        del users[4]
        wizard.file = self._workbook(customers, users)
        wizard.action_validate()
        self.assertEqual(wizard.error_count, 0, wizard.line_ids.mapped('message'))
        self.assertEqual(wizard.user_count, 3)
        wizard.action_import()
        one = self.env['m365.tenant'].search([('name', '=', 'Litware One')])
        two = self.env['m365.tenant'].search([('name', '=', 'Litware Two')])
        self.assertEqual((one.domain, one.custom_domains),
                         ('litwareexample.onmicrosoft.com',
                          'litware-example.com; litware-example.net'))
        self.assertEqual(two.domain, 'litwareexample1.onmicrosoft.com')
        self.assertTrue(one.auto_initial_domain and one.custom_domain_default)
        self.assertTrue(one.auto_provision and one.prov_approve_orders
                        and one.mca_customer_confirmed)
        self.assertEqual(one.prov_user_id, self.admin)
        self.assertEqual(one.pending_user_ids.mapped('upn'),
                         ['adele@litware-example.com', 'alex@litware-example.net',
                          'bad.user@litware-example.com'])
        self.assertEqual(one.pending_user_ids[0].requested_by, self.admin)

    def test_free_initial_domain(self):
        tenant = self.env['m365.tenant'].create({
            'tenant_type': 'new', 'name': 'Adatum', 'domain': 'adatum',
            'auto_initial_domain': True})
        used = {'adatum.onmicrosoft.com', 'adatum1.onmicrosoft.com'}
        # PARTNER CENTER (disabled): HEAD /v1/domains; the tenant of the domain is looked
        # up in Microsoft 365 instead
        with patch(MODELS + '.m365_ingram_tenant.M365TenantIngramCreation._lookup_tenant_id',
                   side_effect=lambda domain: GUID_A if domain in used else False):
            self.assertEqual(tenant._check_domain(), [])
        self.assertEqual(tenant.domain, 'adatum2.onmicrosoft.com')

    def test_template(self):
        # Tenants of the database imported from the template must not clash with it
        self.env['m365.tenant'].with_context(active_test=False).search([
            '|', ('name', '=ilike', 'Contoso Ltd'),
            ('domain', '=ilike', 'contosoltd.onmicrosoft.com')]).write(
            {'name': 'Former Contoso Ltd', 'domain': 'former-contoso.example'})
        wizard = self.env['m365.tenant.import.wizard'].create(
            {'tenant_type': 'new', 'partner_account_id': self.account.id})
        wizard.action_download_template()
        book = openpyxl.load_workbook(io.BytesIO(base64.b64decode(wizard.template_file)))
        self.assertEqual(book.sheetnames, ['Customers', 'Users'])
        self.assertIn('Microsoft 365 Business Basic (no Teams) = 10',
                      [c.value for c in book['Customers'][2]])
        wizard.write({'file': wizard.template_file, 'filename': 'template.xlsx'})
        wizard.action_validate()
        self.assertEqual(wizard.error_count, 0, wizard.line_ids.mapped('message'))
        self.assertEqual(wizard.user_count, 2)


@tagged('post_install', '-at_install')
class TestM365BulkReport(HttpCase, BulkCommon):

    def test_report_sheets(self):
        self.env['m365.tenant.domain'].create({'tenant_id': self.tenant.id,
                                               'name': 'contoso.com', 'state': 'pending',
                                               'txt_record': 'MS=ms77'})
        self.env['m365.tenant.pending.user'].create({
            'tenant_id': self.tenant.id, 'user_name': 'nestor', 'name': 'Nestor W',
            'wait_reason': 'Verification of the domain contoso.com'})
        self.authenticate(self.admin.login, self.admin.login)
        self.env.flush_all()
        resp = self.url_open('/m365/export/provisioning?tenant_ids=%s' % self.tenant.id)
        book = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(book.sheetnames, ['Provisioning', 'Domains', 'Users', 'Licenses',
                                           'Subscriptions'])
        domains = list(book['Domains'].iter_rows(values_only=True))
        self.assertIn('MS=ms77', domains[1])
        users = [row[2] for row in book['Users'].iter_rows(values_only=True)]
        self.assertIn('nestor@contoso.com', users)
        self.assertIn('adele.vance@contoso.com', users)
