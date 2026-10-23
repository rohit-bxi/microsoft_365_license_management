# -*- coding: utf-8 -*-
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from odoo.tools import mute_logger

from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_B, USER_GROUP, M365Common, graph_stub,
                     map_tenants)

# Models whose records belong to a tenant
TENANT_MODELS = ('m365.sku', 'm365.license.user', 'm365.license.request', 'm365.audit.log',
                 'm365.inventory.snapshot')


@tagged('post_install', '-at_install')
class TestM365TenantAccess(M365Common):
    """Contoso and Fabrikam, each with a subscription, a user, a license request of the
    test user, an audit log entry and an inventory snapshot."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, 'm365_access_user', groups=USER_GROUP)
        cls.admin = new_test_user(cls.env, 'm365_access_admin', groups=ADMIN_GROUP)
        cls.fabrikam = cls.env['m365.tenant'].create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'client_id': 'cid', 'client_secret': 'secret', 'state': 'connected',
        })
        cls.fabrikam_sku = cls.env['m365.sku'].create({
            'tenant_id': cls.fabrikam.id, 'sku_id': GUID_B, 'sku_part_number': 'SPE_E5',
            'enabled_units': 5,
        })
        cls.lee = cls.env['m365.license.user'].create({
            'name': 'Lee Gu', 'tenant_id': cls.fabrikam.id, 'graph_id': 'graph-lee',
        })
        cls.tenants = cls.tenant | cls.fabrikam
        for tenant, sku, user in ((cls.tenant, cls.sku, cls.adele),
                                  (cls.fabrikam, cls.fabrikam_sku, cls.lee)):
            cls.env['m365.license.request'].create({
                'tenant_id': tenant.id, 'user_id': user.id, 'sku_id': sku.id,
                'requested_by': cls.user.id,
            })
            tenant._log('sync_inventory', 'success', 'Synchronised.')
            sku._record_snapshot()

    def _visible(self, user):
        """Names of the tenants whose records the user sees, per model."""
        env = self.env(user=user)
        visible = {'m365.tenant': sorted(env['m365.tenant'].search(
            [('id', 'in', self.tenants.ids)]).sudo().mapped('name'))}
        for model in TENANT_MODELS:
            records = env[model].search([('tenant_id', 'in', self.tenants.ids)])
            visible[model] = sorted(records.sudo().tenant_id.mapped('name'))
        return visible

    @staticmethod
    def _expected(*names):
        return dict.fromkeys(('m365.tenant',) + TENANT_MODELS, sorted(names))

    def test_user_without_mapping_sees_nothing(self):
        self.assertEqual(self._visible(self.user), self._expected())

    def test_user_sees_mapped_tenants(self):
        mapping = self.env['m365.user.tenant.mapping'].create({
            'user_id': self.user.id, 'tenant_ids': [Command.link(self.tenant.id)]})
        self.assertEqual(mapping.display_name, self.user.name)
        self.assertEqual(self.tenant.user_mapping_ids, mapping)
        self.assertEqual(self._visible(self.user), self._expected('Contoso'))
        # A change of the mapping applies at once
        mapping.tenant_ids = [Command.link(self.fabrikam.id)]
        self.assertEqual(mapping.tenant_count, 2)
        self.assertEqual(self._visible(self.user), self._expected('Contoso', 'Fabrikam'))
        mapping.unlink()
        self.assertEqual(self._visible(self.user), self._expected())

    def test_administrator_sees_every_tenant(self):
        self.assertEqual(self._visible(self.admin), self._expected('Contoso', 'Fabrikam'))

    def test_dashboard_of_mapped_administrator(self):
        env = self.env(user=self.admin)

        def dashboard_tenants():
            data = env['m365.dashboard'].get_dashboard_data()
            return {tenant['name'] for tenant in data['tenants']}

        # Without mapping: every tenant
        self.assertLessEqual({'Contoso', 'Fabrikam'}, dashboard_tenants())
        map_tenants(self.admin, self.tenant)
        self.assertEqual(dashboard_tenants(), {'Contoso'})
        # The administrator still manages every tenant outside the dashboard
        self.assertEqual(self._visible(self.admin), self._expected('Contoso', 'Fabrikam'))
        # Sync now synchronises the tenants of the dashboard only
        with patch(GRAPH_PATH, side_effect=graph_stub):
            result = env['m365.tenant'].sync_from_dashboard()
        self.assertTrue(result['ok'])
        self.assertEqual({message.split(':')[0] for message in result['messages']},
                         {'Contoso'})

    def test_user_sees_own_requests_only(self):
        map_tenants(self.user, self.tenant)
        other = self.env['m365.license.request'].create({
            'tenant_id': self.tenant.id, 'user_id': self.megan.id, 'sku_id': self.sku.id,
            'request_type': 'remove'})
        requests = self.env['m365.license.request'].with_user(self.user).search(
            [('tenant_id', '=', self.tenant.id)])
        self.assertEqual(requests.requested_by, self.user)
        self.assertNotIn(other, requests)

    def test_user_cannot_use_unmapped_tenant(self):
        map_tenants(self.user, self.tenant)
        with self.assertRaises(AccessError):
            self.fabrikam.with_user(self.user).read(['name'])
        with self.assertRaises(AccessError):
            self.env['m365.license.request'].with_user(self.user).create({
                'tenant_id': self.fabrikam.id, 'user_id': self.lee.id,
                'sku_id': self.fabrikam_sku.id})
        # The dashboard shows the mapped tenants only
        data = self.env(user=self.user)['m365.dashboard'].get_dashboard_data()
        self.assertEqual([tenant['name'] for tenant in data['tenants']], ['Contoso'])

    def test_multi_company(self):
        self.fabrikam.company_id = self.env['res.company'].create({'name': 'Fabrikam Ltd'})
        map_tenants(self.user, self.tenants)
        # Neither user is in the company of Fabrikam
        self.assertEqual(self._visible(self.user), self._expected('Contoso'))
        self.assertEqual(self._visible(self.admin), self._expected('Contoso'))

    def test_one_mapping_per_user(self):
        map_tenants(self.user, self.tenant)
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            map_tenants(self.user, self.fabrikam)

    def test_only_microsoft_365_users_can_be_mapped(self):
        Mapping = self.env['m365.user.tenant.mapping']
        internal = new_test_user(self.env, 'm365_access_internal', groups='base.group_user')
        candidates = self.env['res.users'].search(
            Mapping._fields['user_id'].get_comodel_domain(Mapping))
        self.assertIn(self.user, candidates)
        self.assertIn(self.admin, candidates)
        self.assertNotIn(internal, candidates)
