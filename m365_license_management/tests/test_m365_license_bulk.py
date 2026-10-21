# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError

GUID_A = '11111111-2222-3333-4444-555555555555'
TENANT_PATH = 'odoo.addons.m365_license_management.models.m365_tenant.M365Tenant._graph_request'


@tagged('post_install', '-at_install')
class TestM365LicenseBulk(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env['m365.tenant'].create({
            'name': 'Bulk', 'domain': 'bulk.com', 'tenant_id': 'tid-bulk',
            'client_id': 'cid', 'client_secret': 'secret', 'state': 'connected',
            'allow_license_operations': True, 'auto_approve_admins': True,
        })
        cls.sku = cls.env['m365.sku'].create({
            'tenant_id': cls.tenant.id, 'sku_id': GUID_A, 'sku_part_number': 'E3',
            'enabled_units': 2, 'consumed_units': 0,
        })
        cls.users = cls.env['m365.license.user'].create([{
            'name': 'User %s' % i, 'tenant_id': cls.tenant.id, 'graph_id': 'graph-%s' % i,
        } for i in range(4)])

    def _wizard(self, request_type, users=None, env=None):
        return (env or self.env)['m365.license.bulk.wizard'].create({
            'tenant_id': self.tenant.id,
            'request_type': request_type,
            'user_ids': (users or self.users).ids,
            'sku_ids': self.sku.ids,
        })

    def _results(self, wizard):
        return {line.user_id: (line.status, line.request_id.state) for line in wizard.line_ids}

    def _graph(self, method, path, params=None, json_body=None, paginate=False):
        if path == '/subscribedSkus':
            return [{'skuId': GUID_A, 'skuPartNumber': 'E3', 'capabilityStatus': 'Enabled',
                     'consumedUnits': 2, 'prepaidUnits': {'enabled': 2}}]
        return {}

    def test_bulk_assign(self):
        first, second, third, fourth = self.users
        first.sku_ids = self.sku
        wizard = self._wizard('assign')
        with patch(TENANT_PATH, side_effect=self._graph) as mocked:
            wizard.action_execute()
        self.assertEqual(self._results(wizard), {
            first: ('skipped', False),
            second: ('done', 'done'),
            third: ('done', 'done'),
            fourth: ('failed', False),   # only 2 licenses were available
        })
        self.assertEqual((wizard.done_count, wizard.skipped_count, wizard.failed_count),
                         (2, 1, 1))
        self.assertEqual((second | third).sku_ids, self.sku)
        self.assertFalse(fourth.sku_ids)
        paths = [call.args[1] for call in mocked.call_args_list]
        self.assertEqual(paths, ['/users/graph-1/assignLicense', '/users/graph-2/assignLicense',
                                 '/subscribedSkus'])
        self.assertEqual(self.sku.available_units, 0)

    def test_bulk_remove_graph_failure(self):
        first, second = self.users[:2]
        (first | second).sku_ids = self.sku

        def graph(method, path, params=None, json_body=None, paginate=False):
            if path == '/users/graph-1/assignLicense':
                raise M365GraphError('Request_ResourceNotFound')
            return self._graph(method, path)

        wizard = self._wizard('remove')
        with patch(TENANT_PATH, side_effect=graph):
            wizard.action_execute()
        self.assertEqual(self._results(wizard), {
            first: ('done', 'done'),
            second: ('failed', 'failed'),
            self.users[2]: ('skipped', False),
            self.users[3]: ('skipped', False),
        })
        failed = wizard.line_ids.filtered(lambda line: line.status == 'failed')
        self.assertEqual(failed.message, 'Request_ResourceNotFound')
        self.assertFalse(first.sku_ids)
        self.assertEqual(second.sku_ids, self.sku)

    def test_bulk_validation_errors_leave_no_request(self):
        self.tenant.state = 'draft'
        wizard = self._wizard('assign', users=self.users[:2])
        with patch(TENANT_PATH, side_effect=self._graph) as mocked:
            wizard.action_execute()
        self.assertEqual(wizard.line_ids.mapped('status'), ['failed', 'failed'])
        self.assertIn('Test the connection', wizard.line_ids[0].message)
        self.assertFalse(mocked.called)
        self.assertFalse(self.env['m365.license.request'].search(
            [('tenant_id', '=', self.tenant.id)]))

    def test_bulk_by_user_waits_for_approval(self):
        user = new_test_user(self.env, 'm365_bulk_user',
                             groups='m365_license_management.group_m365_user')
        wizard = self._wizard('assign', users=self.users[:2], env=self.env(user=user))
        with patch(TENANT_PATH, side_effect=self._graph) as mocked:
            wizard.action_execute()
        self.assertEqual(wizard.submitted_count, 2)
        self.assertEqual(set(wizard.line_ids.mapped('request_id.state')), {'submitted'})
        self.assertFalse(mocked.called)

    def test_bulk_requires_users_and_licenses(self):
        wizard = self._wizard('assign')
        wizard.sku_ids = False
        with self.assertRaises(UserError):
            wizard.action_execute()
