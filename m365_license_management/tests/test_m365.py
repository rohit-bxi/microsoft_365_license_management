# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.m365_tenant import M365GraphError

GUID_A = '11111111-2222-3333-4444-555555555555'
GUID_B = '66666666-7777-8888-9999-000000000000'
USER_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
TENANT_PATH = 'odoo.addons.m365_license_management.models.m365_tenant.M365Tenant._graph_request'


@tagged('post_install', '-at_install')
class TestM365(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env['m365.tenant'].create({
            'name': 'Test', 'domain': 'test.com', 'tenant_id': 'tid',
            'client_id': 'cid', 'client_secret': 'secret',
            'allow_license_operations': True, 'state': 'connected',
        })

    def _graph(self, method, path, params=None, json_body=None, paginate=False):
        if path == '/subscribedSkus':
            return [{'skuId': GUID_A, 'skuPartNumber': 'O365_BUSINESS_PREMIUM',
                     'capabilityStatus': 'Enabled', 'consumedUnits': 3,
                     'prepaidUnits': {'enabled': 10, 'suspended': 0, 'warning': 0},
                     'appliesTo': 'User'}]
        if path == '/users':
            return [{'id': USER_ID, 'displayName': 'Jane', 'userPrincipalName': 'j@test.com',
                     'usageLocation': 'IN', 'assignedLicenses': [{'skuId': GUID_A}]}]
        return {}

    def test_sync_inventory_and_users(self):
        with patch(TENANT_PATH, side_effect=self._graph):
            ok, _msgs = self.tenant._sync_all_safe()
        self.assertTrue(ok)
        sku = self.tenant.sku_ids
        self.assertEqual((sku.enabled_units, sku.consumed_units, sku.available_units),
                         (10, 3, 7))
        self.assertEqual(self.tenant.user_ids.sku_ids, sku)
        self.assertEqual(self.tenant.failure_count, 0)

    def test_failure_counter(self):
        with patch(TENANT_PATH, side_effect=M365GraphError('boom')):
            for _i in range(3):
                ok, _msgs = self.tenant._sync_all_safe()
                self.assertFalse(ok)
        self.assertEqual(self.tenant.failure_count, 3)
        self.assertEqual(self.tenant.state, 'error')

    def test_assign_request_flow(self):
        with patch(TENANT_PATH, side_effect=self._graph):
            self.tenant._sync_all_safe()
        user, sku = self.tenant.user_ids, self.tenant.sku_ids
        user.sku_ids = False
        req = self.env['m365.license.request'].create({
            'tenant_id': self.tenant.id, 'user_id': user.id, 'sku_id': sku.id})
        req.action_submit()
        self.assertEqual(req.state, 'submitted')
        with patch(TENANT_PATH, side_effect=self._graph) as mocked:
            req.action_approve()
        self.assertEqual(req.state, 'done')
        post = [c for c in mocked.call_args_list if c.args[0] == 'POST'][0]
        self.assertEqual(post.args[1], '/users/%s/assignLicense' % USER_ID)
        self.assertEqual(post.kwargs['json_body']['addLicenses'][0]['skuId'], GUID_A)

    def test_operations_disabled(self):
        self.tenant.allow_license_operations = False
        with self.assertRaises(M365GraphError):
            self.tenant.graph_assign_license(USER_ID, add_sku_ids=[GUID_B])

    def test_invalid_sku_rejected(self):
        with self.assertRaises(M365GraphError):
            self.tenant.graph_assign_license(USER_ID, add_sku_ids=['not-a-guid'])

    def test_usage_location_validation(self):
        user = self.env['m365.license.user'].create({
            'name': 'U', 'tenant_id': self.tenant.id, 'graph_id': USER_ID})
        with self.assertRaises(Exception):
            user.set_usage_location('ZZ')
        with patch(TENANT_PATH, return_value={}):
            user.set_usage_location('in')
        self.assertEqual(user.usage_location, 'IN')


@tagged('post_install', '-at_install')
class TestM365Live(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env['m365.tenant'].create({
            'name': 'Live', 'domain': 'live.com', 'tenant_id': 'tid2',
            'client_id': 'cid', 'client_secret': 's', 'state': 'connected'})
        cls.sku = cls.env['m365.sku'].create({
            'tenant_id': cls.tenant.id, 'sku_id': GUID_A, 'sku_part_number': 'X'})

    def test_delta_initial_then_incremental_and_removed(self):
        calls = []

        def graph(method, path, params=None, **kw):
            calls.append(path)
            if path == '/users/delta':
                return {'value': [{'id': USER_ID, 'displayName': 'Jane',
                                   'userPrincipalName': 'j@live.com', 'accountEnabled': True,
                                   'assignedLicenses': [{'skuId': GUID_A}]}],
                        '@odata.deltaLink': 'https://graph/delta?token=1'}
            if 'token=1' in path:
                return {'value': [{'id': USER_ID, 'assignedLicenses': []}],
                        '@odata.deltaLink': 'https://graph/delta?token=2'}
            return {'value': [{'id': USER_ID, '@removed': {'reason': 'deleted'}}],
                    '@odata.deltaLink': 'https://graph/delta?token=3'}

        with patch(TENANT_PATH, side_effect=graph):
            self.tenant._sync_users_delta()
            user = self.tenant.user_ids
            self.assertEqual(user.sku_ids, self.sku)
            self.tenant._sync_users_delta()          # partial item: licenses removed only
            self.assertFalse(user.sku_ids)
            self.assertEqual(user.name, 'Jane')
            self.tenant._sync_users_delta()          # @removed -> archived
        self.assertFalse(user.active)
        self.assertIn('token=2', calls[2])

    def test_webhook_client_state(self):
        self.tenant.write({'webhook_enabled': True, 'webhook_subscription_id': 'sub1',
                           'webhook_client_state': 'secret-state'})
        Tenant = self.env['m365.tenant']
        bad = [{'subscriptionId': 'sub1', 'clientState': 'nope'}]
        good = [{'subscriptionId': 'sub1', 'clientState': 'secret-state'}]
        self.assertEqual(Tenant._webhook_receive(self.tenant.id, bad), 0)
        self.assertFalse(self.tenant.webhook_pending)
        self.assertEqual(Tenant._webhook_receive(self.tenant.id, good), 1)
        self.assertTrue(self.tenant.webhook_pending)

    def test_subscribe_requires_https(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://localhost')
        with self.assertRaises(Exception):
            self.tenant.action_webhook_subscribe()
