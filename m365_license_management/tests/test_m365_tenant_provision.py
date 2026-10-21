# -*- coding: utf-8 -*-
import base64
import io
from unittest.mock import patch

import openpyxl
import requests

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from ..models.m365_tenant_provision import IMPORT_COLUMNS, M365PartnerCenterTimeout
from .common import MODELS, USER_GROUP, http_response

PROVISION = MODELS + '.m365_tenant_provision'
PC_PATH = PROVISION + '.M365PartnerAccount._pc_request'
CUSTOMER_ID = 'dddddddd-0000-0000-0000-000000000001'
CREATED = {
    'id': CUSTOMER_ID,
    'companyProfile': {'tenantId': CUSTOMER_ID, 'domain': 'adatum.onmicrosoft.com'},
    'userCredentials': {'userName': 'admin', 'password': 'Temp=Pass1'},
}
HEADER = ('CompanyName,Domain,FirstName,LastName,Email,PhoneNumber,AddressLine1,AddressLine2,'
          'City,State,PostalCode,Country\n')


@tagged('post_install', '-at_install')
class TestM365TenantProvision(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The domains of the database must not clash with the ones of the tests
        cls.env.cr.execute("UPDATE m365_tenant_provision "
                           "SET domain = 'existing-' || id || '.onmicrosoft.com'")
        cls.env.invalidate_all()
        cls.account = cls.env['m365.partner.account'].create({
            'name': 'BXI CSP', 'partner_tenant_id': 'partner.onmicrosoft.com',
            'client_id': 'pc-app', 'client_secret': 'pc-secret', 'default_culture': 'en-IN',
        })
        cls.customer = cls.env['m365.tenant.provision'].create({
            'name': 'Adatum Corporation', 'domain': 'Adatum', 'partner_account_id': cls.account.id,
            'first_name': 'Adele', 'last_name': 'Vance', 'email': 'adele.vance@adatum.com',
            'phone': '+91 80 4000 1000', 'address_line1': '1 MG Road', 'city': 'Bengaluru',
            'address_state': 'KA', 'postal_code': '560001',
            'country_id': cls.env.ref('base.in').id,
            'organization_registration_number': 'U72900KA2020PTC000000',
        })
        cls.admin = new_test_user(cls.env, 'm365_provision_admin',
                                  groups='m365_license_management.group_m365_admin')

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
            self.env['m365.tenant.provision']._cron_provision_tenants()

    def _logs(self):
        return self.env['m365.audit.log'].search([('action', '=', 'provision_tenant'),
                                                  ('message', '=like', 'Adatum Corporation (%')])

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def test_defaults(self):
        customer = self.customer
        # The initial domain is completed, the defaults come from the Partner Center account
        self.assertEqual((customer.domain, customer.culture, customer.language,
                          customer.enable_gdap, customer.state),
                         ('adatum.onmicrosoft.com', 'en-IN', 'en', True, 'draft'))
        customer.domain = ' Adatum-Group '
        self.assertEqual(customer.domain, 'adatum-group.onmicrosoft.com')
        self.assertEqual(customer._check_values(), [])

    def test_check_values(self):
        customer = self.env['m365.tenant.provision'].new({
            'name': 'Bad', 'domain': 'bad_domain.onmicrosoft.com', 'email': 'not-an-email',
            'culture': 'english', 'language': 'eng', 'partner_account_id': self.account.id,
            'country_id': self.env.ref('base.in').id,
        })
        self.assertEqual(customer._check_values(), [
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
        user = new_test_user(self.env, 'm365_provision_user', groups=USER_GROUP)
        with self.assertRaises(AccessError):
            self.env['m365.tenant.provision'].with_user(user).search([])
        with self.assertRaises(AccessError):
            self.env['m365.partner.account'].with_user(user).search([])

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------
    def test_check(self):
        calls, pc_request = self._partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            action = self.customer.action_check()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual((self.customer.state, self.customer.address_status,
                          self.customer.error_message), ('ready', 'VerifiedShippable', False))
        self.assertEqual([call[:3] for call in calls], [
            ('HEAD', '/v1/domains/adatum.onmicrosoft.com', None),
            ('POST', '/v1/validations/address', {
                'AddressLine1': '1 MG Road', 'City': 'Bengaluru', 'State': 'KA',
                'PostalCode': '560001', 'Country': 'IN'}),
        ])
        # Checked again when the data sent to Microsoft changes
        self.customer.address_line2 = 'Floor 2'
        self.assertEqual(self.customer.state, 'draft')

    def test_check_errors(self):
        address = {'status': 'StreetPartial',
                   'validationMessage': "Address field invalid for property: 'PostalCode'",
                   'suggestedAddresses': [{'addressLine1': '1 Mahatma Gandhi Road',
                                           'city': 'Bengaluru', 'state': 'KA',
                                           'postalCode': '560001', 'country': 'IN'}]}
        _calls, pc_request = self._partner_center(domain_status=200, address=address)
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            action = self.customer.action_check()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(self.customer.state, 'draft')
        self.assertEqual(self.customer.error_message.split('\n'), [
            'Domain adatum.onmicrosoft.com is already used in Microsoft 365.',
            "Microsoft could not verify the address (StreetPartial). Address field invalid "
            "for property: 'PostalCode' Suggested address: 1 Mahatma Gandhi Road, Bengaluru, "
            "KA, 560001, IN. Correct the address, or tick Address Confirmed to use it as "
            "entered.",
        ])
        # Address used as entered: not verified again
        self.customer.write({'domain': 'adatum2', 'address_confirmed': True})
        calls, pc_request = self._partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self.customer.action_check()
        self.assertEqual(self.customer.state, 'ready')
        self.assertEqual([call[1] for call in calls], ['/v1/domains/adatum2.onmicrosoft.com'])
        # Invalid data: Microsoft is not called
        self.customer.email = 'adele'
        with patch(PC_PATH) as pc:
            self.customer.action_check()
        self.assertFalse(pc.called)
        self.assertEqual(self.customer.error_message, '"adele" is not a valid email address.')

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def test_create(self):
        user = new_test_user(self.env, 'm365_provision_user', groups=USER_GROUP)
        with self.assertRaisesRegex(UserError, 'Check the tenants first'):
            self.customer.action_create()
        self.customer.state = 'ready'
        with self.assertRaises(AccessError):
            self.customer.with_user(user).action_create()
        calls, pc_request = self._partner_center()
        self._create(self.customer, pc_request)
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
        customer = self.customer
        self.assertEqual((customer.state, customer.customer_id, customer.customer_tenant_id,
                          customer.admin_username, customer.admin_password,
                          customer.pc_request_id, customer.queued_by),
                         ('done', CUSTOMER_ID, CUSTOMER_ID, 'admin@adatum.onmicrosoft.com',
                          'Temp=Pass1', False, self.admin))
        self.assertTrue(customer.created_date)
        log = self._logs()
        self.assertEqual((log.status, log.user_id), ('success', self.admin))
        self.assertEqual(log.message, 'Adatum Corporation (adatum.onmicrosoft.com) created, '
                                      'customer ID %s.' % CUSTOMER_ID)
        # A created tenant cannot be changed nor deleted
        with self.assertRaisesRegex(UserError, 'cannot be changed'):
            customer.city = 'Mysuru'
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            customer.unlink()

    def test_create_failure_and_retry(self):
        self.customer.state = 'ready'
        error = 'HTTP 400 - InvalidInput (2000): The address is invalid.'
        self._create(self.customer, M365GraphError(error))
        self.assertEqual((self.customer.state, self.customer.error_message,
                          self.customer.pc_request_id), ('failed', error, False))
        self.assertEqual((self._logs().status, self._logs().message),
                         ('failed', 'Adatum Corporation (adatum.onmicrosoft.com): %s' % error))
        # No answer: the request ID is kept and sent again, so that the tenant is not
        # created twice
        self._create(self.customer, M365PartnerCenterTimeout('Partner Center did not answer'))
        request_id = self.customer.pc_request_id
        self.assertEqual(self.customer.state, 'failed')
        self.assertTrue(request_id)
        calls, pc_request = self._partner_center()
        self._create(self.customer, pc_request)
        self.assertEqual(calls[0][3], request_id)
        self.assertEqual(self.customer.state, 'done')
        # Reset to draft: checked again, with a new request ID
        failed = self.customer.copy({'domain': 'adatum3', 'state': 'failed',
                                     'pc_request_id': 'old-id'})
        failed.action_reset_draft()
        self.assertEqual((failed.state, failed.pc_request_id), ('draft', False))

    def test_create_in_batches(self):
        self.customer.state = 'ready'
        other = self.customer.copy({'domain': 'adatum-group', 'state': 'ready'})
        cron = self.env.ref('m365_license_management.ir_cron_m365_provision_tenants')
        triggers = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        _calls, pc_request = self._partner_center()
        with patch(PROVISION + '.PROVISION_BATCH', 1):
            self._create(self.customer | other, pc_request)
        # One per run, the scheduled action is triggered again for the next one
        self.assertEqual((self.customer.state, other.state), ('done', 'queued'))
        self.assertEqual(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]),
                         triggers + 2)

    def test_after_creation(self):
        self.customer.write({'state': 'done', 'customer_tenant_id': CUSTOMER_ID,
                             'admin_password': 'Temp=Pass1'})
        action = self.customer.action_register_tenant()
        self.assertEqual(action['context'], {
            'default_name': 'Adatum Corporation', 'default_domain': 'adatum.onmicrosoft.com',
            'default_tenant_id': CUSTOMER_ID, 'default_company_id': self.env.company.id})
        tenant = self.env['m365.tenant'].with_context(**action['context']).create(
            {'client_id': 'app'})
        self.customer.invalidate_recordset(['tenant_record_id'])
        self.assertEqual(self.customer.tenant_record_id, tenant)
        self.assertEqual(self.customer.action_register_tenant()['res_id'], tenant.id)
        self.customer.action_clear_password()
        self.assertFalse(self.customer.admin_password)
        self.assertEqual(self.customer.message_ids[0].preview,
                         'Initial admin password removed from Odoo.')

    # ------------------------------------------------------------------
    # Partner Center client
    # ------------------------------------------------------------------
    def test_partner_center_client(self):
        token = http_response(200, {'access_token': 'pc-token', 'expires_in': 3600})
        profile = http_response(200, {'companyName': 'BXI Technologies'})
        with patch(PROVISION + '.requests.post', return_value=token) as post, \
                patch(PROVISION + '.requests.request', return_value=profile) as call:
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
        with patch(PROVISION + '.requests.request', return_value=error):
            action = self.account.action_test_connection()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual((self.account.state, self.account.last_error),
                         ('error', 'HTTP 403 - Forbidden (900): The app is not allowed.'))
        # Expected statuses are returned, e.g. 404 = the domain is available
        with patch(PROVISION + '.requests.request', return_value=http_response(404)):
            self.assertEqual(self.account._pc_request('HEAD', '/v1/domains/x.onmicrosoft.com',
                                                      expect=(404,)), (404, {}))
        # No answer: the caller retries with the same request ID
        with patch(PROVISION + '.requests.request',
                   side_effect=requests.Timeout('Read timed out')), \
                self.assertRaisesRegex(M365PartnerCenterTimeout, 'Read timed out'):
            self.account._pc_request('POST', '/v1/customers', json_body={}, request_id='r-1')
        # New credentials: tested again with a new token
        self.account.client_secret = 'new-secret'
        self.assertEqual(self.account.state, 'draft')
        self.assertFalse(self.account.sudo().access_token)

    # ------------------------------------------------------------------
    # Bulk import (Customers.csv)
    # ------------------------------------------------------------------
    def _wizard(self, content, filename='Customers.csv'):
        return self.env['m365.tenant.provision.import.wizard'].create({
            'partner_account_id': self.account.id, 'filename': filename,
            'file': base64.b64encode(content.encode() if isinstance(content, str) else content),
        })

    def test_import_customers(self):
        wizard = self._wizard(
            HEADER
            + 'Fabrikam Inc,FabrikamInc,Lee,Gu,lee.gu@fabrikam.com,,1 Main St,,Redmond,WA,'
              '98052,US\n'
            + 'Northwind,northwind.onmicrosoft.com,Megan,Bowen,megan@northwind.com,,'
              '2 Pine St,Suite 5,Seattle,WA,98101,United States\n')
        wizard.action_validate()
        self.assertEqual((wizard.line_count, wizard.error_count), (2, 0))
        self.assertEqual(wizard.line_ids.mapped('domain'),
                         ['fabrikaminc.onmicrosoft.com', 'northwind.onmicrosoft.com'])
        action = wizard.action_import()
        records = self.env['m365.tenant.provision'].search(
            action['params']['next']['domain'], order='id')
        self.assertEqual(records.mapped('name'), ['Fabrikam Inc', 'Northwind'])
        self.assertEqual(set(records.mapped('state')), {'draft'})
        self.assertEqual(set(records.mapped('culture')), {'en-IN'})
        self.assertEqual(records.country_id, self.env.ref('base.us'))
        self.assertEqual(records[1].address_line2, 'Suite 5')
        self.assertEqual(records.partner_account_id, self.account)

    def test_import_errors(self):
        wizard = self._wizard(
            HEADER
            + 'Adatum Again,adatum,A,B,a@adatum.com,,1 St,,Pune,MH,411001,US\n'
            + ',bad domain!,A,B,not-an-email,,1 St,,City,,,Narnia\n'
            + 'India Co,indiaco,A,B,a@india.co,,1 St,,Pune,MH,411001,IN\n'
            + 'Twice,twice,A,B,a@twice.co,,1 St,,Austin,TX,73301,US\n'
            + 'Twice Again,TWICE,A,B,a@twice.co,,1 St,,Austin,TX,73301,US\n')
        with self.assertRaisesRegex(UserError, 'Validate the file'):
            wizard.action_import()
        wizard.action_validate()
        self.assertEqual((wizard.line_count, wizard.error_count), (5, 4))
        messages = wizard.line_ids.mapped('message')
        self.assertEqual(messages[0], 'Domain adatum.onmicrosoft.com is already in the '
                                      'tenants to create.')
        for text in ('Country "Narnia" not found', 'Company Name is required.',
                     'Postal Code is required.', 'Domain "bad domain!.onmicrosoft.com" is not '
                     'valid', '"not-an-email" is not a valid email address.'):
            self.assertIn(text, messages[1])
        self.assertNotIn('Country is required.', messages[1])
        self.assertEqual(messages[2], 'Microsoft requires the organization registration '
                                      'number for India.')
        self.assertFalse(messages[3])
        self.assertEqual(messages[4], 'Duplicate Domain, same as SN. 4.')
        with self.assertRaisesRegex(UserError, '(?s)SN. 1: .*SN. 5: '):
            wizard.action_import()

    def test_import_headers(self):
        for header, error in (('CompanyName,Domain,Email\n', 'Missing required column'),
                              (HEADER.replace('State', 'Region'), 'Unknown column'),
                              (HEADER.replace('Email', 'Contact Email,E-mail'), 'Unknown'),
                              (HEADER.replace('PhoneNumber', 'Phone_Number,Phone'),
                               'appears more than once')):
            with self.assertRaisesRegex(UserError, error):
                self._wizard(header + 'x\n').action_validate()
        with self.assertRaisesRegex(UserError, 'no customer rows'):
            self._wizard(HEADER).action_validate()
        # The field labels are accepted as well
        wizard = self._wizard('Company Name,Domain,Contact First Name,Contact Last Name,'
                              'Contact Email,Address Line 1,City,Postal Code,Country\n'
                              'Litware,litware,Lee,Gu,lee@litware.com,1 St,Austin,73301,US\n')
        wizard.action_validate()
        self.assertEqual((wizard.line_count, wizard.error_count), (1, 0))

    def test_import_template(self):
        wizard = self.env['m365.tenant.provision.import.wizard'].create(
            {'partner_account_id': self.account.id})
        action = wizard.action_download_template()
        self.assertIn('/template_file/m365_customers_template.xlsx', action['url'])
        sheet = openpyxl.load_workbook(io.BytesIO(base64.b64decode(wizard.template_file))).active
        self.assertEqual([cell.value for cell in sheet[1]],
                         [title for _fname, title, _required in IMPORT_COLUMNS])
        # The example row is valid
        wizard.write({'file': wizard.template_file, 'filename': 'template.xlsx'})
        wizard.action_validate()
        self.assertEqual((wizard.line_count, wizard.error_count), (1, 0))
