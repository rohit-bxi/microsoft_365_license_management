# -*- coding: utf-8 -*-
"""Licenses bought through Ingram Micro in bulk (19.0.1.9.0): subscription periods,
default plan and plan matching, orders without catalogue product, Ingram orders in the
provisioning, customer file of existing tenants, shared app without Partner Center."""
import base64
import io
from unittest.mock import patch

import openpyxl

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from .common import GRAPH_PATH, GUID_A, FakeGraph
from .test_m365_ingram import IM_PATH, ORDERS, FakeIngram, IngramCommon, skus

VPN = 'CFQ7TTC0LH18:000P'
BASIC = 'Microsoft 365 Business Basic (no Teams) (NCE COM MTH)'
GUID_NEW = 'eeeeeeee-0000-0000-0000-000000000001'


def periods(*keys):
    return [{'subscriptionPeriod': n, 'subscriptionPeriodUnit': unit} for n, unit in keys]


class IngramBulkCommon(IngramCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.basic = cls.env['m365.ingram.plan'].create({
            'account_id': cls.account.id, 'plan_id': '700100', 'name': BASIC,
            'vendor_part_number': VPN, 'ingram_part_number': 'MS-BB-NT', 'unit_price': 109.2})
        cls.basic._set_terms(periods(('1', 'MONTHS'), ('1', 'YEARS')))
        cls.account.write({'default_plan_id': cls.basic.id, 'default_term': 'P1Y'})


@tagged('post_install', '-at_install')
class TestM365IngramTerms(IngramBulkCommon):

    def test_terms_from_catalog(self):
        page = {'recordsFound': 1, 'subscriptionCatalog': [
            {'planId': '700100', 'planName': BASIC,
             'subscriptionPeriodSummary': periods(('1', 'MONTHS'), ('12', 'MONTHS'))}]}
        with patch(IM_PATH, side_effect=FakeIngram({('GET', '/resellers/v6/catalog'): page})):
            self.account.with_user(self.admin).action_sync_plans()
        self.assertEqual(self.basic.term_ids.mapped('key'), ['P1M', 'P1Y'])
        self.assertEqual(self.basic.term_ids.mapped('name'), ['1 Month', '1 Year'])
        self.assertEqual(self.basic.term_ids.mapped('months'), [1, 12])
        # The value sent to Ingram Micro, once set, is kept (an administrator may change it)
        self.assertEqual(self.basic.term_ids[1].code, '1 YEARS')
        page['subscriptionCatalog'][0]['subscriptionPeriodSummary'] = periods(('1', 'YEAR'))
        with patch(IM_PATH, side_effect=FakeIngram({('GET', '/resellers/v6/catalog'): page})):
            self.account._sync_plans()
        self.assertEqual(self.basic.term_ids.mapped('key'), ['P1Y'])

    def test_line_period_and_vendor_field(self):
        self.basic.product_id = self.sku.product_id
        self.env['m365.ingram.vmf.attribute'].create([
            {'account_id': self.account.id, 'level': 'line', 'name': 'SubscriptionPeriod',
             'source': 'term'},
            {'account_id': self.account.id, 'level': 'line', 'name': 'TermMonths',
             'source': 'term_months'}])
        order = self._order(line_ids=[(0, 0, {'plan_id': self.basic.id, 'quantity': 2})])
        line = order.line_ids
        self.assertEqual(line.term_id.key, 'P1Y')  # default period of the account
        line.term_id = False
        with self.assertRaisesRegex(UserError, 'Choose the subscription period'):
            order.action_submit()
        line.term_id = self.basic.term_ids.filtered(lambda t: t.key == 'P1M')
        order.action_submit()
        fake = FakeIngram({('POST', ORDERS): {'confirmationNumber': 'C-1'}})
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_approve()
        body = fake.bodies('POST', ORDERS)[0]['lines'][0]
        self.assertEqual(body['vendorPartNumber'], VPN)
        self.assertEqual(body['vmfAdditionalAttributesLines'], [
            {'attributeName': 'SubscriptionPeriod', 'attributeValue': '1 MONTHS'},
            {'attributeName': 'TermMonths', 'attributeValue': '1'}])

    def test_match_plan(self):
        account = self.account
        plan, term, error = account._match_plan(VPN)
        self.assertEqual((plan, term.key, error), (self.basic, 'P1Y', False))
        plan, term, error = account._match_plan('Microsoft 365 Business Basic (no Teams) 1 Month')
        self.assertEqual((plan, term.key), (self.basic, 'P1M'))
        _plan, _term, error = account._match_plan(BASIC + ' P3Y')
        self.assertIn('not sold for 3 Years', error)
        # One plan per period: the period chooses; otherwise ambiguous
        annual = self.basic.copy({'plan_id': '700200', 'name': BASIC.replace('MTH', 'ANN')})
        annual._set_terms(periods(('1', 'YEARS')))
        self.basic._set_terms(periods(('1', 'MONTHS')))
        plan, term, error = account._match_plan('Microsoft 365 Business Basic (no Teams)')
        self.assertEqual((plan, term.key), (annual, 'P1Y'))  # default period: 1 year
        self.basic._set_terms(periods(('1', 'YEARS')))
        _plan, _term, error = account._match_plan('Microsoft 365 Business Basic (no Teams)')
        self.assertIn('matches several plans', error)
        _plan, _term, error = account._match_plan('Nothing')
        self.assertIn('not found', error)

    def test_order_without_product(self):
        """A license never bought: the plan is mapped when its licenses arrive."""
        self.basic.product_id = False
        order = self._order(line_ids=[(0, 0, {'plan_id': self.basic.id, 'quantity': 5})])
        order.action_submit()
        with patch(IM_PATH, side_effect=FakeIngram({('POST', ORDERS): {}})):
            order.with_user(self.admin).action_approve()
        self.assertEqual(order.state, 'sent')
        self.assertIn(GUID_A, order.line_ids.baseline_skus)
        order._confirm(['IM-1'], 'test')
        product = self.env['m365.product'].create({
            'name': 'Microsoft 365 Business Basic (no Teams)',
            'sku_part_number': 'BASIC_NO_TEAMS_TEST'})
        self.env['m365.sku'].create({
            'tenant_id': self.tenant.id, 'sku_id': GUID_NEW, 'product_id': product.id,
            'sku_part_number': 'BASIC_NO_TEAMS_TEST', 'enabled_units': 5})
        order._check_delivery()
        self.assertEqual(order.state, 'delivered')
        self.assertEqual(self.basic.product_id, product)
        self.assertEqual(self.tenant._match_license('default').sku_id, GUID_NEW)
        self.assertEqual(self.tenant._match_license(VPN).sku_id, GUID_NEW)


@tagged('post_install', '-at_install')
class TestM365IngramProvisioning(IngramBulkCommon):

    def setUp(self):
        super().setUp()
        self.basic.product_id = self.sku.product_id
        self.tenant.write({'requested_subscriptions': '3', 'prov_approve_orders': True,
                           'auto_provision': True, 'prov_user_id': self.admin.id})

    def test_ordered_and_delivered(self):
        tenant = self.tenant
        self.assertEqual(tenant._purchase_channel(), 'ingram')
        self.assertEqual((tenant.prov_gdap, tenant.prov_agreement, tenant.prov_relationship),
                         ('na', 'done', 'done'))
        fake = FakeIngram({('POST', ORDERS): {'confirmationNumber': 'C-7'}})
        graph = FakeGraph({'/subscribedSkus': skus(13)})
        with patch(IM_PATH, side_effect=fake), patch(GRAPH_PATH, side_effect=graph):
            self.env['m365.tenant']._cron_auto_provision()
        order = tenant.ingram_order_ids
        self.assertEqual((order.state, order.approver_id), ('sent', self.admin))
        self.assertEqual(order.line_ids.mapped(lambda l: (l.plan_id, l.term_id.key,
                                                          l.quantity)),
                         [(self.basic, 'P1Y', 3)])
        self.assertEqual(tenant.prov_subscriptions, 'pending')
        self.assertIn('Ingram Micro to confirm', tenant.prov_waiting)
        order._confirm(['IM-9'], 'webhook')
        tenant.prov_next_run = False
        with patch(IM_PATH, side_effect=fake), patch(GRAPH_PATH, side_effect=graph):
            self.env['m365.tenant']._cron_auto_provision()
        self.assertEqual(order.state, 'delivered')
        self.assertEqual(tenant.prov_subscriptions, 'done')

    def test_relationship_needed(self):
        self.tenant.ingram_relationship = False
        self.assertEqual(self.tenant.prov_relationship, 'action')
        with patch(IM_PATH, side_effect=FakeIngram()) as call:
            self.env['m365.tenant']._cron_auto_provision()
        call.assert_not_called()
        self.assertFalse(self.tenant.ingram_order_ids)
        self.assertIn('reseller relationship', self.tenant.prov_waiting)
        # Agreement through Ingram: recorded in Odoo, not attested in Partner Center
        self.tenant.write({'ingram_relationship': True, 'mca_accepted': False})
        self.assertEqual(self.tenant.prov_agreement, 'action')

    def test_unknown_plan_stops(self):
        self.tenant.requested_subscriptions = 'Unknown Plan = 2'
        self.env['m365.tenant']._cron_auto_provision()
        self.assertFalse(self.tenant.auto_provision)
        self.assertIn('Unknown Plan', self.tenant.prov_error)


@tagged('post_install', '-at_install')
class TestM365IngramImport(IngramBulkCommon):

    HEADER = ('Tenant Name,Primary Domain,Directory (Tenant) ID,Application (Client) ID,'
              'Client Secret,Subscriptions,CustomDomains,AgreementFirstName,AgreementEmail,'
              'IngramRelationship\n')

    def _wizard(self, rows, users=None, **vals):
        values = dict({
            'tenant_type': 'existing',
            'file': base64.b64encode((self.HEADER + rows).encode()), 'filename': 'tenants.csv',
            'mca_confirmed': True, 'auto_provision': True, 'approve_orders': True}, **vals)
        if users:
            values.update(users_file=base64.b64encode(users.encode()),
                          users_filename='users.csv')
        return self.env['m365.tenant.import.wizard'].with_user(self.admin).create(values)

    def test_existing_tenants_with_licenses_and_users(self):
        wizard = self._wizard(
            'Litware Bulk,litware-bulk.example,%s,%s,s1,%s = 4,litware-bulk.in,Lee,'
            'lee@litware-bulk.example,yes\n' % (GUID_NEW, GUID_NEW, VPN),
            users='CompanyName,UserName,DisplayName,Licenses\n'
                  'Litware Bulk,adele,Adele V,default\n')
        wizard.action_validate()
        self.assertEqual(wizard.error_count, 0, wizard.line_ids.mapped('message'))
        self.assertEqual(wizard.user_count, 1)
        wizard.action_import()
        tenant = self.env['m365.tenant'].search([('name', '=', 'Litware Bulk')])
        self.assertEqual((tenant.tenant_type, tenant.requested_subscriptions,
                          tenant.custom_domains), ('existing', VPN + ' = 4', 'litware-bulk.in'))
        self.assertTrue(tenant._mca_recorded())
        self.assertEqual((tenant.mca_email, tenant.mca_date),
                         ('lee@litware-bulk.example', fields.Date.context_today(tenant)))
        self.assertTrue(tenant.ingram_relationship and tenant.ingram_relationship_date)
        self.assertTrue(tenant.auto_provision and tenant.prov_approve_orders)
        self.assertEqual(tenant.pending_user_ids.upn, 'adele@litware-bulk.in')
        lines, errors = tenant._parse_requested_ingram()
        self.assertEqual((lines, errors), ([(self.basic, self.basic.term_ids[1], 4)], []))

    def test_agreement_contact_required(self):
        wizard = self._wizard('Litware Two,litware-two.example,%s,%s,s1,,,,,\n'
                              % (GUID_NEW, GUID_NEW), ingram_linked=True)
        wizard.action_validate()
        self.assertIn('AgreementEmail are required', wizard.line_ids.message)


@tagged('post_install', '-at_install')
class TestM365SharedAppWithoutPartnerCenter(IngramBulkCommon):

    def test_account_without_partner_center(self):
        account = self.env['m365.partner.account'].create({
            'name': 'Shared App Only', 'graph_client_id': 'shared-cid',
            'graph_client_secret': 'shared-secret', 'purchase_channel': 'ingram'})
        action = account.with_user(self.admin).action_test_connection()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('not configured', action['params']['message'])
        self.tenant.partner_account_id = account
        self.assertEqual(self.tenant.prov_gdap, 'na')
        self.assertEqual(account._shared_app_credentials(), ('shared-cid', 'shared-secret'))
        with self.assertRaisesRegex(UserError, 'Partner Directory'):
            self.tenant._gdap_account()


@tagged('post_install', '-at_install')
class TestM365IngramReport(HttpCase, IngramBulkCommon):

    def test_report_ingram_lines(self):
        order = self._order(line_ids=[(0, 0, {'plan_id': self.basic.id, 'quantity': 2})])
        order.ingram_order_numbers = 'IM-42'
        self.authenticate(self.admin.login, self.admin.login)
        self.env.flush_all()
        resp = self.url_open('/m365/export/provisioning?tenant_ids=%s' % self.tenant.id)
        rows = list(openpyxl.load_workbook(io.BytesIO(resp.content))['Subscriptions']
                    .iter_rows(values_only=True))
        self.assertEqual(rows[1][1:7], ('Ingram Micro', order.name, BASIC, '1 Year', 2,
                                        'IM-42'))
