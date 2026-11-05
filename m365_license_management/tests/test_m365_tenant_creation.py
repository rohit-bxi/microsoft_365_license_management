# -*- coding: utf-8 -*-
import ast
from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.m365_partner_account import M365PartnerCenterTimeout
from ..models.m365_tenant import M365GraphError
from .common import ADMIN_GROUP, MODELS, USER_GROUP, http_response, map_tenants

ACCOUNT = MODELS + '.m365_partner_account'
PC_PATH = ACCOUNT + '.M365PartnerAccount._pc_request'
CUSTOMER_ID = 'dddddddd-0000-0000-0000-000000000001'
CREATED = {
    'id': CUSTOMER_ID,
    'companyProfile': {'tenantId': CUSTOMER_ID, 'domain': 'adatum.onmicrosoft.com'},
    'userCredentials': {'userName': 'admin', 'password': 'Temp=Pass1'},
}


@tagged('post_install', '-at_install')
class TestM365TenantCreation(TransactionCase):
    """A tenant of type New: created through Partner Center, then connected."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['m365.partner.account'].create({
            'name': 'BXI CSP', 'partner_tenant_id': 'partner.onmicrosoft.com',
            'client_id': 'pc-app', 'client_secret': 'pc-secret', 'default_culture': 'en-IN',
        })
        cls.tenant = cls.env['m365.tenant'].create({
            'tenant_type': 'new', 'name': 'Adatum Corporation', 'domain': 'Adatum',
            'partner_account_id': cls.account.id, 'contact_first_name': 'Adele',
            'contact_last_name': 'Vance', 'contact_email': 'adele.vance@adatum.com',
            'contact_phone': '+91 80 4000 1000', 'address_line1': '1 MG Road',
            'city': 'Bengaluru', 'address_state': 'KA', 'postal_code': '560001',
            'country_id': cls.env.ref('base.in').id,
            'organization_registration_number': 'U72900KA2020PTC000000',
        })
        cls.admin = new_test_user(cls.env, 'm365_creation_admin', groups=ADMIN_GROUP)

    def _partner_center(self, domain_status=404, address=None, created=None):
        """Partner Center mock: returns the list of the calls and the side effect."""
        calls = []

        def pc_request(account, method, path, json_body=None, request_id=None, expect=()):
            calls.append((method, path, json_body, request_id))
            if path.startswith('/v1/domains/'):
                return domain_status, {}
            if path == '/v1/validations/address':
                return 200, address or {'status': 'VerifiedShippable'}
            if path == '/v1/customers':
                return 201, created or CREATED
            raise AssertionError('Unexpected call %s %s' % (method, path))

        return calls, pc_request

    def _create(self, records, pc_request):
        """Queue the records and run the scheduled action, as an administrator."""
        records.with_user(self.admin).action_create()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self.env['m365.tenant']._cron_provision_tenants()

    def _logs(self):
        return self.env['m365.audit.log'].search([('action', '=', 'provision_tenant'),
                                                  ('tenant_id', '=', self.tenant.id)])

    # ------------------------------------------------------------------
    # Type and data
    # ------------------------------------------------------------------
    def test_defaults(self):
        tenant = self.tenant
        # The initial domain is completed, the defaults come from the Partner Center account
        self.assertEqual((tenant.domain, tenant.culture, tenant.language, tenant.enable_gdap,
                          tenant.provision_state, tenant.state, tenant.tenant_id),
                         ('adatum.onmicrosoft.com', 'en-IN', 'en', True, 'draft', 'draft',
                          False))
        tenant.domain = ' Adatum-Group '
        self.assertEqual(tenant.domain, 'adatum-group.onmicrosoft.com')
        self.assertEqual(tenant._check_values(), [])
        # From the Tenants button of the Partner Center account
        action = self.account.action_view_tenants()
        other = self.env['m365.tenant'].with_context(action['context']).create(
            {'name': 'Litware', 'domain': 'litware'})
        self.assertEqual((other.tenant_type, other.partner_account_id, other.domain,
                          other.provision_state),
                         ('new', self.account, 'litware.onmicrosoft.com', 'draft'))
        self.assertEqual(self.account.tenant_count, 2)

    def test_existing_tenant(self):
        Tenant = self.env['m365.tenant']
        with self.assertRaisesRegex(UserError, 'required for the existing tenant Contoso'):
            Tenant.create({'name': 'Contoso', 'domain': 'contoso.com'})
        existing = Tenant.create({'name': 'Contoso', 'domain': 'Contoso.com',
                                  'tenant_id': 'tid-contoso-x', 'client_id': 'cid'})
        self.assertEqual((existing.tenant_type, existing.provision_state, existing.domain,
                          existing.partner_account_id),
                         ('existing', False, 'Contoso.com', self.account.browse()))

    def test_change_type(self):
        tenant = self.tenant
        other = tenant.copy({'domain': 'adatum-other'})
        tenant.write({'tenant_type': 'existing', 'tenant_id': 'tid-adatum', 'client_id': 'cid'})
        self.assertEqual(tenant.provision_state, False)
        tenant.tenant_type = 'new'
        self.assertEqual(tenant.provision_state, 'draft')
        tenant.write({'tenant_type': 'existing', 'state': 'connected'})
        with self.assertRaisesRegex(UserError, 'type of a connected or created tenant'):
            tenant.tenant_type = 'new'
        # An existing tenant needs its IDs
        with self.assertRaisesRegex(UserError, 'required for the existing tenant'):
            other.tenant_type = 'existing'

    def test_check_values(self):
        tenant = self.env['m365.tenant'].new({
            'tenant_type': 'new', 'name': 'Bad', 'domain': 'bad_domain.onmicrosoft.com',
            'contact_email': 'not-an-email', 'culture': 'english', 'language': 'eng',
            'country_id': self.env.ref('base.in').id,
        })
        self.assertEqual(tenant._check_values(), [
            'Partner Center Account is required.',
            'Contact First Name is required.',
            'Contact Last Name is required.',
            'Address Line 1 is required.',
            'City is required.',
            'Postal Code is required.',
            'Domain "bad_domain.onmicrosoft.com" is not valid: use letters, digits and '
            'hyphens followed by .onmicrosoft.com, e.g. contoso.onmicrosoft.com.',
            '"not-an-email" is not a valid email address.',
            'Culture "english" is not valid, e.g. en-US.',
            'Language "eng" is not valid, e.g. en.',
            'Microsoft requires the organization registration number for India.',
        ])

    def test_only_admins(self):
        user = map_tenants(new_test_user(self.env, 'm365_creation_user', groups=USER_GROUP),
                           self.tenant)
        tenant = self.tenant.with_user(user)
        self.assertEqual(tenant.name, 'Adatum Corporation')
        with self.assertRaises(AccessError):
            tenant.read(['partner_account_id'])
        with self.assertRaises(AccessError):
            tenant.action_check()
        with self.assertRaises(AccessError):
            self.env['m365.partner.account'].with_user(user).search([])

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------
    def test_check(self):
        calls, pc_request = self._partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            action = self.tenant.action_check()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual((self.tenant.provision_state, self.tenant.address_status,
                          self.tenant.provision_error), ('ready', 'VerifiedShippable', False))
        self.assertEqual([call[:3] for call in calls], [
            ('HEAD', '/v1/domains/adatum.onmicrosoft.com', None),
            ('POST', '/v1/validations/address', {
                'AddressLine1': '1 MG Road', 'City': 'Bengaluru', 'State': 'KA',
                'PostalCode': '560001', 'Country': 'IN'}),
        ])
        # Checked again when the data sent to Microsoft changes
        self.tenant.address_line2 = 'Floor 2'
        self.assertEqual(self.tenant.provision_state, 'draft')

    def test_check_errors(self):
        address = {'status': 'StreetPartial',
                   'validationMessage': "Address field invalid for property: 'PostalCode'",
                   'suggestedAddresses': [{'addressLine1': '1 Mahatma Gandhi Road',
                                           'city': 'Bengaluru', 'state': 'KA',
                                           'postalCode': '560001', 'country': 'IN'}]}
        _calls, pc_request = self._partner_center(domain_status=200, address=address)
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            action = self.tenant.action_check()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(self.tenant.provision_state, 'draft')
        self.assertEqual(self.tenant.provision_error.split('\n'), [
            'Domain adatum.onmicrosoft.com is already used in Microsoft 365.',
            "Microsoft could not verify the address (StreetPartial). Address field invalid "
            "for property: 'PostalCode' Suggested address: 1 Mahatma Gandhi Road, Bengaluru, "
            "KA, 560001, IN. Correct the address, or tick Address Confirmed to use it as "
            "entered.",
        ])
        # Address used as entered: not verified again
        self.tenant.write({'domain': 'adatum2', 'address_confirmed': True})
        calls, pc_request = self._partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self.tenant.action_check()
        self.assertEqual(self.tenant.provision_state, 'ready')
        self.assertEqual([call[1] for call in calls], ['/v1/domains/adatum2.onmicrosoft.com'])
        # Invalid data: Microsoft is not called
        self.tenant.contact_email = 'adele'
        with patch(PC_PATH) as pc:
            self.tenant.action_check()
        self.assertFalse(pc.called)
        self.assertEqual(self.tenant.provision_error, '"adele" is not a valid email address.')

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def test_create(self):
        user = map_tenants(new_test_user(self.env, 'm365_creation_user', groups=USER_GROUP),
                           self.tenant)
        with self.assertRaisesRegex(UserError, 'Check the tenants first'):
            self.tenant.action_create()
        self.tenant.provision_state = 'ready'
        with self.assertRaises(AccessError):
            self.tenant.with_user(user).action_create()
        calls, pc_request = self._partner_center()
        self._create(self.tenant, pc_request)
        (method, path, payload, request_id), = calls
        self.assertEqual((method, path), ('POST', '/v1/customers'))
        self.assertEqual(len(request_id), 36)
        self.assertEqual(payload, {
            'enableGDAPByDefault': True,
            'CompanyProfile': {'Domain': 'adatum.onmicrosoft.com',
                               'OrganizationRegistrationNumber': 'U72900KA2020PTC000000'},
            'BillingProfile': {
                'Culture': 'en-IN', 'Email': 'adele.vance@adatum.com', 'Language': 'en',
                'CompanyName': 'Adatum Corporation',
                'DefaultAddress': {
                    'AddressLine1': '1 MG Road', 'City': 'Bengaluru', 'State': 'KA',
                    'PostalCode': '560001', 'Country': 'IN', 'FirstName': 'Adele',
                    'LastName': 'Vance', 'PhoneNumber': '+91 80 4000 1000',
                },
            },
        })
        tenant = self.tenant
        # Created: the directory ID comes from Microsoft, the connection is still to test
        self.assertEqual((tenant.provision_state, tenant.customer_id, tenant.tenant_id,
                          tenant.state, tenant.admin_username, tenant.admin_password,
                          tenant.pc_request_id, tenant.queued_by),
                         ('done', CUSTOMER_ID, CUSTOMER_ID, 'draft',
                          'admin@adatum.onmicrosoft.com', 'Temp=Pass1', False, self.admin))
        self.assertTrue(tenant.created_date)
        log = self._logs()
        self.assertEqual((log.status, log.user_id), ('success', self.admin))
        self.assertEqual(log.message, 'Adatum Corporation (adatum.onmicrosoft.com) created, '
                                      'customer ID %s.' % CUSTOMER_ID)
        # Its Partner Center data cannot be changed, its name and domain can
        with self.assertRaisesRegex(UserError, 'cannot be changed'):
            tenant.city = 'Mysuru'
        tenant.write({'name': 'Adatum', 'domain': 'adatum.com'})
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            tenant.unlink()
        with self.assertRaisesRegex(UserError, 'type of a connected or created tenant'):
            tenant.tenant_type = 'existing'

    def test_create_failure_and_retry(self):
        self.tenant.provision_state = 'ready'
        error = 'HTTP 400 - InvalidInput (2000): The address is invalid.'
        self._create(self.tenant, M365GraphError(error))
        self.assertEqual((self.tenant.provision_state, self.tenant.provision_error,
                          self.tenant.pc_request_id), ('failed', error, False))
        self.assertEqual((self._logs().status, self._logs().message),
                         ('failed', 'Adatum Corporation (adatum.onmicrosoft.com): %s' % error))
        # No answer: the request ID is kept and sent again, so that the tenant is not
        # created twice
        self._create(self.tenant, M365PartnerCenterTimeout('Partner Center did not answer'))
        request_id = self.tenant.pc_request_id
        self.assertEqual(self.tenant.provision_state, 'failed')
        self.assertTrue(request_id)
        calls, pc_request = self._partner_center()
        self._create(self.tenant, pc_request)
        self.assertEqual(calls[0][3], request_id)
        self.assertEqual(self.tenant.provision_state, 'done')
        # Reset to draft: checked again, with a new request ID
        failed = self.tenant.copy({'domain': 'adatum3', 'provision_state': 'failed',
                                   'pc_request_id': 'old-id'})
        failed.action_reset_creation()
        self.assertEqual((failed.provision_state, failed.pc_request_id), ('draft', False))

    def test_create_in_batches(self):
        self.tenant.provision_state = 'ready'
        other = self.tenant.copy({'domain': 'adatum-group', 'provision_state': 'ready'})
        cron = self.env.ref('m365_license_management.ir_cron_m365_create_tenants')
        triggers = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        _calls, pc_request = self._partner_center()
        with patch(MODELS + '.m365_tenant_creation.PROVISION_BATCH', 1):
            self._create(self.tenant | other, pc_request)
        # One per run, the scheduled action is triggered again for the next one
        self.assertEqual((self.tenant.provision_state, other.provision_state),
                         ('done', 'queued'))
        self.assertEqual(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]),
                         triggers + 2)

    def test_after_creation(self):
        self.tenant.write({'provision_state': 'done', 'tenant_id': CUSTOMER_ID,
                           'admin_password': 'Temp=Pass1'})
        self.tenant.action_clear_password()
        self.assertFalse(self.tenant.admin_password)
        self.assertEqual(self.tenant.message_ids[0].preview,
                         'Initial admin password removed from Odoo.')

    # ------------------------------------------------------------------
    # Tenants not created yet
    # ------------------------------------------------------------------
    def test_not_created_yet(self):
        tenant = self.tenant
        Tenant = self.env['m365.tenant']
        # Not connected nor synchronised before it exists in Microsoft 365
        with patch(MODELS + '.m365_tenant.requests.post') as post:
            action = tenant.action_test_connection()
            self.assertEqual(tenant._sync_all_safe(), (False, [
                'Adatum Corporation: create the tenant in Microsoft 365 first.']))
        self.assertFalse(post.called)
        self.assertEqual((action['params']['type'], action['params']['message']),
                         ('danger', 'Adatum Corporation: create the tenant in Microsoft 365 '
                                    'first.'))
        self.assertEqual(tenant.state, 'draft')
        # Not in the dashboard, its refresh and the license request tenants
        admin = Tenant.with_user(self.admin)
        result = admin.sync_from_dashboard(tenant_ids=[tenant.id])
        self.assertEqual(result, {'ok': True, 'messages': []})
        data = self.env['m365.dashboard'].get_dashboard_data()
        self.assertNotIn(tenant.id, [row['id'] for row in data['tenants']])
        domain = ast.literal_eval(
            self.env['m365.license.request']._fields['tenant_id'].domain)
        self.assertNotIn(tenant, Tenant.search(domain))
        # Once created it is a tenant like the others
        tenant.write({'provision_state': 'done', 'tenant_id': CUSTOMER_ID})
        data = self.env['m365.dashboard'].get_dashboard_data()
        self.assertIn(tenant.id, [row['id'] for row in data['tenants']])
        self.assertIn(tenant, Tenant.search(domain))

    # ------------------------------------------------------------------
    # Partner Center client
    # ------------------------------------------------------------------
    def test_partner_center_client(self):
        token = http_response(200, {'access_token': 'pc-token', 'expires_in': 3600})
        profile = http_response(200, {'companyName': 'BXI Technologies'})
        with patch(ACCOUNT + '.requests.post', return_value=token) as post, \
                patch(ACCOUNT + '.requests.request', return_value=profile) as call:
            self.account.action_test_connection()
        self.assertEqual(post.call_args.args, ('https://login.microsoftonline.com/'
                                               'partner.onmicrosoft.com/oauth2/v2.0/token',))
        self.assertEqual(post.call_args.kwargs['data'], {
            'client_id': 'pc-app', 'client_secret': 'pc-secret',
            'scope': 'https://api.partnercenter.microsoft.com/.default',
            'grant_type': 'client_credentials'})
        self.assertEqual(call.call_args.args, (
            'GET', 'https://api.partnercenter.microsoft.com/v1/profiles/legalbusiness'))
        headers = call.call_args.kwargs['headers']
        self.assertEqual((headers['Authorization'], headers['Accept']),
                         ('Bearer pc-token', 'application/json'))
        self.assertNotEqual(headers['MS-RequestId'], headers['MS-CorrelationId'])
        self.assertEqual((self.account.state, self.account.partner_name),
                         ('connected', 'BXI Technologies'))
        error = http_response(403, {'code': 900, 'errorName': 'Forbidden',
                                    'description': 'The app is not allowed.'})
        with patch(ACCOUNT + '.requests.request', return_value=error):
            action = self.account.action_test_connection()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual((self.account.state, self.account.last_error),
                         ('error', 'HTTP 403 - Forbidden (900): The app is not allowed.'))
        # Expected statuses are returned, e.g. 404 = the domain is available
        with patch(ACCOUNT + '.requests.request', return_value=http_response(404)):
            self.assertEqual(self.account._pc_request('HEAD', '/v1/domains/x.onmicrosoft.com',
                                                      expect=(404,)), (404, {}))
        # No answer: the caller retries with the same request ID
        with patch(ACCOUNT + '.requests.request',
                   side_effect=requests.Timeout('Read timed out')), \
                self.assertRaisesRegex(M365PartnerCenterTimeout, 'Read timed out'):
            self.account._pc_request('POST', '/v1/customers', json_body={}, request_id='r-1')
        # New credentials: tested again with a new token
        self.account.client_secret = 'new-secret'
        self.assertEqual(self.account.state, 'draft')
        self.assertFalse(self.account.sudo().access_token)
        # The Tenants button lists the tenants created with the account
        action = self.account.action_view_tenants()
        self.assertEqual(self.env['m365.tenant'].search(action['domain']), self.tenant)
