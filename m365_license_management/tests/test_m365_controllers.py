# -*- coding: utf-8 -*-
import csv
import io
from unittest.mock import patch

from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models.m365_tenant import M365GraphError
from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_A, GUID_B, USER_GROUP, USER_ID, M365Common,
                     graph_stub)

API = '/m365/api/v1'
ADMIN_ONLY = 'Only Microsoft 365 administrators can perform this action.'


@tagged('post_install', '-at_install')
class TestM365Controllers(HttpCase, M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, 'm365_api_user', groups=USER_GROUP)
        cls.admin = new_test_user(cls.env, 'm365_api_admin', groups=ADMIN_GROUP)
        cls.user_key = cls._api_key(cls.user)
        cls.admin_key = cls._api_key(cls.admin)

    @classmethod
    def _api_key(cls, user):
        return cls.env['res.users.apikeys'].with_user(user).sudo()._generate(None, 'Tests', None)

    def _open(self, url, **kwargs):
        # The requests run in another thread on the cursor of the test
        self.env.flush_all()
        resp = self.url_open(url, **kwargs)
        self.env.invalidate_all()
        return resp

    def _api(self, path, key, method=None, **kwargs):
        resp = self._open(API + path, headers={'Authorization': 'Bearer %s' % key},
                          method=method, **kwargs)
        return resp.status_code, resp.json()

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------
    def test_authentication(self):
        self.assertEqual(self._open(API + '/me').status_code, 401)
        resp = self._open(API + '/me', headers={'Authorization': 'Bearer invalid-key'})
        self.assertEqual(resp.status_code, 401)
        status, body = self._api('/me', self.user_key)
        self.assertEqual((status, body['ok']), (200, True))
        self.assertEqual(body['data'], {'id': self.user.id, 'name': self.user.name,
                                        'login': 'm365_api_user', 'is_admin': False,
                                        'is_m365_user': True})
        self.assertTrue(self._api('/me', self.admin_key)[1]['data']['is_admin'])

    def test_inventory_endpoints(self):
        self.env['m365.sku'].create({
            'tenant_id': self.tenant.id, 'sku_id': GUID_B, 'sku_part_number': 'VISIOCLIENT',
            'enabled_units': 5, 'consumed_units': 5})
        self.sku._record_snapshot()
        tenant_path = '/tenants/%s' % self.tenant.id
        status, body = self._api(tenant_path, self.user_key)
        self.assertEqual(status, 200)
        self.assertEqual({key: body['data'][key] for key in (
            'name', 'domain', 'state', 'sku_count', 'user_count', 'total_licenses',
            'assigned_licenses', 'available_licenses')}, {
            'name': 'Contoso', 'domain': 'contoso.com', 'state': 'connected', 'sku_count': 2,
            'user_count': 2, 'total_licenses': 15, 'assigned_licenses': 9,
            'available_licenses': 6})
        status, body = self._api('/tenants', self.user_key)
        self.assertIn(self.tenant.id, [tenant['id'] for tenant in body['data']])
        self.assertEqual(self._api('/tenants/2147483647', self.user_key),
                         (404, {'ok': False, 'error': 'Record not found.'}))
        # Subscriptions with licenses left
        status, body = self._api('/skus?available_only=1&tenant_id=%s' % self.tenant.id,
                                 self.user_key)
        self.assertEqual(body['data']['total'], 1)
        sku = body['data']['items'][0]
        self.assertEqual((sku['id'], sku['sku_id'], sku['name'], sku['total'], sku['assigned'],
                          sku['available'], sku['utilization']),
                         (self.sku.id, GUID_A, 'Microsoft 365 E3', 10, 4, 6, 40.0))
        self.assertEqual(self._api('/skus?limit=abc', self.user_key),
                         (400, {'ok': False, 'error': 'limit must be an integer.'}))
        status, body = self._api('/skus/%s/history' % self.sku.id, self.user_key)
        self.assertEqual([(point['total'], point['assigned'], point['available'])
                          for point in body['data']['history']], [(10, 4, 6)])
        status, body = self._api('/dashboard?days=7&tenant_ids=%s' % self.tenant.id,
                                 self.user_key)
        self.assertEqual((body['data']['kpis']['total'], body['data']['is_admin']), (15, False))
        self.assertEqual(self._api('/dashboard?tenant_ids=1,x', self.user_key),
                         (400, {'ok': False, 'error': 'tenant_ids must be an integer.'}))

    def test_user_endpoints(self):
        users = '/users?tenant_id=%s' % self.tenant.id
        status, body = self._api(users + '&licensed=1', self.user_key)
        self.assertEqual(body['data']['total'], 1)
        megan = body['data']['items'][0]
        self.assertEqual((megan['id'], megan['licenses']), (self.megan.id, [
            {'id': self.sku.id, 'sku_part_number': 'SPE_E3', 'name': 'Microsoft 365 E3'}]))
        status, body = self._api(users + '&licensed=0&search=ADELE', self.user_key)
        self.assertEqual([user['id'] for user in body['data']['items']], [self.adele.id])
        status, body = self._api(users + '&limit=1&offset=1', self.user_key)
        self.assertEqual((body['data']['total'], [user['id'] for user in body['data']['items']]),
                         (2, [self.megan.id]))
        adele = '/users/%s' % self.adele.id
        status, body = self._api(adele, self.user_key)
        self.assertEqual((body['data']['user_principal_name'], body['data']['usage_location']),
                         ('adele.vance@contoso.com', 'IN'))
        # Microsoft Graph calls are for administrators
        for path, method in ((adele + '?live=1', 'GET'), (adele + '/refresh', 'POST')):
            self.assertEqual(self._api(path, self.user_key, method),
                             (403, {'ok': False, 'error': ADMIN_ONLY}))
        with patch(GRAPH_PATH, return_value=[]):
            status, body = self._api(adele + '?live=1', self.admin_key)
        self.assertEqual(body['data']['license_details'], [])
        item = {'id': USER_ID, 'displayName': 'Adele V.', 'usageLocation': 'GB',
                'userPrincipalName': 'adele.vance@contoso.com', 'assignedLicenses': []}
        with patch(GRAPH_PATH, return_value=item):
            status, body = self._api(adele + '/refresh', self.admin_key, 'POST')
        self.assertEqual((body['data']['name'], body['data']['usage_location']), ('Adele V.', 'GB'))
        with patch(GRAPH_PATH, return_value={}) as graph:
            status, body = self._api(adele + '/usage_location', self.admin_key, 'POST',
                                     json={'country_code': 'de'})
        self.assertEqual(graph.call_args.kwargs['json_body'], {'usageLocation': 'DE'})
        self.assertEqual((status, body['data']['usage_location']), (200, 'DE'))
        status, body = self._api(adele + '/usage_location', self.admin_key, 'POST',
                                 json={'country_code': 'ZZ'})
        self.assertEqual((status, body['error']),
                         (400, 'ZZ is not a valid ISO 3166-1 alpha-2 country code.'))

    def test_tenant_administration(self):
        tenant = '/tenants/%s' % self.tenant.id
        for path, method in ((tenant + '/test', 'POST'), (tenant + '/sync', 'POST'),
                             (tenant + '/service_plans', 'GET'), ('/audit', 'GET')):
            self.assertEqual(self._api(path, self.user_key, method),
                             (403, {'ok': False, 'error': ADMIN_ONLY}), path)
        self.tenant.state = 'error'
        with patch(GRAPH_PATH, return_value=[{'displayName': 'Contoso Ltd'}]):
            status, body = self._api(tenant + '/test', self.admin_key, 'POST')
        self.assertEqual((body['data']['organization'], body['data']['tenant']['state']),
                         ('Contoso Ltd', 'connected'))
        skus = [{'skuId': GUID_A, 'skuPartNumber': 'SPE_E3', 'servicePlans': [{
            'servicePlanId': GUID_B, 'servicePlanName': 'EXCHANGE_S_ENTERPRISE',
            'appliesTo': 'User'}]}]
        with patch(GRAPH_PATH, return_value=skus):
            status, body = self._api(tenant + '/service_plans', self.admin_key)
        self.assertEqual(body['data'], [{'sku_id': GUID_A, 'sku_part_number': 'SPE_E3',
                                         'service_plans': [{'id': GUID_B,
                                                            'name': 'EXCHANGE_S_ENTERPRISE',
                                                            'applies_to': 'User'}]}])
        with patch(GRAPH_PATH, side_effect=graph_stub):
            status, body = self._api(tenant + '/sync', self.admin_key, 'POST')
        self.assertEqual(body['data']['messages'], ['Contoso: 1 subscription(s) synchronised.',
                                                    'Contoso: 0 user(s) synchronised.'])
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 401 - Unauthorized')):
            status, body = self._api(tenant + '/sync', self.admin_key, 'POST')
        self.assertEqual((status, body), (400, {'ok': False,
                                                'error': 'Contoso: HTTP 401 - Unauthorized'}))
        # The failure is kept in the audit log
        status, body = self._api('/audit?status=failed&tenant_id=%s' % self.tenant.id,
                                 self.admin_key)
        self.assertEqual([(log['action'], log['message'], log['user'])
                          for log in body['data']['items']],
                         [('sync_inventory', 'HTTP 401 - Unauthorized', self.admin.name)])

    def test_request_workflow(self):
        status, body = self._api('/requests', self.user_key, json={
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id,
            'reason': 'New joiner'})
        self.assertEqual(status, 200)
        req = body['data']
        self.assertEqual((req['type'], req['state'], req['reason'], req['requested_by'],
                          req['user'], req['sku']),
                         ('assign', 'submitted', 'New joiner', self.user.name,
                          {'id': self.adele.id, 'name': 'Adele Vance'},
                          {'id': self.sku.id, 'name': 'Microsoft 365 E3'}))
        path = '/requests/%s' % req['id']
        self.assertEqual(self._api(path + '/approve', self.user_key, 'POST'),
                         (403, {'ok': False, 'error': ADMIN_ONLY}))
        with patch(GRAPH_PATH, side_effect=graph_stub):
            status, body = self._api(path + '/approve', self.admin_key, 'POST')
        self.assertEqual((body['data']['state'], body['data']['approver']),
                         ('done', self.admin.name))
        self.assertEqual(self.adele.sku_ids, self.sku)
        # The requester follows the request
        self.assertEqual(self._api(path, self.user_key)[1]['data']['state'], 'done')
        status, body = self._api('/requests?state=done&tenant_id=%s' % self.tenant.id,
                                 self.user_key)
        self.assertEqual([item['id'] for item in body['data']['items']], [req['id']])

    def test_request_rejected(self):
        status, body = self._api('/requests', self.user_key, json={
            'tenant_id': self.tenant.id, 'user_id': self.megan.id, 'sku_id': self.sku.id,
            'type': 'remove'})
        path = '/requests/%s' % body['data']['id']
        for action in ('/reject', '/retry'):
            self.assertEqual(self._api(path + action, self.user_key, 'POST', json={}),
                             (403, {'ok': False, 'error': ADMIN_ONLY}))
        status, body = self._api(path + '/reject', self.admin_key, 'POST',
                                 json={'reason': 'Still needed'})
        self.assertEqual((body['data']['state'], body['data']['reject_reason']),
                         ('rejected', 'Still needed'))
        self.assertEqual(self.megan.sku_ids, self.sku)

    def test_invalid_requests(self):
        key = self.user_key
        valid = {'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id}
        for kwargs, error in (
                ({'data': 'not json'}, 'The request body must be valid JSON.'),
                ({'json': [valid]}, 'The request body must be a JSON object.'),
                ({'json': dict(valid, type='swap')}, "type must be 'assign' or 'remove'."),
                ({'json': dict(valid, user_id=None)}, 'user_id is required.'),
                ({'json': dict(valid, tenant_id='contoso')}, 'tenant_id must be an integer.')):
            self.assertEqual(self._api('/requests', key, 'POST', **kwargs),
                             (400, {'ok': False, 'error': error}), kwargs)
        self.assertFalse(self.env['m365.license.request'].search(
            [('tenant_id', '=', self.tenant.id)]))

    # ------------------------------------------------------------------
    # Webhook and export
    # ------------------------------------------------------------------
    def test_webhook(self):
        self.tenant.write({'webhook_enabled': True, 'webhook_subscription_id': 'sub1',
                           'webhook_client_state': 'secret-state'})
        url = '/m365/webhook/%s' % self.tenant.id
        # Subscription validation: the token is sent back as plain text
        resp = self._open(url + '?validationToken=Validation%3A%20Testing', method='POST')
        self.assertEqual((resp.status_code, resp.text), (200, 'Validation: Testing'))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain; charset=utf-8')
        self.assertFalse(self.tenant.webhook_pending)
        notification = {'subscriptionId': 'sub1', 'clientState': 'secret-state'}
        resp = self._open(url, json={'value': [notification]})
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(self.tenant.webhook_pending)
        with mute_logger('odoo.addons.m365_license_management.controllers.webhook'):
            resp = self._open(url, data='{not json', headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status_code, 400)

    def test_export_licenses(self):
        self.authenticate('m365_api_user', 'm365_api_user')
        resp = self._open('/m365/export/licenses?tenant_id=%s' % self.tenant.id)
        self.assertEqual(resp.headers['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('m365_licenses.csv', resp.headers['Content-Disposition'])
        self.assertEqual(list(csv.reader(io.StringIO(resp.text))), [
            ['Tenant', 'Product', 'SKU', 'Status', 'Total', 'Assigned', 'Available',
             'Suspended', 'Utilisation %'],
            ['Contoso', 'Microsoft 365 E3', 'SPE_E3', 'Enabled', '10', '4', '6', '0', '40.0'],
        ])
        # Internal users without access to Microsoft 365
        new_test_user(self.env, 'm365_api_internal', groups='base.group_user')
        self.env.flush_all()
        self.authenticate('m365_api_internal', 'm365_api_internal')
        self.assertEqual(self._open('/m365/export/licenses').status_code, 404)
