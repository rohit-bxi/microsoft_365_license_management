# -*- coding: utf-8 -*-
"""New tenants created by Ingram Micro with their first order (19.0.1.10.0)."""
import copy
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.m365_ingram_account import M365IngramError
from .common import MODELS, http_response
from .test_m365_ingram import IM_PATH, ORDERS, FakeIngram, IngramCommon

TENANT_MODULE = MODELS + '.m365_ingram_tenant'
LOOKUP = TENANT_MODULE + '.M365TenantIngramCreation._lookup_tenant_id'
HTTP_GET = TENANT_MODULE + '.requests.get'
VRI = '/resellers/v7/vendorrequiredinfo'
GUID_NEW = 'abcdef01-2345-6789-abcd-ef0123456789'


def sub(name, value='', **extra):
    return dict({'attributeName': name, 'attributeValue': value, 'choices': []}, **extra)


# Vendor required info of a Microsoft plan, as in the Ingram Micro specification
REQUIRED_INFO = [{
    'planId': '438292', 'planName': 'Microsoft 365 E3 (NCE COM MTH)', 'responseMessages': [],
    'vmfAdditionalAttributes': [],
    'vriAdditionalAttributes': [{
        'vendorName': 'Microsoft', 'productId': 'PRD-814-505-018',
        'additionalAttributes': [
            sub('tier1_mpn', '5068860'),
            sub('tenant_preference', 'new,existing', choices=[
                {'attributeValue': 'new', 'choices': [
                    sub('microsoft_domain', 'onmicrosoft.com'),
                    sub('effective_address'), sub('effective_city'), sub('effective_state'),
                    sub('effective_postal_code'), sub('effective_country'),
                    sub('mca_acceptance', 'yes,no'), sub('first_name_agreement'),
                    sub('last_name_agreement'), sub('email_address_agreement'),
                    sub('agreement_date'), sub('effective_phonenumber'),
                    sub('partner_on_record_attestation_accepted', 'attestation_accepted'),
                    sub('special_qualifications', 'None,StateOwnedEntity'),
                    sub('address_approved_by_user', 'approved'),
                ]},
                {'attributeValue': 'existing', 'choices': [
                    sub('microsoft_domain', 'onmicrosoft.com'), sub('relationship_url', 'yes,no'),
                ]},
            ]),
            sub('indirect_reseller_domain', 'bxitech.onmicrosoft.com'),
        ],
    }],
}]

EXPECTED_ATTRIBUTES = {
    'tier1_mpn': '5068860',
    'tenant_preference': 'new',
    'microsoft_domain': 'fabrikam',
    'effective_address': '2 Residency Road',
    'effective_city': 'Bengaluru',
    'effective_state': 'KA',
    'effective_postal_code': '560025',
    'effective_country': 'IN',
    'mca_acceptance': 'yes',
    'first_name_agreement': 'Alex',
    'last_name_agreement': 'Wilber',
    'email_address_agreement': 'alex@fabrikam.com',
    'agreement_date': '11-10-2026',
    'effective_phonenumber': '+91 80 5555 0100',
    'partner_on_record_attestation_accepted': 'attestation_accepted',
    'special_qualifications': 'None',
    'address_approved_by_user': 'approved',
    'indirect_reseller_domain': 'bxitech.onmicrosoft.com',
}


def attributes(body):
    """{name: value} of the vendor required info of the first line of an order body."""
    group = body['lines'][0]['vriAdditionalAttributes'][0]
    return {a['attributeName']: a['attributeValue'] for a in group['additionalAttributes']}


@tagged('post_install', '-at_install')
class TestM365IngramTenant(IngramCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.new = cls.env['m365.tenant'].create({
            'name': 'Fabrikam', 'tenant_type': 'new', 'domain': 'fabrikam',
            'contact_first_name': 'Alex', 'contact_last_name': 'Wilber',
            'contact_email': 'alex@fabrikam.com', 'contact_phone': '+91 80 5555 0100',
            'address_line1': '2 Residency Road', 'city': 'Bengaluru', 'address_state': 'KA',
            'postal_code': '560025', 'country_id': cls.env.ref('base.in').id,
            'mca_accepted': True, 'mca_first_name': 'Alex', 'mca_last_name': 'Wilber',
            'mca_email': 'alex@fabrikam.com', 'mca_date': fields.Date.to_date('2026-11-10'),
            'requested_subscriptions': 'Microsoft 365 E3 (NCE COM MTH) = 5',
        })

    def _fake(self, **routes):
        return FakeIngram(dict({('POST', VRI): copy.deepcopy(REQUIRED_INFO),
                                ('POST', ORDERS): {'confirmationNumber': 'C-1001'}},
                               **routes))

    def _check(self, tenant=None, used=False):
        tenant = (tenant or self.new).with_user(self.admin)
        with patch(LOOKUP, return_value=used) as lookup:
            tenant.action_check()
        return lookup

    def _create(self, fake=None):
        fake = fake or self._fake()
        with patch(IM_PATH, side_effect=fake):
            action = self.new.with_user(self.admin).action_create()
        return fake, action

    # ------------------------------------------------------------------
    # Channel and check
    # ------------------------------------------------------------------
    def test_channel(self):
        """Partner Center is disabled: every new tenant is created by Ingram Micro."""
        self.assertEqual(self.new.creation_channel, 'ingram')
        self.assertEqual(self.new._purchase_channel(), 'ingram')
        Tenant = self.env['m365.tenant']
        self.assertEqual(Tenant.default_get(['creation_channel'])['creation_channel'], 'ingram')
        self.assertEqual(Tenant._fields['creation_channel'].selection,
                         [('ingram', 'Ingram Micro')])
        # Neither culture / language nor a Partner Center account are needed
        self.assertFalse(self.new.culture or self.new.partner_account_id)
        # Even with a Partner Center account buying through Partner Center
        account = self.env['m365.partner.account'].with_context(active_test=False).search(
            [], limit=1)
        if account:
            account.write({'active': True, 'purchase_channel': 'partner_center'})
            self.assertEqual(self.new._purchase_channel(), 'ingram')

    def test_check(self):
        lookup = self._check()
        self.assertEqual((self.new.provision_state, self.new.provision_error), ('ready', False))
        # The domain is looked up in Microsoft 365; no registration number for Ingram Micro
        self.assertEqual(lookup.call_args.args, ('fabrikam.onmicrosoft.com',))

        self.new.contact_phone = False
        self.assertEqual(self.new.provision_state, 'draft')  # checked again
        self._check()
        self.assertEqual(self.new.provision_error, 'Contact Phone is required.')
        # What Ingram Micro needs: checked once the data is valid
        self.new.write({'contact_phone': '+91 80 5555 0100', 'mca_accepted': False,
                        'requested_subscriptions': 'Unknown Plan = 2', 'address_state': False})
        lookup = self._check()
        error = self.new.provision_error
        self.assertEqual(self.new.provision_state, 'draft')
        self.assertIn('State / Region is required', error)  # India has states
        self.assertIn('Microsoft Customer Agreement', error)
        self.assertIn('Plan "Unknown Plan" not found', error)
        self.assertFalse(lookup.called)

        self.new.write({'mca_accepted': True, 'address_state': 'KA',
                        'requested_subscriptions': 'Microsoft 365 E3 (NCE COM MTH) = 5',
                        'domain': 'fabrikam-international-holdings-ltd'})
        self._check()
        self.assertIn('27 characters at most', self.new.provision_error)

        self.new.domain = 'fabrikam'
        self._check(used=GUID_NEW)
        self.assertIn('already used in Microsoft 365', self.new.provision_error)
        self._check(used=None)
        self.assertIn('did not tell whether', self.new.provision_error)

        self.new.requested_subscriptions = False
        self._check()
        self.assertIn('Enter the Subscriptions to Buy', self.new.provision_error)

    def test_lookup_tenant_id(self):
        found = http_response(200, {'issuer': 'https://login.microsoftonline.com/%s/v2.0'
                                              % GUID_NEW.upper()})
        with patch(HTTP_GET, return_value=found) as get:
            self.assertEqual(self.new._lookup_tenant_id('fabrikam.onmicrosoft.com'), GUID_NEW)
        self.assertEqual(get.call_args.args, (
            'https://login.microsoftonline.com/fabrikam.onmicrosoft.com/v2.0/'
            '.well-known/openid-configuration',))
        with patch(HTTP_GET, return_value=http_response(400, {'error': 'invalid_tenant'})):
            self.assertIs(self.new._lookup_tenant_id('fabrikam.onmicrosoft.com'), False)
        with patch(HTTP_GET, return_value=http_response(503, {})):
            self.assertIsNone(self.new._lookup_tenant_id('fabrikam.onmicrosoft.com'))
        with patch(HTTP_GET, side_effect=requests.ConnectionError('down')):
            self.assertIsNone(self.new._lookup_tenant_id('fabrikam.onmicrosoft.com'))

    # ------------------------------------------------------------------
    # Order creating the tenant
    # ------------------------------------------------------------------
    def test_create(self):
        self._check()
        fake, action = self._create()
        order = self.new.ingram_creation_order_id
        self.assertEqual(action['params']['type'], 'success')
        self.assertTrue(order.create_tenant)
        self.assertEqual((order.state, order.confirmation_number), ('sent', 'C-1001'))
        self.assertEqual(order.approver_id, self.admin)
        self.assertEqual((self.new.provision_state, self.new.queued_by),
                         ('queued', self.admin))
        self.assertEqual(fake.bodies('POST', VRI), [{'products': [{'planID': '438292'}]}])
        body = fake.bodies('POST', ORDERS)[0]
        line = body['lines'][0]
        self.assertEqual((line['planid'], line['quantity'], line['ingramPartNumber']),
                         ('438292', 5, 'MS-E3-M'))
        self.assertNotIn('subscriptionPeriod', line)  # the plan has no period
        group = line['vriAdditionalAttributes'][0]
        self.assertEqual((group['vendorName'], group['productId']),
                         ('Microsoft', 'PRD-814-505-018'))
        self.assertEqual(attributes(body), EXPECTED_ATTRIBUTES)
        self.assertNotIn('relationship_url', attributes(body))  # choice "existing"
        self.assertEqual(body['endUserInfo']['companyName'], 'Fabrikam')

        # Created once the domain exists in Microsoft 365
        with patch(LOOKUP, return_value=False):
            self.env['m365.tenant']._cron_provision_tenants()
        self.assertEqual(self.new.provision_state, 'queued')
        with patch(LOOKUP, return_value=GUID_NEW):
            self.env['m365.tenant']._cron_provision_tenants()
        self.assertEqual((self.new.provision_state, self.new.tenant_id), ('done', GUID_NEW))
        self.assertTrue(self.new.ingram_relationship and self.new.ingram_relationship_date)
        self.assertTrue(self.new.created_date)
        self.assertEqual(self._logs('provision_tenant', self.new).status, 'success')
        # A tenant created stays created, whatever happens to its order
        order.write({'state': 'cancelled'})
        self.assertEqual(self.new.provision_state, 'done')

    def test_period_and_vendor_field(self):
        self.plan._set_terms([{'subscriptionPeriod': '1', 'subscriptionPeriodUnit': 'YEARS'}])
        self.env['m365.ingram.vmf.attribute'].create({
            'account_id': self.account.id, 'level': 'header', 'name': 'special_qualifications',
            'source': 'fixed', 'value': 'StateOwnedEntity'})
        self._check()
        fake, _action = self._create()
        body = fake.bodies('POST', ORDERS)[0]
        self.assertEqual(body['lines'][0]['subscriptionPeriod'], {'type': 'YEARS', 'duration': 1})
        self.assertEqual(attributes(body)['special_qualifications'], 'StateOwnedEntity')

    def test_existing_tenant_order(self):
        """An order for an existing tenant tells Ingram Micro which tenant gets the
        licenses: tenant_preference existing and its .onmicrosoft.com domain."""
        self.plan._set_terms([{'subscriptionPeriod': '1', 'subscriptionPeriodUnit': 'YEARS'}])
        order = self._submitted()
        fake = self._fake()
        with patch(IM_PATH, side_effect=fake):
            order.with_user(self.admin).action_approve()
        self.assertEqual(order.state, 'sent', order.error_message)
        self.assertEqual(fake.bodies('POST', VRI), [{'products': [{'planID': '438292'}]}])
        line = fake.bodies('POST', ORDERS)[0]['lines'][0]
        self.assertEqual((line['planid'], line['subscriptionPeriod']),
                         ('438292', {'type': 'YEARS', 'duration': 1}))
        self.assertEqual(attributes({'lines': [line]}), {
            'tier1_mpn': '5068860', 'tenant_preference': 'existing',
            'microsoft_domain': 'contoso', 'relationship_url': 'no',
            'indirect_reseller_domain': 'bxitech.onmicrosoft.com'})

    def test_existing_tenant_without_domain(self):
        """Without its .onmicrosoft.com domain, Ingram Micro cannot find the tenant: the
        order is not submitted."""
        self.tenant.domain_ids.unlink()
        self.tenant.state = 'error'  # domains not read again from Microsoft 365
        with self.assertRaisesRegex(UserError, 'onmicrosoft.com domain of tenant Contoso'):
            self._submitted()
        # A vendor field gives it
        self.env['m365.ingram.vmf.attribute'].create({
            'account_id': self.account.id, 'level': 'header', 'name': 'microsoft_domain',
            'source': 'fixed', 'value': 'contoso'})
        self.assertEqual(self._submitted().state, 'submitted')

    def test_missing_required_field(self):
        info = copy.deepcopy(REQUIRED_INFO)
        info[0]['vriAdditionalAttributes'][0]['additionalAttributes'].append(
            sub('customer_po_ref'))
        self._check()
        fake = self._fake()
        fake.routes[('POST', VRI)] = info
        _fake, action = self._create(fake)
        order = self.new.ingram_creation_order_id
        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(order.state, 'failed')
        self.assertFalse(fake.bodies('POST', ORDERS))  # nothing sent
        self.assertEqual(self.new.provision_state, 'failed')
        self.assertIn('customer_po_ref', self.new.provision_error)

        # Given as a vendor field of the account: Retry sends the same order
        self.env['m365.ingram.vmf.attribute'].create({
            'account_id': self.account.id, 'level': 'line', 'name': 'customer_po_ref',
            'source': 'fixed', 'value': 'PO-77'})
        self._create(fake)
        self.assertEqual(self.new.ingram_creation_order_id, order)
        self.assertEqual((order.state, self.new.provision_state), ('sent', 'queued'))
        self.assertEqual(attributes(fake.bodies('POST', ORDERS)[0])['customer_po_ref'], 'PO-77')

    def test_without_required_info(self):
        self._check()
        fake = self._fake()
        fake.routes[('POST', VRI)] = M365IngramError('HTTP 404: not found', 404)
        self._create(fake)
        order = self.new.ingram_creation_order_id
        self.assertEqual(order.state, 'sent')
        group = fake.bodies('POST', ORDERS)[0]['lines'][0]['vriAdditionalAttributes'][0]
        self.assertEqual(group['vendorName'], 'Microsoft')
        self.assertNotIn('productId', group)
        expected = {k: v for k, v in EXPECTED_ATTRIBUTES.items()
                    if k not in ('tier1_mpn', 'indirect_reseller_domain')}
        self.assertEqual({a['attributeName']: a['attributeValue']
                          for a in group['additionalAttributes']}, expected)
        self.assertIn('gave no vendor required info', ' '.join(order.message_ids.mapped('body')))

    def test_refused_then_cancelled(self):
        self._check()
        fake = self._fake()
        fake.routes[('POST', ORDERS)] = M365IngramError('HTTP 400: Invalid domain', 400)
        self._create(fake)
        order = self.new.ingram_creation_order_id
        self.assertEqual((order.state, order.send_attempted), ('failed', False))
        self.assertEqual(self.new.provision_state, 'failed')
        self.assertIn('Invalid domain', self.new.provision_error)

        order.with_user(self.admin).action_cancel()
        self.assertEqual(self.new.provision_state, 'failed')
        self.assertIn('was cancelled', self.new.provision_error)
        self.assertFalse(self.new.ingram_creation_order_id)
        # Retry: a new order of the Subscriptions to Buy
        self._create()
        second = self.new.ingram_creation_order_id
        self.assertNotEqual(second, order)
        self.assertEqual((second.state, self.new.provision_state), ('sent', 'queued'))

    def test_order_checks(self):
        Order = self.env['m365.ingram.order'].with_user(self.admin)
        line = [(0, 0, {'plan_id': self.plan.id, 'quantity': 1})]
        # Not a creation order: the tenant must exist
        with self.assertRaisesRegex(UserError, 'Create the tenant in Microsoft 365 first'):
            Order.create({'tenant_id': self.new.id, 'line_ids': line}).action_submit()
        first = Order.create({'tenant_id': self.new.id, 'line_ids': line,
                              'create_tenant': True})
        with self.assertRaisesRegex(UserError, 'Check tenant Fabrikam first'):
            first.action_submit()
        self._check()
        first.action_submit()
        self.assertEqual(self.new.provision_state, 'queued')
        with self.assertRaisesRegex(UserError, 'already creates tenant'):
            Order.create({'tenant_id': self.new.id, 'line_ids': line,
                          'create_tenant': True}).action_submit()
        with self.assertRaisesRegex(UserError, 'Only a new tenant'):
            Order.create({'tenant_id': self.tenant.id, 'line_ids': line,
                          'create_tenant': True}).action_submit()

    def test_same_tenant_id_in_odoo(self):
        self._check()
        self._create()
        with patch(LOOKUP, return_value='tid-contoso'):
            self.env['m365.tenant']._cron_provision_tenants()
        self.assertEqual(self.new.provision_state, 'failed')
        self.assertIn('already in Odoo as Contoso', self.new.provision_error)

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------
    def test_provisioning_step(self):
        tenant = self.new.with_user(self.admin)
        with patch(LOOKUP, return_value=False), patch(IM_PATH, side_effect=self._fake()):
            status, message, _action = tenant._prov_handle('prov_tenant', interactive=True)
        order = self.new.ingram_creation_order_id
        self.assertEqual((status, order.state, self.new.provision_state),
                         ('action', 'submitted', 'queued'))
        self.assertIn('Approval of the order %s' % order.name, message)
        self.assertEqual(self.new.prov_tenant, 'action')

        self.new.prov_approve_orders = True
        self.assertEqual(self.new.prov_tenant, 'pending')
        fake = self._fake()
        with patch(LOOKUP, return_value=False), patch(IM_PATH, side_effect=fake):
            status, message, _action = tenant._prov_handle('prov_tenant')
        self.assertEqual((status, order.state), ('waiting', 'sent'))
        self.assertIn('Ingram Micro to create the tenant', message)
        with patch(LOOKUP, return_value=GUID_NEW):
            status, _message, _action = tenant._prov_handle('prov_tenant')
        self.assertEqual((status, self.new.prov_tenant), ('progress', 'done'))
        # The order is the subscription order of the provisioning as well
        self.assertEqual(self.new._purchase_orders(), order)
        self.assertEqual(self.new.prov_relationship, 'done')

    def test_provisioning_refused(self):
        self.new.prov_approve_orders = True
        fake = self._fake()
        fake.routes[('POST', ORDERS)] = M365IngramError('HTTP 400: Invalid domain', 400)
        tenant = self.new.with_user(self.admin)
        with patch(LOOKUP, return_value=False), patch(IM_PATH, side_effect=fake):
            status, message, _action = tenant._prov_handle('prov_tenant')
            self.assertEqual(status, 'action')
            self.assertIn('Invalid domain', message)
            # Refused: the automatic run does not send it again
            status, message, _action = tenant._prov_handle('prov_tenant')
        self.assertEqual(status, 'action')
        self.assertIn('Ingram Micro refused the order', message)
        self.assertEqual(len(fake.bodies('POST', ORDERS)), 1)
