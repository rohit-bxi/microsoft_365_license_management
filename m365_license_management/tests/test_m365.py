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
            'client_id': 'cid', 'client_secret': 'secret', 'state': 'connected',
            'allow_license_operations': True, 'auto_approve_admins': False,
            # Full user reads (GET /users); delta queries are tested in test_m365_live
            'use_delta_sync': False,
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

    def test_test_connection_from_list(self):
        other = self.env['m365.tenant'].create({
            'name': 'Other', 'domain': 'other.com', 'tenant_id': 'tid2', 'client_id': 'cid2',
            'client_secret': 'secret'})
        tenants = self.tenant | other

        def graph(tenant, method, path, params=None, json_body=None, paginate=False):
            if tenant == other:
                raise M365GraphError('AADSTS700016')
            return [{'displayName': 'Test Org'}] if path == '/organization' else []

        server_action = self.env.ref(
            'm365_license_management.action_m365_tenant_test_connection')
        with patch(TENANT_PATH, autospec=True, side_effect=graph):
            action = server_action.with_context(
                active_model='m365.tenant', active_ids=tenants.ids).run()
        self.assertEqual((self.tenant.state, other.state), ('connected', 'error'))
        self.assertEqual(other.last_error, 'AADSTS700016')
        params = action['params']
        self.assertEqual(params['type'], 'danger')
        self.assertIn('Connected: Test.', params['message'])
        self.assertIn('Failed: Other.', params['message'])

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
