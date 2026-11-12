# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, new_test_user, tagged

from ..models.m365_ingram_account import M365IngramError, M365IngramTimeout
from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_A, MODELS, USER_GROUP, FakeGraph,
                     M365Common, http_response, map_tenants)

ACCOUNT = MODELS + '.m365_ingram_account'
IM_PATH = ACCOUNT + '.M365IngramAccount._im_request'
TOKEN_GET = ACCOUNT + '.requests.get'
HTTP_CALL = ACCOUNT + '.requests.request'
SLEEP = ACCOUNT + '.time.sleep'
ORDERS = '/resellers/v7/orders'
SEARCH = '/resellers/v6/orders/search'


class FakeIngram:
    """Ingram Micro API answering ``(200, data)`` from routes; records the calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, method, path, params=None, json_body=None, expect=()):
        self.calls.append((method, path, params, json_body))
        result = self.routes.get((method, path), {})
        if isinstance(result, Exception):
            raise result
        return 200, (result(json_body) if callable(result) else result)

    def bodies(self, method, path):
        return [body for m, p, _params, body in self.calls if (m, p) == (method, path)]


def signature(secret, event_id):
    return base64.b64encode(hmac.new(secret.encode(), event_id.encode(),
                                     hashlib.sha512).digest()).decode()


def skus(enabled):
    return [{'skuId': GUID_A, 'skuPartNumber': 'SPE_E3', 'capabilityStatus': 'Enabled',
             'consumedUnits': 4, 'prepaidUnits': {'enabled': enabled}}]


class IngramCommon(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_ingram_admin', groups=ADMIN_GROUP)
        cls.user = map_tenants(new_test_user(cls.env, 'm365_ingram_user', groups=USER_GROUP),
                               cls.tenant)
        cls.env['m365.ingram.account'].search([]).active = False
        # Buy Licenses goes to Ingram Micro without a Partner Center account
        cls.env['m365.partner.account'].search([]).active = False
        cls.account = cls.env['m365.ingram.account'].create({
            'name': 'Ingram Micro India', 'client_id': 'im-cid', 'client_secret': 'im-secret',
            'customer_number': '20-222222', 'country_code': 'IN', 'webhook_secret': 'whsec',
        })
        cls.plan = cls.env['m365.ingram.plan'].create({
            'account_id': cls.account.id, 'plan_id': '438292',
            'name': 'Microsoft 365 E3 (NCE COM MTH)', 'product_id': cls.sku.product_id.id,
            'ingram_part_number': 'MS-E3-M', 'unit_price': 36.0,
        })
        customer = cls.tenant.tenant_customer_id
        customer.write({'name': 'Contoso Ltd', 'email': 'it@contoso.com', 'street': '1 MG Road',
                        'city': 'Bengaluru', 'zip': '560001', 'phone': '+91 80 1234 5678',
                        'country_id': cls.env.ref('base.in').id})
        cls.env['customer.customer'].create({'name': 'Megan Bowen', 'parent_id': customer.id,
                                             'email': 'megan@contoso.com'})
        cls.tenant.write({
            'ingram_relationship': True, 'mca_accepted': True, 'mca_first_name': 'Megan',
            'mca_last_name': 'Bowen', 'mca_email': 'megan@contoso.com',
            'mca_date': fields.Date.to_date('2026-11-01'),
        })

    def _order(self, quantity=5, **vals):
        return self.env['m365.ingram.order'].with_user(self.admin).create(dict({
            'tenant_id': self.tenant.id, 'reason': 'New joiners',
            'line_ids': [(0, 0, {'plan_id': self.plan.id, 'quantity': quantity,
                                 'unit_price': 36.0})],
        }, **vals))

    def _submitted(self, **vals):
        order = self._order(**vals)
        order.action_submit()
        return order


@tagged('post_install', '-at_install')
class TestM365Ingram(IngramCommon):

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------
    def test_client(self):
        token = http_response(200, {'access_token': 'im-tok', 'token_type': 'Bearer',
                                    'expires_in': '86399'})
        with patch(TOKEN_GET, return_value=token) as get, \
                patch(HTTP_CALL, return_value=http_response(200, {'recordsFound': 1180})) as call:
            action = self.account.with_user(self.admin).action_test_connection()
        self.assertEqual((action['params']['type'], self.account.state), ('success', 'connected'))
        self.assertEqual(get.call_args.args, ('https://api.ingrammicro.com:443/oauth/oauth20/token',))
        self.assertEqual(get.call_args.kwargs['params'], {
            'grant_type': 'client_credentials', 'client_id': 'im-cid', 'client_secret': 'im-secret'})
        self.assertAlmostEqual(self.account.sudo().token_expiry,
                               fields.Datetime.now() + timedelta(seconds=86399),
                               delta=timedelta(minutes=1))
        self.assertEqual(call.call_args.args,
                         ('GET', 'https://api.ingrammicro.com:443/resellers/v6/catalog'))
        headers = call.call_args.kwargs['headers']
        correlation = headers.pop('IM-CorrelationID')
        self.assertEqual(len(correlation), 32)
        self.assertEqual(headers, {
            'Authorization': 'Bearer im-tok', 'Accept': 'application/json',
            'Content-Type': 'application/json', 'IM-CustomerNumber': '20-222222',
            'IM-CountryCode': 'IN', 'IM-SenderID': 'Odoo Microsoft 365',
            'IM-ApplicationID': 'Odoo Microsoft 365'})
        self.assertEqual(call.call_args.kwargs['params']['type'], 'IM::subscription')

        bad = http_response(400, {'errors': [{
            'type': 'errors/bad-request', 'message': 'Input passed is not in right format.',
            'fields': [{'field': 'IM-CountryCode', 'message': 'The countryCode field is required.'}],
        }]})
        with patch(HTTP_CALL, return_value=bad), self.assertRaisesRegex(
                M365IngramError, r'HTTP 400: Input passed is not in right format. '
                                 r'\(IM-CountryCode: The countryCode field is required.\)'):
            self.account._im_request('GET', '/resellers/v6/catalog')
        with patch(HTTP_CALL, side_effect=requests.ConnectionError('down')), \
                self.assertRaises(M365IngramTimeout):
            self.account._im_request('POST', ORDERS)
        # Expired token renewed once; throttling retried
        with patch(TOKEN_GET, return_value=token) as get, patch(HTTP_CALL, side_effect=[
                http_response(401, {}), http_response(429, {}, {'Retry-After': '1'}),
                http_response(200, {'ok': 1})]), patch(SLEEP) as sleep:
            self.assertEqual(self.account._im_request('GET', '/x'), (200, {'ok': 1}))
        get.assert_called_once()
        sleep.assert_called_once_with(1)
        # Valid credentials, but no API product for the path (Apigee): explained, and no
        # new token requested since it would not help
        no_product = http_response(401, {'fault': {
            'faultstring': 'Invalid API call as no apiproduct match found',
            'detail': {'errorcode': 'keymanagement.service.'
                                    'InvalidAPICallAsNoApiProductMatchFound'}}})
        no_product.url = 'https://api.ingrammicro.com:443/resellers/v6/catalog?pageSize=1'
        self.account.sudo().write({'access_token': 'im-tok',
                                   'token_expiry': fields.Datetime.now() + timedelta(hours=1)})
        with patch(TOKEN_GET) as get, patch(HTTP_CALL, return_value=no_product) as call:
            action = self.account.with_user(self.admin).action_test_connection()
        get.assert_not_called()
        call.assert_called_once()
        self.assertEqual((action['params']['type'], self.account.state), ('danger', 'error'))
        self.assertIn('has no API product for /resellers/v6/catalog', self.account.last_error)
        self.assertIn('/sandbox', self.account.last_error)
        # Sandbox app: API calls under /sandbox, token at the root of the host
        self.account.api_url = 'https://api.ingrammicro.com:443/sandbox/'
        with patch(TOKEN_GET, return_value=token) as get, \
                patch(HTTP_CALL, return_value=http_response(200, {'recordsFound': 3})) as call:
            self.account.with_user(self.admin).action_test_connection()
        self.assertEqual(get.call_args.args,
                         ('https://api.ingrammicro.com:443/oauth/oauth20/token',))
        self.assertEqual(call.call_args.args,
                         ('GET', 'https://api.ingrammicro.com:443/sandbox/resellers/v6/catalog'))
        self.assertEqual(self.account.state, 'connected')
        # Credentials changed by an administrator: token forgotten
        self.account.with_user(self.admin).client_secret = 'new'
        self.assertFalse(self.account.sudo().access_token)
        self.assertEqual(self.account.state, 'draft')

    # ------------------------------------------------------------------
    # Catalog and plans
    # ------------------------------------------------------------------
    def test_catalog_sync(self):
        Plan = self.env['m365.ingram.plan'].with_context(active_test=False)
        old = Plan.create({'account_id': self.account.id, 'plan_id': '999', 'name': 'Old Plan'})
        suite = self.env['m365.product'].create({'name': 'Contoso Suite',
                                                 'sku_part_number': 'CONTOSO_SUITE'})
        page = {'recordsFound': 3, 'subscriptionCatalog': [
            {'groupName': 'NCE Microsoft 365 (Commercial)', 'plans': [
                {'planId': '438292', 'planName': 'Microsoft 365 E3 (NCE COM MTH)',
                 'subscriptionPeriodSummary': [{'subscriptionPeriodUnit': 'MONTHS',
                                                'subscriptionPeriod': '1'}]},
                {'planId': 438400, 'planName': 'Contoso Suite (NCE COM ANN)',
                 'subscriptionPeriodSummary': [{'subscriptionPeriodUnit': 'YEAR',
                                                'subscriptionPeriod': '1'}]},
            ]},
            {'planId': '500100', 'planName': 'Unknown Add-on'},
        ]}
        fake = FakeIngram({('GET', '/resellers/v6/catalog'): page})
        with patch(IM_PATH, side_effect=fake):
            self.account.with_user(self.admin).action_sync_plans()
        self.assertEqual(fake.calls[0][2], {'type': 'IM::subscription', 'vendor': 'Microsoft',
                                            'pageSize': 100, 'pageNumber': 1,
                                            'showGroupInfo': 'false'})
        plans = {p.plan_id: p for p in Plan.search([('account_id', '=', self.account.id)])}
        self.assertEqual((plans['438292'].product_id, plans['438292'].product_suggested,
                          plans['438292'].term), (self.sku.product_id, False, '1 months'))
        self.assertEqual((plans['438400'].product_id, plans['438400'].product_suggested,
                          plans['438400'].group_name, plans['438400'].term),
                         (suite, True, 'NCE Microsoft 365 (Commercial)', '1 year'))
        self.assertFalse(plans['500100'].product_id)
        self.assertFalse(old.active)
        plans['438400'].with_user(self.admin).product_id = self.sku.product_id
        self.assertFalse(plans['438400'].product_suggested)

    def test_plan_details_and_price(self):
        fake = FakeIngram({
            ('GET', '/resellers/v6/catalog/details'): {
                'ingramPartNumber': 'MS-E3-M2', 'vendorPartNumber': 'CFQ7TTC0LFLZ:0001',
                'subscriptionDetails': [{
                    'billingPeriod': {'billingPeriodUnit': 'MONTHS', 'billingPeriod': '1'},
                    'options': [{'resourceId': 'res-1', 'minUnits': '1', 'maxUnits': '300'}]}]},
            ('POST', '/resellers/v6/catalog/priceandavailability'): [{
                'index': 0, 'productStatusCode': 'S', 'subscriptionPrice': [{
                    'planId': '438292', 'options': [{'resourcePricing': [
                        {'unitPrice': 2475.5, 'currencyCode': 'INR'}]}]}]}],
        })
        with patch(IM_PATH, side_effect=fake):
            self.plan.with_user(self.admin).action_refresh()
        plan = self.plan
        self.assertEqual((plan.ingram_part_number, plan.vendor_part_number, plan.resource_id,
                          plan.min_units, plan.max_units, plan.billing_period),
                         ('MS-E3-M2', 'CFQ7TTC0LFLZ:0001', 'res-1', 1, 300, '1 months'))
        self.assertEqual((plan.unit_price, plan.currency_id.name), (2475.5, 'INR'))
        self.assertEqual(fake.bodies('POST', '/resellers/v6/catalog/priceandavailability'),
                         [{'products': [{'planID': '438292', 'quantityRequested': '1'}]}])
        fake.routes[('POST', '/resellers/v6/catalog/priceandavailability')] = [{
            'productStatusCode': 'E', 'productStatusMessage': 'Plan not authorized'}]
        with patch(IM_PATH, side_effect=fake), \
                self.assertRaisesRegex(M365IngramError, 'Plan not authorized'):
            self.plan._refresh_prices()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def test_order_workflow(self):
        self.account.vmf_attribute_ids = [
            (0, 0, {'level': 'header', 'name': 'msTenantId', 'source': 'tenant_id'}),
            (0, 0, {'level': 'header', 'name': 'resellerType', 'source': 'fixed',
                    'value': 'Indirect'}),
            (0, 0, {'level': 'line', 'name': 'planId', 'source': 'plan_id'}),
            (0, 0, {'level': 'header', 'name': 'mcaEmail', 'source': 'mca_email'}),
            (0, 0, {'level': 'header', 'name': 'mcaDate', 'source': 'mca_date'}),
        ]
        with self.assertRaises(AccessError):
            self.env['m365.ingram.order'].with_user(self.user).create(
                {'tenant_id': self.tenant.id})
        order = self._order()
        self.assertTrue(order.name.startswith('M365-'))
        self.assertEqual((order.account_id, order.amount_total), (self.account, 180.0))
        order.action_submit()
        self.assertEqual(order.state, 'submitted')
        self.assertTrue(order.sudo().activity_ids)
        with self.assertRaises(AccessError):
            order.with_user(self.user).action_approve()

        fake = FakeIngram({('POST', ORDERS): {'confirmationNumber': 'CNF-1',
                                               'message': 'Order received'}})
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_approve()
        self.assertEqual((order.state, order.confirmation_number, order.approver_id),
                         ('sent', 'CNF-1', self.admin))
        self.assertEqual(order.line_ids.baseline_units, 10)
        self.assertEqual(fake.bodies('POST', ORDERS), [{
            'customerOrderNumber': order.name,
            'endCustomerOrderNumber': 'contoso',
            'endUserInfo': {
                'companyName': 'Contoso Ltd', 'contact': 'Megan Bowen',
                'addressLine1': '1 MG Road', 'city': 'Bengaluru', 'postalCode': '560001',
                'countryCode': 'IN', 'phoneNumber': '+91 80 1234 5678',
                'email': 'megan@contoso.com',
            },
            'vmfAdditionalAttributes': [
                {'attributeName': 'msTenantId', 'attributeValue': 'tid-contoso'},
                {'attributeName': 'resellerType', 'attributeValue': 'Indirect'},
                {'attributeName': 'mcaEmail', 'attributeValue': 'megan@contoso.com'},
                {'attributeName': 'mcaDate', 'attributeValue': '2026-11-01'},
            ],
            'lines': [{'customerLineNumber': '1', 'quantity': 5, 'ingramPartNumber': 'MS-E3-M',
                       'vmfAdditionalAttributesLines': [
                           {'attributeName': 'planId', 'attributeValue': '438292'}]}],
        }])

        # Ingram Micro created the order: webhook
        Event = self.env['m365.ingram.event']
        payload = {'topic': 'resellers/orders', 'event': 'im::created', 'eventId': 'ev-1',
                   'resource': [{'eventType': 'im::created', 'orderNumber': '20-AB123',
                                 'customerOrderNumber': order.name,
                                 'lines': [{'lineStatus': 'Processing'}]}]}
        raw = json.dumps(payload).encode()
        self.assertEqual(Event._receive(self.account.id, raw, signature('whsec', 'ev-1')), 200)
        self.assertEqual(Event._receive(self.account.id, raw, signature('whsec', 'ev-1')), 200)
        event = Event.search([('account_id', '=', self.account.id)])
        self.assertEqual((len(event), event.state), (1, 'pending'))
        Event._cron_process()
        self.assertEqual((event.state, event.order_ids), ('done', order))
        self.assertEqual((order.state, order.ingram_order_numbers), ('confirmed', '20-AB123'))

        # Delivered once the tenant has 5 licenses more than the 10 of the order time
        Order = self.env['m365.ingram.order']
        with patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': skus(12)})):
            Order._cron_check_deliveries()
        self.assertEqual(order.state, 'confirmed')
        with patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': skus(15)})):
            Order._cron_check_deliveries()
        self.assertEqual(order.state, 'delivered')
        self.assertTrue(order.line_ids.delivered)
        self.assertIn('delivered', self._logs('license_order')[0].message)

    def test_delivery_alert(self):
        order = self._submitted()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): {}})):
            order.with_user(self.admin).action_approve()
        order._confirm(['20-AB124'], 'test')
        order.sudo().activity_ids.unlink()
        order.confirmed_date = fields.Datetime.now() - timedelta(hours=25)
        with patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': skus(10)})):
            self.env['m365.ingram.order']._cron_check_deliveries()
        self.assertTrue(order.delivery_alert_sent)
        self.assertIn('not delivered', order.sudo().activity_ids[:1].summary)

    def test_no_duplicate_order_after_timeout(self):
        order = self._submitted()
        timeout = M365IngramTimeout('Ingram Micro did not answer: read timeout')
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): timeout})):
            order.with_user(self.admin).action_approve()
        self.assertEqual((order.state, order.send_attempted), ('failed', True))
        self.assertIn('Retry looks for the order', order.error_message)
        with self.assertRaisesRegex(UserError, 'may have received'):
            order.action_cancel()
        # Found at Ingram Micro: not sent again
        fake = FakeIngram({('GET', SEARCH): {'orders': [
            {'ingramOrderNumber': '20-XY999', 'customerOrderNumber': order.name}]}})
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_retry()
        self.assertEqual((order.state, order.ingram_order_numbers), ('confirmed', '20-XY999'))
        self.assertFalse(fake.bodies('POST', ORDERS))
        self.assertEqual(fake.calls[0][2]['customerOrderNumber'], order.name)

        # Not found: sent again, with the same PO number
        other = self._submitted()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): timeout})):
            other.with_user(self.admin).action_approve()
        fake = FakeIngram({('GET', SEARCH): {'orders': []},
                           ('POST', ORDERS): {'confirmationNumber': 'CNF-2'}})
        with patch(IM_PATH, side_effect=fake):
            other.with_user(self.admin).action_retry()
        self.assertEqual(other.state, 'sent')
        self.assertEqual([b['customerOrderNumber'] for b in fake.bodies('POST', ORDERS)],
                         [other.name])

    def test_order_refused(self):
        order = self._submitted()
        refused = M365IngramError('HTTP 400: Invalid part number.', 400)
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): refused})):
            order.with_user(self.admin).action_approve()
        self.assertEqual((order.state, order.send_attempted), ('failed', False))
        order.action_cancel()
        self.assertEqual(order.state, 'cancelled')
        order.action_reset_draft()
        self.assertEqual(order.state, 'draft')
        order.action_submit()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): M365IngramError(
                'HTTP 500: Internal Server Error', 500)})):
            order.with_user(self.admin).action_approve()
        self.assertTrue(order.send_attempted)  # unknown result: looked for on retry

    def test_order_checks(self):
        Order = self.env['m365.ingram.order'].with_user(self.admin)
        with self.assertRaisesRegex(UserError, 'Add the plans'):
            Order.create({'tenant_id': self.tenant.id}).action_submit()
        self.plan.min_units = 10
        with self.assertRaisesRegex(UserError, 'at least 10 units'):
            self._order(quantity=5).action_submit()
        # A plan without catalogue product can be ordered: mapped when delivered
        self.plan.write({'min_units': 0, 'product_id': False})
        self.assertEqual(self._submitted().state, 'submitted')
        self.plan.write({'product_id': self.sku.product_id.id, 'ingram_part_number': False})
        order = self._submitted()
        fake = FakeIngram({('GET', '/resellers/v6/catalog/details'): {}})
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_approve()
        self.assertEqual((order.state, order.send_attempted), ('failed', False))
        self.assertIn('no part number', order.error_message)
        self.assertFalse(fake.bodies('POST', ORDERS))
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            order.unlink()

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------
    def test_webhook_signature_and_errors(self):
        Event = self.env['m365.ingram.event']
        good = signature('whsec', 'ev-1')
        digest = hmac.new(b'whsec', b'ev-1', hashlib.sha512).digest()
        for value in (good, digest.hex(), 'sha512=' + good):
            self.assertTrue(Event._signature_ok('whsec', 'ev-1', value))
        for secret, event_id, value in (('whsec', 'ev-1', 'wrong'), ('whsec', 'ev-2', good),
                                        (False, 'ev-1', good), ('whsec', 'ev-1', None)):
            self.assertFalse(Event._signature_ok(secret, event_id, value))
        raw = json.dumps({'eventId': 'ev-1', 'resource': []}).encode()
        self.assertEqual(Event._receive(self.account.id, raw, 'wrong'), 401)
        self.assertEqual(Event._receive(self.account.id, raw, None), 401)
        self.assertEqual(Event._receive(2147483647, raw, good), 404)
        self.assertEqual(Event._receive(self.account.id, b'not json', good), 400)
        self.assertFalse(Event.search([('account_id', '=', self.account.id)]))

        # Lines refused by Ingram Micro: the order fails, administrators are told
        order = self._submitted()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): {}})):
            order.with_user(self.admin).action_approve()
        payload = {'eventId': 'ev-3', 'event': 'im::order_error', 'resource': [{
            'customerOrderNumber': order.name, 'orders': [{
                'numberOfLinesWithError': 1, 'lines': [{
                    'lineStatus': 'Error', 'ingramPartNumber': 'MS-E3-M',
                    'statusReason': 'The customer has not accepted the Microsoft Customer '
                                    'Agreement.'}]}]}]}
        Event._receive(self.account.id, json.dumps(payload).encode(), signature('whsec', 'ev-3'))
        unknown = {'eventId': 'ev-4', 'resource': {'customerOrderNumber': 'NOT-OURS',
                                                   'orderNumber': '20-ZZ'}}
        Event._receive(self.account.id, json.dumps(unknown).encode(), signature('whsec', 'ev-4'))
        Event._cron_process()
        self.assertEqual(order.state, 'failed')
        self.assertIn('Microsoft Customer Agreement', order.error_message)
        self.assertIn('failed', order.sudo().activity_ids[:1].summary)
        self.assertEqual(Event.search([('event_id', '=', 'ev-4')]).message, 'No order of Odoo.')

        cancel = {'eventId': 'ev-5', 'event': 'im::cancelled',
                  'resource': [{'customerOrderNumber': order.name}]}
        Event._receive(self.account.id, json.dumps(cancel).encode(), signature('whsec', 'ev-5'))
        Event._cron_process()
        self.assertEqual(order.state, 'cancelled')

    # ------------------------------------------------------------------
    # Buy Licenses
    # ------------------------------------------------------------------
    def test_buy_licenses(self):
        action = self.sku.with_user(self.admin).action_buy_licenses()
        self.assertEqual(action['res_model'], 'm365.ingram.order')
        order = self.env['m365.ingram.order'].with_user(self.admin).with_context(
            action['context']).create({})
        self.assertEqual((order.tenant_id, order.account_id), (self.tenant, self.account))
        self.assertEqual((order.line_ids.plan_id, order.line_ids.quantity), (self.plan, 1))
        with self.assertRaises(AccessError):
            self.sku.with_user(self.user).action_buy_licenses()

        # No license left: dashboard alert and license request lead to Buy Licenses
        self.sku.consumed_units = 10
        Dashboard = self.env['m365.dashboard']
        alert, = [a for a in Dashboard.with_user(self.admin)._get_alerts(self.tenant, self.sku)
                  if a['kind'] == 'sku_empty']
        self.assertEqual(alert['buy_sku_id'], self.sku.id)
        alert, = [a for a in Dashboard.with_user(self.user)._get_alerts(self.tenant, self.sku)
                  if a['kind'] == 'sku_empty']
        self.assertNotIn('buy_sku_id', alert)
        request = self.env['m365.license.request'].with_user(self.admin).create({
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id})
        self.assertTrue(request.can_buy)
        self.assertEqual(request.action_buy_licenses()['res_model'], 'm365.ingram.order')
        own = self.env['m365.license.request'].with_user(self.user).create({
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id})
        self.assertFalse(own.can_buy)

        self.account.active = False
        with self.assertRaisesRegex(UserError, 'Add an Ingram Micro account first'):
            self.tenant.with_user(self.admin).action_buy_licenses()

    def test_order_access(self):
        order = self._order()
        self.assertEqual(self.env['m365.ingram.order'].with_user(self.user).search([]), order)
        other_tenant = self.env['m365.tenant'].create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'client_id': 'cid'})
        self._order(tenant_id=other_tenant.id)
        self.assertEqual(self.env['m365.ingram.order'].with_user(self.user).search([]), order)
        with self.assertRaises(AccessError):
            self.env['m365.ingram.account'].with_user(self.user).search([])


@tagged('post_install', '-at_install')
class TestM365IngramWebhookRoute(HttpCase, IngramCommon):

    def test_webhook_route(self):
        url = '/m365/ingram/webhook/%s' % self.account.id
        raw = json.dumps({'eventId': 'ev-9', 'event': 'im::created', 'resource': []}).encode()
        self.env.flush_all()
        resp = self.url_open(url, data=raw, headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status_code, 401)
        resp = self.url_open(url, data=raw, headers={'Content-Type': 'application/json',
                                                     'x-hub-signature': signature('whsec', 'ev-9')})
        self.assertEqual(resp.status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(self.env['m365.ingram.event'].search(
            [('account_id', '=', self.account.id)]).event_id, 'ev-9')


@tagged('post_install', '-at_install')
class TestM365IngramGaps(IngramCommon):
    """Limits of the specification, statuses, follow-up, cancellation, billing."""

    def _sent(self, numbers=None):
        order = self._submitted()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): {'confirmationNumber': 'C'}})):
            order.with_user(self.admin).action_approve()
        if numbers:
            order._confirm(numbers, 'test')
        return order

    def test_order_limits(self):
        long_domain = 'averyveryverylongcompany.onmicrosoft.com'
        self.tenant.write({'domain': long_domain, 'address_state': 'Karnataka',
                           'country_id': self.env.ref('base.in').id})
        self.tenant.tenant_customer_id.write({
            'name': 'Contoso Information Technology Services Private Limited',
            'city': 'Bengaluru Rural District Office Area', 'zip': '560001-12345'})
        order = self._order()
        body = order._order_body()
        self.assertEqual(body['endCustomerOrderNumber'], 'averyveryverylongc')
        info = body['endUserInfo']
        self.assertEqual(info['companyName'], 'Contoso Information Technology Services ')
        self.assertEqual((len(info['city']), info['postalCode'], info['state']),
                         (25, '560001-123', 'KA'))
        self.tenant.tenant_customer_id.ref = 'CONTOSO-001'
        self.assertEqual(order._order_body()['endCustomerOrderNumber'], 'CONTOSO-001')
        # Unknown state name: not sent rather than refused
        self.tenant.address_state = 'Somewhere Else'
        self.assertNotIn('state', order._end_user_info())

        order.name = 'M365-ORDER-NUMBER-TOO-LONG'
        with self.assertRaisesRegex(UserError, 'longer than the 18 characters'):
            order.action_submit()
        other = self._order()
        other.line_ids.copy({'order_id': other.id,
                             'currency_id': self.env.ref('base.EUR').id})
        other.line_ids[:1].currency_id = self.env.ref('base.INR')
        with self.assertRaisesRegex(UserError, 'same currency'):
            other.action_submit()
        self.tenant.ingram_relationship = False
        with self.assertRaisesRegex(UserError, 'reseller relationship with Ingram Micro'):
            self._order().action_submit()
        self.tenant.ingram_relationship = True
        self.tenant.mca_email = False
        with self.assertRaisesRegex(UserError, 'Microsoft Customer Agreement'):
            self._order().action_submit()
        self.account.require_mca = False
        self._order().action_submit()

    def test_found_cancelled_and_new_po(self):
        order = self._submitted()
        timeout = M365IngramTimeout('Ingram Micro did not answer: read timeout')
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): timeout})):
            order.with_user(self.admin).action_approve()
        fake = FakeIngram({('GET', SEARCH): {'orders': [
            {'ingramOrderNumber': '20-XY999', 'customerOrderNumber': order.name,
             'orderStatus': 'CANCELLED'}]}})
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_retry()
        self.assertEqual((order.state, order.ingram_order_numbers), ('cancelled', '20-XY999'))
        self.assertFalse(fake.bodies('POST', ORDERS))
        old = order.name
        order.action_reset_draft()
        self.assertEqual(order.state, 'draft')
        self.assertNotEqual(order.name, old)  # Ingram Micro refuses a PO number twice
        self.assertFalse(order.ingram_order_numbers)
        # On hold: confirmed, administrators told
        other = self._submitted()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): timeout})):
            other.with_user(self.admin).action_approve()
        with patch(IM_PATH, side_effect=FakeIngram({('GET', SEARCH): {'orders': [
                {'ingramOrderNumber': '20-HH1', 'orderStatus': 'ON HOLD'}]}})):
            other.with_user(self.admin).action_retry()
        self.assertEqual(other.state, 'confirmed')
        self.assertIn('needs attention', other.sudo().activity_ids[:1].summary)

    def test_follow_up_without_webhook(self):
        Order = self.env['m365.ingram.order']
        now = fields.Datetime.now()
        sent = self._sent()
        sent.sent_date = now - timedelta(hours=1)
        cut = self._submitted()
        cut.write({'state': 'sending', 'send_attempted': True,
                   'attempt_date': now - timedelta(minutes=20)})
        lost = self._sent()
        lost.sent_date = now - timedelta(hours=25)
        recent = self._sent()  # sent just now: not looked up yet
        confirmed = self._sent(['20-CC1'])
        confirmed.confirmed_date = now - timedelta(hours=3)

        def search(params):
            return {sent.name: {'orders': [{'ingramOrderNumber': '20-SS1',
                                            'orderStatus': 'PROCESSING'}]},
                    confirmed.name: {'orders': [{'ingramOrderNumber': '20-CC1',
                                                 'orderStatus': 'CANCELLED'}]},
                    }.get(params['customerOrderNumber'], {'orders': []})

        calls = []

        def fake(method, path, params=None, json_body=None, expect=()):
            calls.append(params['customerOrderNumber'])
            return 200, search(params)

        with patch(IM_PATH, side_effect=fake), \
                patch(GRAPH_PATH, side_effect=FakeGraph({'/subscribedSkus': skus(10)})):
            Order._cron_check_deliveries()
        self.assertNotIn(recent.name, calls)
        self.assertEqual((sent.state, sent.ingram_order_numbers), ('confirmed', '20-SS1'))
        self.assertEqual((cut.state, cut.send_attempted), ('failed', True))
        self.assertEqual(lost.state, 'sent')
        self.assertTrue(lost.followup_alert_sent)
        self.assertIn('not confirmed', lost.sudo().activity_ids[:1].summary)
        self.assertEqual(confirmed.state, 'cancelled')
        self.assertEqual(recent.state, 'sent')

    def test_status_events(self):
        Event = self.env['m365.ingram.event']

        def send(event_id, resource, event='im::updated'):
            payload = {'eventId': event_id, 'event': event, 'resource': [resource]}
            Event._receive(self.account.id, json.dumps(payload).encode(),
                           signature('whsec', event_id))
            Event._cron_process()

        held = self._sent(['20-HH1'])
        send('ev-h', {'eventType': 'im::order_hold', 'customerOrderNumber': held.name,
                      'lines': [{'lineStatus': 'IM::hold'}]})
        self.assertEqual(held.state, 'confirmed')
        self.assertIn('needs attention', held.sudo().activity_ids[:1].summary)
        self.assertIn('on hold', self._logs('license_order')[0].message)

        partly = self._sent(['20-PP1'])
        send('ev-p', {'customerOrderNumber': partly.name, 'lines': [
            {'lineStatus': 'IM::cancelled'}, {'lineStatus': 'IM::processing'}]})
        self.assertEqual(partly.state, 'confirmed')
        self.assertIn('some lines are cancelled', self._logs('license_order')[0].message)

        cancelled = self._sent(['20-XX1'])
        send('ev-c', {'eventType': 'im::order_updated', 'customerOrderNumber': cancelled.name,
                      'lines': [{'lineStatus': 'IM::cancelled'}]})
        self.assertEqual(cancelled.state, 'cancelled')

        shipped = self._sent()
        send('ev-s', {'eventType': 'im::order_shipped', 'orderNumber': '20-SH1',
                      'customerOrderNumber': shipped.name,
                      'lines': [{'lineStatus': 'IM::shipped'}]}, event='IM::created')
        self.assertEqual((shipped.state, shipped.ingram_order_numbers), ('confirmed', '20-SH1'))

    def test_cancel_at_ingram(self):
        order = self._sent()
        with self.assertRaisesRegex(UserError, 'has not given the order number'):
            order.with_user(self.admin).action_cancel_at_ingram()
        order._confirm(['20-AB1', '20-AB2'], 'test')
        refused = M365IngramError('HTTP 400: Order released to the warehouse.', 400)
        fake = FakeIngram({('DELETE', '/resellers/v6/orders/20-AB1'): {},
                           ('DELETE', '/resellers/v6/orders/20-AB2'): refused})
        with patch(IM_PATH, side_effect=fake):
            action = order.with_user(self.admin).action_cancel_at_ingram()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(order.state, 'confirmed')
        self.assertIn('20-AB2', self._logs('license_order')[0].message)
        fake.routes[('DELETE', '/resellers/v6/orders/20-AB2')] = {}
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_cancel_at_ingram()
        self.assertEqual(order.state, 'cancelled')
        with self.assertRaises(AccessError):
            order.with_user(self.user).action_cancel_at_ingram()

    def test_retry_while_sending(self):
        order = self._submitted()
        order.write({'state': 'sending', 'send_attempted': True,
                     'attempt_date': fields.Datetime.now()})
        with self.assertRaisesRegex(UserError, 'is being sent'):
            order.with_user(self.admin).action_retry()
        order.attempt_date = fields.Datetime.now() - timedelta(minutes=20)
        fake = FakeIngram({('GET', SEARCH): {'orders': []},
                           ('POST', ORDERS): {'confirmationNumber': 'C2'}})
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_retry()
        self.assertEqual(order.state, 'sent')
        self.assertEqual(len(fake.bodies('GET', SEARCH)), 1)  # looked for first

    def test_import_vendor_fields(self):
        self.account.vmf_attribute_ids = [(0, 0, {'level': 'header', 'name': 'msTenantId',
                                                  'source': 'tenant_id'})]
        fake = FakeIngram({('GET', '/resellers/v6/q2o/validatequote'): {
            'quoteNumber': 'QUO-1', 'vmfAdditionalAttributes': [
                {'attributeName': 'msTenantId', 'attributeDescription': 'Tenant'},
                {'attributeName': 'mcaFirstName', 'attributeDescription': 'MCA first name'}],
            'lines': [{'vmfAdditionalAttributesLines': [
                {'attributeName': 'subscriptionTerm', 'attributeValue': 'P1M',
                 'attributeDescription': 'Term'}]}]}})
        wizard = self.env['m365.ingram.vmf.import.wizard'].with_user(self.admin).create(
            {'account_id': self.account.id, 'quote_number': ' QUO-1 '})
        with patch(IM_PATH, side_effect=fake):
            action = wizard.action_import()
        self.assertIn('2 vendor field(s) added', action['params']['message'])
        self.assertEqual(fake.calls[0][2], {'quoteNumber': 'QUO-1'})
        attrs = {(a.level, a.name): a for a in self.account.vmf_attribute_ids}
        self.assertEqual(attrs[('header', 'mcaFirstName')].description, 'MCA first name')
        self.assertEqual((attrs[('line', 'subscriptionTerm')].source,
                          attrs[('line', 'subscriptionTerm')].value), ('fixed', 'P1M'))

    def test_invoices_and_renewals(self):
        order = self._sent(['40-MKTRW'])
        other_po = 'NOT-FROM-ODOO'
        search = {'recordsFound': 2, 'invoices': [
            {'invoiceNumber': '40MKTRW11', 'invoiceStatus': 'OPEN', 'invoiceDate': '2026-11-02',
             'invoiceDueDate': '2026-12-02', 'invoicedAmountDue': 243.16,
             'customerOrderNumber': order.name, 'erpOrderNumber': '40-MKTRW-11',
             'invoiceAmountInclTax': 286.93},
            {'invoiceNumber': '40ZZZ', 'customerOrderNumber': other_po,
             'invoiceDate': '2026-11-03', 'invoicedAmountDue': 10.0},
        ]}
        details = {'invoiceType': 'Invoice', 'lines': [
            {'ingramLineNumber': '1', 'ingramPartNumber': 'MS-E3-M', 'quantity': 5,
             'unitPrice': 36.0, 'extendedPrice': 180.0, 'taxAmount': 32.4, 'currencyCode': 'INR'}],
            'summary': {'totals': {'netInvoiceAmount': 180.0, 'totalTaxAmount': 32.4,
                                   'invoicedAmountDue': 212.4}}}
        renewals = {'recordsFound': 1, 'renewals': [
            {'renewalId': 4239056, 'customerOrderNumber': order.name, 'endUser': 'CONTOSO',
             'vendor': 'Microsoft', 'expirationDate': (fields.Date.today() + timedelta(days=10))
             .isoformat() + 'T00:00:00', 'renewalValue': 381.5, 'status': 'Quote pending'}]}
        fake = FakeIngram({
            ('GET', '/resellers/v6/invoices'): search,
            ('GET', '/resellers/v6.1/invoices/40MKTRW11'): details,
            ('POST', '/resellers/v6/renewals/search'): renewals,
        })
        with patch(IM_PATH, side_effect=fake):
            self.account.with_user(self.admin).action_sync_billing()
        params = fake.calls[0][2]
        today = fields.Date.context_today(self.account)
        self.assertEqual(params['invoiceFromDate'], (today - timedelta(days=90)).isoformat())
        invoices = self.env['m365.ingram.invoice'].search([('account_id', '=', self.account.id)])
        mine = invoices.filtered(lambda i: i.invoice_number == '40MKTRW11')
        self.assertEqual((mine.order_id, mine.tenant_id, mine.details_loaded, mine.amount_due),
                         (order, self.tenant, True, 212.4))
        self.assertEqual((mine.line_ids.quantity, mine.line_ids.currency_id.name), (5, 'INR'))
        self.assertFalse(invoices.filtered(lambda i: i.invoice_number == '40ZZZ').order_id)
        self.assertEqual(len(fake.bodies('GET', '/resellers/v6.1/invoices/40MKTRW11')), 1)
        self.assertEqual(order.invoice_count, 1)
        renewal = self.env['m365.ingram.renewal'].search([('account_id', '=', self.account.id)])
        self.assertEqual((renewal.renewal_id, renewal.order_id, renewal.alert_sent),
                         ('4239056', order, True))
        self.assertIn('Renewal of', order.sudo().activity_ids.mapped('summary')[-1])
        self.assertEqual(fake.bodies('POST', '/resellers/v6/renewals/search'),
                         [{'vendor': 'Microsoft'}])
        # Users see the invoices of their tenants only
        self.assertEqual(self.env['m365.ingram.invoice'].with_user(self.user).search([]), mine)
        # Next run: from the last synchronisation less a week, lines not read again
        with patch(IM_PATH, side_effect=fake):
            self.account._sync_invoices()
        self.assertEqual(fake.calls[-1][2]['invoiceFromDate'],
                         (today - timedelta(days=7)).isoformat())
        self.assertEqual(len(fake.bodies('GET', '/resellers/v6.1/invoices/40MKTRW11')), 1)

    def test_account_settings(self):
        fake = FakeIngram({('GET', '/resellers/v6/catalog'): {'subscriptionCatalog': []}})
        with patch(IM_PATH, side_effect=fake):
            self.account._sync_plans()
        self.assertEqual(fake.calls[0][2]['type'], 'IM::subscription')
        self.account.catalog_type = False
        with patch(IM_PATH, side_effect=fake):
            self.account._sync_plans()
        self.assertNotIn('type', fake.calls[1][2])
        for vals in ({'customer_number': '20-2222222222'}, {'country_code': 'I1'},
                     {'sender_id': 'x' * 33}):
            with self.assertRaises(ValidationError):
                self.account.write(vals)

    def test_events_cleanup(self):
        Event = self.env['m365.ingram.event']
        old = Event.create({'account_id': self.account.id, 'event_id': 'old', 'state': 'done'})
        old.received = fields.Datetime.now() - timedelta(days=400)
        failed = Event.create({'account_id': self.account.id, 'event_id': 'err',
                               'state': 'error'})
        failed.received = old.received
        Event._cron_cleanup()
        self.assertFalse(old.exists())
        self.assertTrue(failed.exists())
