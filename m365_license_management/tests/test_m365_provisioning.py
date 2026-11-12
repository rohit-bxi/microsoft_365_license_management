# -*- coding: utf-8 -*-
import base64
import io
from datetime import timedelta
from unittest.mock import patch

import openpyxl

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_A, MODELS, USER_GROUP, USER_ID, FakeGraph,
                     M365Common, http_response, map_tenants)

PC_PATH = MODELS + '.m365_partner_account.M365PartnerAccount._pc_request'
PARTNER_GRAPH = MODELS + '.m365_tenant_gdap.M365PartnerAccountGraph._partner_graph_request'
RELATIONSHIPS = '/tenantRelationships/delegatedAdminRelationships'
CUSTOMER = 'cust-contoso'
CATALOG_ID = 'CFQ7TTC0LH18:0001:CFQ7TTC0LDPB'


class FakePartnerCenter:
    """Partner Center answering ``(status, data)`` from routes; records the calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, method, path, json_body=None, request_id=None, expect=()):
        self.calls.append((method, path, json_body))
        result = self.routes.get((method, path), {})
        if isinstance(result, Exception):
            raise result
        return 200, (result(json_body) if callable(result) else result)

    def paths(self, method):
        return [p for m, p, _b in self.calls if m == method]


def skus(enabled, sku_id=GUID_A):
    return [{'skuId': sku_id, 'skuPartNumber': 'SPE_E3', 'capabilityStatus': 'Enabled',
             'consumedUnits': 4, 'prepaidUnits': {'enabled': enabled}}]


class ProvisioningCommon(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_prov_admin', groups=ADMIN_GROUP)
        cls.user = map_tenants(new_test_user(cls.env, 'm365_prov_user', groups=USER_GROUP),
                               cls.tenant)
        cls.env['m365.partner.account'].search([]).active = False
        cls.account = cls.env['m365.partner.account'].create({
            'name': 'BXI CSP', 'partner_tenant_id': 'partner-tid', 'client_id': 'pc-cid',
            'client_secret': 'pc-secret', 'graph_client_id': 'shared-cid',
            'graph_client_secret': 'shared-secret', 'state': 'connected'})
        cls.tenant.write({
            'partner_account_id': cls.account.id, 'mca_accepted': True,
            'mca_first_name': 'Megan', 'mca_email': 'megan@contoso.com',
            'mca_date': fields.Date.today()})
        cls.tenant.sudo().customer_id = CUSTOMER
        cls.offer = cls.env['m365.pc.offer'].create({
            'account_id': cls.account.id, 'name': 'Microsoft 365 E3', 'sku_title': 'Microsoft 365 E3',
            'catalog_item_id': CATALOG_ID, 'term_duration': 'P1M', 'billing_cycle': 'monthly',
            'provisioning_id': GUID_A, 'min_quantity': 1, 'max_quantity': 300})


@tagged('post_install', '-at_install')
class TestM365Gdap(ProvisioningCommon):

    def test_partner_tenant_token(self):
        token = http_response(200, {'access_token': 'partner-tok', 'expires_in': 3600})
        with patch(MODELS + '.m365_tenant_gdap.requests.post', return_value=token) as post, \
                patch(MODELS + '.m365_tenant_gdap.requests.request',
                      return_value=http_response(200, {'value': []})) as call:
            self.assertEqual(self.account._partner_graph_request('GET', RELATIONSHIPS,
                                                                 paginate=True), [])
        self.assertEqual(post.call_args.args,
                         ('https://login.microsoftonline.com/partner-tid/oauth2/v2.0/token',))
        self.assertEqual(post.call_args.kwargs['data']['client_id'], 'shared-cid')
        self.assertEqual(call.call_args.args,
                         ('GET', 'https://graph.microsoft.com/v1.0' + RELATIONSHIPS))
        self.account.graph_client_secret = 'new'
        self.assertFalse(self.account.sudo().graph_access_token)

    def test_refresh_and_request(self):
        relations = [
            {'id': 'rel-old', 'status': 'terminated', 'displayName': 'Old',
             'customer': {'tenantId': 'tid-contoso'}},
            {'id': 'rel-1', 'status': 'active', 'displayName': 'Default_Contoso',
             'endDateTime': '2028-11-11T00:00:00Z',
             'customer': {'tenantId': 'TID-CONTOSO'},
             'accessDetails': {'unifiedRoles': [{'roleDefinitionId': 'a'}, {'roleDefinitionId': 'b'}]}},
        ]
        calls = []

        def graph(method, path, params=None, json_body=None, paginate=False):
            calls.append((method, path, params, json_body))
            if method == 'GET':
                return relations
            if path == RELATIONSHIPS:
                return {'id': 'rel-new', 'displayName': json_body['displayName']}
            return {}

        with patch(PARTNER_GRAPH, side_effect=graph):
            self.tenant.with_user(self.admin).action_refresh_gdap()
            self.assertEqual(calls[0][2], {'$filter': "customer/tenantId eq 'tid-contoso'"})
            self.assertEqual((self.tenant.gdap_status, self.tenant.gdap_relationship_id,
                              self.tenant.gdap_role_count), ('active', 'rel-1', 2))
            with self.assertRaisesRegex(UserError, 'already has a GDAP relationship'):
                self.tenant.with_user(self.admin).action_request_gdap()
            relations[:] = []
            self.tenant._refresh_gdap()
            self.assertEqual(self.tenant.gdap_status, 'none')
            self.tenant.with_user(self.admin).action_request_gdap()
        post, lock = [c for c in calls if c[0] == 'POST']
        body = post[3]
        self.assertEqual((body['duration'], body['autoExtendDuration'],
                          body['customer']['tenantId']), ('P730D', 'P180D', 'tid-contoso'))
        # User, License and Helpdesk Administrator, Directory Readers, Global Reader and
        # Privileged Role Administrator (Install Shared App)
        self.assertEqual(len(body['accessDetails']['unifiedRoles']), 6)
        self.assertLessEqual(len(body['displayName']), 50)
        self.assertEqual((lock[1], lock[3]), (RELATIONSHIPS + '/rel-new/requests',
                                              {'action': 'lockForApproval'}))
        self.assertEqual(self.tenant.gdap_status, 'approvalPending')
        self.assertIn('granularAdminRelationships/rel-new', self.tenant.gdap_approval_url)
        with self.assertRaises(AccessError):
            self.tenant.with_user(self.user).action_request_gdap()

    def test_request_refused(self):
        self.tenant.gdap_status = 'none'
        refused = M365GraphError('HTTP 403 - Forbidden: Partner Customer Delegated '
                                 'Administration service principal missing.')
        with patch(PARTNER_GRAPH, side_effect=refused):
            action = self.tenant.with_user(self.admin).action_request_gdap()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(self._logs('gdap').status, 'failed')


@tagged('post_install', '-at_install')
class TestM365PartnerCenterPurchase(ProvisioningCommon):

    def test_catalog_sync(self):
        stale = self.offer.copy({'catalog_item_id': 'OLD:0001:OLD'})
        fake = FakePartnerCenter({
            ('GET', '/v1/products?country=IN&targetView=OnlineServicesNCE'): {'items': [
                {'id': 'CFQ7TTC0LH18', 'title': 'Microsoft 365 Business Basic'},
                {'id': 'DZH318Z0BQ36', 'title': 'Azure Reserved VM Instances'}]},
            ('GET', '/v1/products/CFQ7TTC0LH18/skus?country=IN'): {'items': [
                {'id': '0001', 'title': 'Microsoft 365 Business Basic', 'minimumQuantity': 1,
                 'maximumQuantity': 300, 'dynamicAttributes': {
                     'provisioningId': '3B555118-DA6A-4418-894F-7DF1E2096870'}}]},
            ('GET', '/v1/products/CFQ7TTC0LH18/skus/0001/availabilities?country=IN'): {'items': [
                {'id': 'CFQ7TTC0LDPB', 'catalogItemId': 'CFQ7TTC0LH18:0001:CFQ7TTC0LDPB',
                 'terms': [{'duration': 'P1M', 'billingCycle': 'monthly'},
                           {'duration': 'P1Y', 'billingCycle': 'annual'}]}]},
        })
        with patch(PC_PATH, side_effect=fake):
            self.account.with_user(self.admin).action_sync_offers()
        offers = self.env['m365.pc.offer'].search([('account_id', '=', self.account.id),
                                                   ('name', '=', 'Microsoft 365 Business Basic')])
        self.assertEqual(sorted(offers.mapped('term_duration')), ['P1M', 'P1Y'])
        self.assertEqual(offers.mapped('provisioning_id')[0],
                         '3b555118-da6a-4418-894f-7df1e2096870')
        # Products outside the filter are not read further; offers no longer sold archived
        self.assertNotIn('/v1/products/DZH318Z0BQ36/skus?country=IN', fake.paths('GET'))
        self.assertFalse(stale.active)
        self.assertEqual((self.offer.active, self.offer.name),
                         (True, 'Microsoft 365 Business Basic'))

    def _order(self, quantity=5):
        return self.env['m365.pc.order'].with_user(self.admin).create({
            'tenant_id': self.tenant.id, 'account_id': self.account.id,
            'line_ids': [(0, 0, {'offer_id': self.offer.id, 'quantity': quantity})]})

    def test_order_lifecycle(self):
        order = self._order()
        self.assertTrue(order.name.startswith('SUB/'))
        self.tenant.mca_email = False
        with self.assertRaisesRegex(UserError, 'Microsoft Customer Agreement'):
            order.action_submit()
        self.tenant.mca_email = 'megan@contoso.com'
        order.action_submit()
        carts = '/v1/customers/%s/carts' % CUSTOMER
        fake = FakePartnerCenter({
            ('POST', carts): {'id': 'cart-1', 'lineItems': [{'id': 0}]},
            ('POST', carts + '/cart-1/checkout'): M365GraphError(
                'HTTP 500 - InternalError (1000): Try again.'),
        })
        with patch(PC_PATH, side_effect=fake):
            order.with_user(self.admin).action_approve()
        self.assertEqual((order.state, order.cart_id), ('failed', 'cart-1'))
        self.assertEqual(fake.calls[0][2], {
            'PartnerOnRecordAttestationAccepted': True,
            'lineItems': [{'id': 0, 'catalogItemId': CATALOG_ID, 'quantity': 5,
                           'billingCycle': 'monthly', 'termDuration': 'P1M'}]})
        self.assertEqual(order.line_ids.baseline_units, 10)
        # Retry: the same cart is checked out again (idempotent), no second cart
        fake.routes[('POST', carts + '/cart-1/checkout')] = {'orders': [{
            'id': 'ord-1', 'status': 'pending', 'totalPrice': 180.0, 'currencyCode': 'INR',
            'lineItems': [{'offerId': CATALOG_ID, 'quantity': 5}]}]}
        with patch(PC_PATH, side_effect=fake):
            order.with_user(self.admin).action_retry()
        self.assertEqual(fake.paths('POST').count(carts), 1)
        self.assertEqual((order.state, order.pc_order_ids, order.amount_total, order.currency),
                         ('ordered', 'ord-1', 180.0, 'INR'))
        # Completed in Partner Center, then the licenses in the tenant
        fake.routes[('GET', '/v1/customers/%s/orders/ord-1' % CUSTOMER)] = {
            'id': 'ord-1', 'status': 'completed', 'lineItems': [
                {'offerId': CATALOG_ID, 'subscriptionId': 'sub-1'}]}
        with patch(PC_PATH, side_effect=fake), \
                patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': skus(12)})):
            self.env['m365.pc.order']._cron_check_progress()
        self.assertEqual((order.state, order.line_ids.subscription_id), ('ordered', 'sub-1'))
        with patch(PC_PATH, side_effect=fake), \
                patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': skus(15)})):
            self.env['m365.pc.order']._cron_check_progress()
        self.assertEqual(order.state, 'done')
        self.assertEqual(self._logs('pc_order')[0].status, 'success')
        with self.assertRaisesRegex(UserError, 'cannot be cancelled'):
            order.action_cancel()

    def test_order_errors(self):
        order = self._order(quantity=500)
        with self.assertRaisesRegex(UserError, '1 to 300 licenses'):
            order.action_submit()
        order.line_ids.quantity = 5
        order.action_submit()
        carts = '/v1/customers/%s/carts' % CUSTOMER
        fake = FakePartnerCenter({
            ('POST', carts): {'id': 'cart-2'},
            ('POST', carts + '/cart-2/checkout'): {'orders': [], 'orderErrors': [
                {'code': '800002', 'description': 'Customer has not accepted the MCA.'}]},
        })
        with patch(PC_PATH, side_effect=fake):
            order.with_user(self.admin).action_approve()
        self.assertEqual(order.state, 'failed')
        self.assertIn('800002', order.error_message)
        with self.assertRaises(AccessError):
            order.with_user(self.user).action_retry()
        self.assertEqual(self.env['m365.pc.order'].with_user(self.user).search([]), order)

    def test_buy_licenses_channel(self):
        action = self.sku.with_user(self.admin).action_buy_licenses()
        self.assertEqual(action['res_model'], 'm365.pc.order')
        order = self.env['m365.pc.order'].with_user(self.admin).with_context(
            action['context']).create({})
        self.assertEqual((order.tenant_id, order.line_ids.offer_id), (self.tenant, self.offer))
        self.sku.consumed_units = 10
        alert, = [a for a in self.env['m365.dashboard'].with_user(self.admin)._get_alerts(
            self.tenant, self.sku) if a['kind'] == 'sku_empty']
        self.assertEqual(alert['buy_sku_id'], self.sku.id)
        self.account.purchase_channel = 'ingram'
        self.env['m365.ingram.account'].search([]).active = False
        with self.assertRaisesRegex(UserError, 'Ingram Micro account'):
            self.sku.with_user(self.admin).action_buy_licenses()


@tagged('post_install', '-at_install')
class TestM365ProvisioningFlow(ProvisioningCommon):

    def test_requested_subscriptions(self):
        self.tenant.requested_subscriptions = ('Microsoft 365 E3 = 10\n%s x 2; Unknown = 1; bad'
                                               % CATALOG_ID)
        lines, errors = self.tenant._parse_requested_subscriptions()
        self.assertEqual([(o, q) for o, q in lines], [(self.offer, 10), (self.offer, 2)])
        self.assertEqual(len(errors), 2)
        self.tenant.requested_subscriptions = 'Microsoft 365 E3 = 10'
        order = self.tenant._order_requested_subscriptions()
        self.assertEqual((order.state, order.line_ids.quantity), ('submitted', 10))

    def test_verification(self):
        request = self.env['m365.license.request'].create({
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id,
            'state': 'done', 'executed_date': fields.Datetime.now() - timedelta(minutes=5)})
        user_path = '/users/%s' % USER_ID
        details = '/users/%s/licenseDetails' % USER_ID
        graph = FakeGraph({
            user_path: {'id': USER_ID, 'accountEnabled': True},
            details: [{'skuId': GUID_A.upper(), 'skuPartNumber': 'SPE_E3', 'servicePlans': [
                {'servicePlanName': 'EXCHANGE_S_ENTERPRISE', 'provisioningStatus': 'Success'},
                {'servicePlanName': 'TEAMS1', 'provisioningStatus': 'PendingProvisioning'}]}],
        })
        with patch(GRAPH_PATH, side_effect=graph):
            self.env['m365.license.request']._cron_verify()
        self.assertEqual(request.verification_state, 'pending')
        self.assertIn('TEAMS1', request.verification_message)
        graph.routes[details][0]['servicePlans'][1]['provisioningStatus'] = 'Success'
        with patch(GRAPH_PATH, side_effect=graph):
            self.env['m365.license.request']._cron_verify()
        self.assertEqual(request.verification_state, 'verified')
        graph.routes[details] = []
        graph.routes[user_path] = {'id': USER_ID, 'accountEnabled': False}
        with patch(GRAPH_PATH, side_effect=graph):
            request.with_user(self.admin).action_verify()
        self.assertEqual(request.verification_state, 'mismatch')
        self.assertIn('disabled', request.verification_message)
        self.assertIn('not assigned', request.verification_message)
        self.assertEqual(self.tenant.prov_verification, 'failed')

    def test_steps_and_next(self):
        """Steps in order: tenant, GDAP, agreement, connection, domains, subscriptions,
        users, licenses, verification; Next Step runs the first one Odoo can run."""
        self.tenant.state = 'draft'
        tenant = self.tenant
        # Own app registration with its secret: Odoo can test the connection itself
        self.assertEqual((tenant.prov_tenant, tenant.prov_connection), ('done', 'pending'))
        # Agreement recorded on the tenant; no custom domain
        self.assertEqual((tenant.prov_agreement, tenant.prov_domains), ('done', 'na'))
        self.assertEqual(tenant.prov_state, 'progress')

        def partner_graph(method, path, params=None, json_body=None, paginate=False):
            return [] if method == 'GET' else {'id': 'rel-9', 'displayName': 'x'}

        with patch(PARTNER_GRAPH, side_effect=partner_graph):
            tenant.with_user(self.admin).action_provisioning_next()  # requests GDAP
        self.assertEqual(tenant.gdap_status, 'approvalPending')
        self.assertEqual(tenant.prov_gdap, 'action')
        # Without app registration secret nor signed-in partner user: Grant Consent
        tenant.sudo().client_secret = False
        self.assertEqual(tenant.prov_connection, 'action')
        self.env['ir.config_parameter'].set_param('web.base.url', 'https://odoo.example.com')
        tenant.gdap_status = 'active'
        self.assertEqual(tenant.prov_gdap, 'done')
        action = tenant.with_user(self.admin).action_provisioning_next()
        self.assertEqual(action['type'], 'ir.actions.act_url')  # Grant Consent
        tenant.write({'state': 'connected', 'connection_mode': 'shared_app'})
        tenant.state = 'connected'  # a new connection mode sets it back to Not Tested
        tenant.requested_subscriptions = 'Microsoft 365 E3 = 3'
        self.assertEqual(tenant.prov_agreement, 'done')  # recorded on the tenant
        tenant.with_user(self.admin).action_provisioning_next()
        self.assertEqual(tenant.pc_order_ids.state, 'submitted')
        self.assertEqual(tenant.prov_subscriptions, 'action')
        self.assertTrue(tenant.prov_summary.startswith('Subscriptions'))

    def test_import_links_customers(self):
        header = ('CompanyName,Domain,FirstName,LastName,Email,AddressLine1,City,PostalCode,'
                  'Country,OrganizationRegistrationNumber,Subscriptions\n')
        row = ('Northwind Provision,northwindprov,Nancy,Davolio,nancy@northwind.example,'
               '1 MG Road,Bengaluru,560001,IN,U72900KA2020PTC000001,Microsoft 365 E3 = 25\n')
        wizard = self.env['m365.tenant.import.wizard'].create({
            'tenant_type': 'new', 'partner_account_id': self.account.id,
            'file': base64.b64encode((header + row).encode()), 'filename': 'Customers.csv'})
        wizard.action_validate()
        self.assertEqual(wizard.line_ids.status, 'valid', wizard.line_ids.message)
        wizard.action_import()
        tenant = self.env['m365.tenant'].search([('name', '=', 'Northwind Provision')])
        self.assertEqual(tenant.requested_subscriptions, 'Microsoft 365 E3 = 25')
        customer = tenant.tenant_customer_id
        self.assertEqual((customer.name, customer.city, customer.child_ids.name),
                         ('Northwind Provision', 'Bengaluru', 'Nancy Davolio'))


@tagged('post_install', '-at_install')
class TestM365ProvisioningReport(HttpCase, ProvisioningCommon):

    def test_report(self):
        self.authenticate(self.user.login, self.user.login)
        self.env.flush_all()
        resp = self.url_open('/m365/export/provisioning')
        self.assertEqual(resp.status_code, 200)
        book = openpyxl.load_workbook(io.BytesIO(resp.content))
        rows = list(book.active.iter_rows(values_only=True))
        self.assertEqual(rows[0][:3], ('Customer', 'Tenant', 'Domain'))
        self.assertEqual([r[1] for r in rows[1:]], ['Contoso'])  # the user's tenants only
