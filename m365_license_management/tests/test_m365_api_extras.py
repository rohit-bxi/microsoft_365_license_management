# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from .common import (ADMIN_GROUP, GRAPH_PATH, USER_GROUP, FakeGraph, M365Common,
                     map_tenants)
from .test_m365_ingram import IM_PATH, ORDERS, FakeIngram

API = '/m365/api/v1'


@tagged('post_install', '-at_install')
class TestM365ApiExtras(HttpCase, M365Common):
    """REST API of the consent, configuration, domains and license orders."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_api_x_admin', groups=ADMIN_GROUP)
        cls.user = map_tenants(new_test_user(cls.env, 'm365_api_x_user', groups=USER_GROUP),
                               cls.tenant)
        Keys = cls.env['res.users.apikeys']
        cls.admin_key = Keys.with_user(cls.admin).sudo()._generate(None, 'Tests', None)
        cls.user_key = Keys.with_user(cls.user).sudo()._generate(None, 'Tests', None)
        cls.env['ir.config_parameter'].set_param('web.base.url', 'https://odoo.example.com')
        account = cls.env['m365.partner.account'].create({
            'name': 'BXI CSP', 'partner_tenant_id': 'partner-tid', 'client_id': 'pc-cid',
            'graph_client_id': 'shared-cid', 'graph_client_secret': 'shared-secret'})
        cls.tenant.write({
            'partner_account_id': account.id,
            'config_profile_id': cls.env['m365.config.profile'].create({'name': 'API'}).id,
            'ingram_relationship': True, 'mca_accepted': True, 'mca_first_name': 'Megan',
            'mca_email': 'megan@contoso.com', 'mca_date': fields.Date.today(),
        })
        cls.env['m365.ingram.account'].search([]).active = False
        cls.ingram = cls.env['m365.ingram.account'].create({
            'name': 'Ingram', 'client_id': 'im', 'client_secret': 'im',
            'customer_number': '20-222222', 'country_code': 'IN'})
        cls.plan = cls.env['m365.ingram.plan'].create({
            'account_id': cls.ingram.id, 'plan_id': '438292', 'name': 'Microsoft 365 E3',
            'product_id': cls.sku.product_id.id, 'ingram_part_number': 'MS-E3-M',
            'unit_price': 36.0})

    def _api(self, path, key, method='GET', **kwargs):
        self.env.flush_all()
        resp = self.url_open(API + path, headers={'Authorization': 'Bearer %s' % key},
                             method=method, **kwargs)
        self.env.invalidate_all()
        return resp.status_code, resp.json()

    def test_consent_and_configuration(self):
        path = '/tenants/%s' % self.tenant.id
        self.assertEqual(self._api(path + '/consent', self.user_key, 'POST')[0], 403)
        status, body = self._api(path + '/consent', self.admin_key, 'POST')
        self.assertEqual(status, 200, body)
        self.assertTrue(body['data']['url'].startswith(
            'https://login.microsoftonline.com/tid-contoso/v2.0/adminconsent?'))
        with patch(GRAPH_PATH, side_effect=FakeGraph()):
            status, body = self._api(path + '/configuration', self.admin_key, 'POST')
        self.assertEqual(status, 200, body)
        self.assertEqual((body['data']['ok'], body['data']['config_state']), (True, 'applied'))

    def test_domains(self):
        path = '/tenants/%s/domains' % self.tenant.id
        graph = FakeGraph({
            ('POST', '/domains'): lambda body: {'id': body['id'], 'isVerified': False},
            '/domains/fabrikam.com/verificationDnsRecords': [
                {'recordType': 'Txt', 'label': 'fabrikam.com', 'text': 'MS=ms1', 'ttl': 3600}],
            ('POST', '/domains/fabrikam.com/verify'): M365GraphError(
                'HTTP 400 - Request_BadRequest: Domain verification failed.'),
        })
        self.assertEqual(self._api(path, self.user_key, 'POST', json={'name': 'x.com'})[0], 403)
        with patch(GRAPH_PATH, side_effect=graph):
            status, body = self._api(path, self.admin_key, 'POST', json={'name': 'fabrikam.com'})
            self.assertEqual(status, 200, body)
            domain = body['data']
            self.assertIn('MS=ms1', domain['verification_records'])
            status, body = self._api('/domains/%s/verify' % domain['id'], self.admin_key,
                                     'POST')
        self.assertEqual(status, 200, body)
        self.assertFalse(body['data']['is_verified'])
        self.assertIn('could not verify', body['data']['error'])
        status, body = self._api(path, self.user_key)
        self.assertEqual([d['name'] for d in body['data']], ['fabrikam.com'])
        self.assertEqual(self._api(path, self.admin_key, 'POST', json={'name': 'bad name'})[0],
                         400)

    def test_orders(self):
        order_body = {'tenant_id': self.tenant.id, 'reason': 'New joiners',
                      'lines': [{'plan_id': '438292', 'quantity': 5}]}
        self.assertEqual(self._api('/orders', self.user_key, 'POST', json=order_body)[0], 403)
        status, body = self._api('/orders', self.admin_key, 'POST', json=order_body)
        self.assertEqual(status, 200, body)
        order = body['data']
        self.assertEqual((order['state'], order['lines'][0]['quantity']), ('submitted', 5))
        fake = FakeIngram({('POST', ORDERS): {'confirmationNumber': 'CNF-9'}})
        with patch(IM_PATH, side_effect=fake):
            status, body = self._api('/orders/%s/approve' % order['id'], self.admin_key, 'POST')
        self.assertEqual((status, body['data']['state'], body['data']['confirmation_number']),
                         (200, 'sent', 'CNF-9'))
        # Users read the orders of their tenants
        status, body = self._api('/orders?tenant_id=%s' % self.tenant.id, self.user_key)
        self.assertEqual((status, body['data']['total']), (200, 1))
        # Not cancellable at Ingram Micro before its order number is known
        status, body = self._api('/orders/%s/cancel' % order['id'], self.admin_key, 'POST')
        self.assertEqual(status, 400)
        self.assertIn('has not given the order number', body['error'])
        self.assertEqual(self._api('/orders', self.admin_key, 'POST', json=dict(
            order_body, lines=[{'plan_id': 'nope', 'quantity': 1}]))[0], 400)
