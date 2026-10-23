# -*- coding: utf-8 -*-
import io
import uuid
from unittest.mock import patch

import openpyxl

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import Request
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from ..models import m365_tenant_portal
from ..models.m365_tenant import M365GraphError
from .common import ADMIN_GROUP, GRAPH_PATH, MODELS

PC_PATH = MODELS + '.m365_partner_account.M365PartnerAccount._pc_request'
PORTAL_GROUP = 'm365_license_management.group_m365_portal'
PORTAL_GROUPS = 'base.group_portal,' + PORTAL_GROUP
URL = '/my/m365/tenants'
TENANT_1 = 'aaaaaaaa-0000-4000-8000-000000000001'
TENANT_2 = 'aaaaaaaa-0000-4000-8000-000000000002'
TENANT_3 = 'aaaaaaaa-0000-4000-8000-000000000003'
CLIENT = 'bbbbbbbb-0000-4000-8000-000000000001'
# Single forms, as posted
EXISTING = {'name': 'Portal Contoso', 'domain': 'portal-contoso.com', 'tenant_id': TENANT_1,
            'client_id': CLIENT, 'client_secret': 'portal-secret'}
NEW = {'name': 'Portal Adatum', 'domain': 'PortalAdatum', 'contact_first_name': 'Adele',
       'contact_last_name': 'Vance', 'contact_email': 'adele.vance@adatum.com',
       'contact_phone': '+91 80 4000 1000', 'address_line1': '1 MG Road', 'city': 'Bengaluru',
       'address_state': 'KA', 'postal_code': '560001', 'country_id': 'IN',
       'organization_registration_number': 'U72900KA2020PTC000000', 'culture': 'en-IN',
       'language': 'en'}
EXISTING_HEADER = ('Tenant Name,Primary Domain,Directory (Tenant) ID,Application (Client) ID,'
                   'Client Secret\n')
NEW_HEADER = ('CompanyName,Domain,FirstName,LastName,Email,AddressLine1,City,PostalCode,'
              'Country,OrganizationRegistrationNumber\n')


def partner_center(domain_status=404):
    """Partner Center mock: domain available, address verified, customer created."""
    calls = []

    def pc_request(account, method, path, json_body=None, request_id=None, expect=()):
        calls.append((method, path))
        if path.startswith('/v1/domains/'):
            return domain_status, {}
        if path == '/v1/validations/address':
            return 200, {'status': 'VerifiedShippable'}
        if path == '/v1/customers':
            domain = json_body['CompanyProfile']['Domain']
            customer_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, domain))
            return 201, {'id': customer_id,
                         'companyProfile': {'tenantId': customer_id, 'domain': domain},
                         'userCredentials': {'userName': 'admin', 'password': 'Temp=Pass1'}}
        raise AssertionError('Unexpected call %s %s' % (method, path))

    return calls, pc_request


def graph_ok(method, path, params=None, json_body=None, paginate=False):
    """Microsoft Graph of a tenant whose app has every permission."""
    if path == '/organization':
        return [{'id': 'org', 'displayName': 'Contoso'}]
    return [] if paginate else {}


def graph_denied(method, path, params=None, json_body=None, paginate=False):
    raise M365GraphError("Authentication failed. HTTP 401 - invalid_client: bad secret")


class M365PortalCase(TransactionCase):
    """A Partner Center account, two portal users with the tenant self-service (the
    other one with a tenant) and a portal user without it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The account of the portal is the first one: no other account in the test
        cls.env['m365.partner.account'].search([]).write({'active': False})
        cls.account = cls.env['m365.partner.account'].create({
            'name': 'BXI CSP', 'partner_tenant_id': 'partner.onmicrosoft.com',
            'client_id': 'pc-app', 'client_secret': 'pc-secret', 'default_culture': 'en-IN',
        })
        cls.owner = new_test_user(cls.env, 'm365_portal_owner', groups=PORTAL_GROUPS)
        cls.other = new_test_user(cls.env, 'm365_portal_other', groups=PORTAL_GROUPS)
        cls.plain = new_test_user(cls.env, 'm365_portal_plain', groups='base.group_portal')
        cls.other_tenant = cls.env['m365.tenant'].with_user(cls.other)._portal_create([{
            'tenant_type': 'existing', 'name': 'Other Fabrikam',
            'domain': 'other-fabrikam.com', 'tenant_id': TENANT_2, 'client_id': CLIENT}])

    def _tenants(self, user=None):
        return self.env['m365.tenant'].search(
            [('portal_user_id', '=', (user or self.owner).id)], order='id')


@tagged('post_install', '-at_install')
class TestM365Portal(M365PortalCase):

    def _portal(self, user=None):
        return self.env['m365.tenant'].with_user(user or self.owner)

    def test_access(self):
        Tenant = self._portal()
        mine = Tenant._portal_create([dict(EXISTING, tenant_type='existing')])
        self.assertEqual((mine.portal_user_id, mine.tenant_type, mine.company_id),
                         (self.owner, 'existing', self.env.company))
        self.assertIn('Tenant added on the portal by', mine.message_ids[0].body)
        # Own tenants only, read only
        self.assertEqual(Tenant.search([]), mine)
        with self.assertRaises(AccessError):
            self.other_tenant.with_user(self.owner).read(['name'])
        mine_as_owner = mine.with_user(self.owner)
        mine_as_owner.read(['name', 'state', 'tenant_id'])
        for operation in (lambda: mine_as_owner.write({'name': 'Renamed'}),
                          mine_as_owner.unlink,
                          lambda: Tenant.create(dict(EXISTING, name='X', domain='x.com',
                                                     tenant_id=TENANT_3)),
                          # Administrator fields
                          lambda: mine_as_owner.read(['client_secret']),
                          lambda: mine_as_owner.read(['admin_password'])):
            with self.assertRaises(AccessError):
                operation()
        # Portal users without the self-service see no tenant
        with self.assertRaises(AccessError):
            mine.with_user(self.plain).read(['name'])

    def test_group(self):
        group = self.env.ref(PORTAL_GROUP)
        internal = new_test_user(self.env, 'm365_portal_internal', groups='base.group_user')
        # Exclusive with the internal users
        with self.assertRaises(ValidationError):
            internal.group_ids = [Command.link(group.id)]
        # Given / removed by the Microsoft 365 administrators from the users list
        admin = new_test_user(self.env, 'm365_portal_admin', groups=ADMIN_GROUP)
        users = self.plain | internal
        grant = self.env.ref('m365_license_management.action_m365_portal_grant')
        result = grant.with_user(admin).with_context(
            active_model='res.users', active_ids=users.ids).run()
        self.assertIn('1 user(s) skipped', result['params']['message'])
        self.assertIn(group, self.plain.group_ids)
        self.assertNotIn(group, internal.all_group_ids)
        revoke = self.env.ref('m365_license_management.action_m365_portal_revoke')
        revoke.with_user(admin).with_context(active_model='res.users',
                                             active_ids=self.plain.ids).run()
        self.assertNotIn(group, self.plain.group_ids)
        self.assertIn(self.env.ref('base.group_portal'), self.plain.all_group_ids)

    def test_form_values_existing(self):
        Tenant = self._portal()
        vals, errors, others = Tenant._portal_form_values(
            'existing', dict(EXISTING, tenant_id=TENANT_1.upper(), authority_url=''))
        self.assertEqual((errors, others), ({}, []))
        self.assertEqual(vals, dict(EXISTING))
        vals, errors, others = Tenant._portal_form_values('existing', {
            'name': 'Other Fabrikam', 'domain': 'not a domain', 'tenant_id': 'not-a-guid',
            'client_id': '', 'graph_url': 'http://graph.microsoft.com'})
        self.assertEqual(set(errors), {'name', 'domain', 'tenant_id', 'client_id',
                                       'client_secret', 'graph_url'})
        self.assertEqual(errors['name'], 'Tenant Name "Other Fabrikam" already exists in Odoo.')
        self.assertEqual(errors['client_id'], 'Application (Client) ID is required.')
        self.assertIn('is not a valid ID', errors['tenant_id'])
        # Editing: the tenant itself is no duplicate, a blank secret keeps the saved one
        tenant = self.other_tenant.sudo()
        tenant.client_secret = 'saved'
        vals, errors, others = Tenant._portal_form_values(
            'existing', dict(EXISTING, name='Other Fabrikam', tenant_id=TENANT_2,
                             client_secret=''), tenant)
        self.assertEqual(errors, {})
        self.assertNotIn('client_secret', vals)

    def test_form_values_new(self):
        Tenant = self._portal()
        vals, errors, others = Tenant._portal_form_values('new', dict(NEW, culture=''))
        self.assertEqual((errors, others), ({}, []))
        self.assertEqual((vals['domain'], vals['country_id']),
                         ('portaladatum.onmicrosoft.com', self.env.ref('base.in').id))
        # Blank culture: the default of the Partner Center account
        self.assertNotIn('culture', vals)
        self.assertEqual(Tenant._portal_form_defaults('new'),
                         {'culture': 'en-IN', 'language': 'en'})
        # The checks of the Check button
        vals, errors, others = Tenant._portal_form_values('new', dict(
            NEW, contact_email='not-an-email', contact_first_name='',
            organization_registration_number='', country_id='XX'))
        self.assertEqual(set(errors), {'contact_first_name', 'country_id'})
        self.assertEqual(others, ['"not-an-email" is not a valid email address.'])
        vals, errors, others = Tenant._portal_form_values(
            'new', dict(NEW, organization_registration_number='', domain='bad_domain'))
        self.assertEqual(len(others), 2)
        self.assertIn('Microsoft requires the organization registration number for India.',
                      others)

    def test_new_tenant(self):
        Tenant = self._portal()
        vals, _errors, _others = Tenant._portal_form_values('new', NEW)
        tenant = Tenant._portal_create([dict(vals, tenant_type='new')])
        self.assertEqual((tenant.partner_account_id, tenant.culture, tenant.provision_state),
                         (self.account, 'en-IN', 'draft'))
        self.assertEqual(tenant._portal_operations(), {'edit', 'check', 'remove'})
        calls, pc_request = partner_center(domain_status=200)
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            tenant._portal_check()
        self.assertEqual(tenant.provision_state, 'draft')
        self.assertIn('already used', tenant.provision_error)
        self.assertEqual(tenant._portal_status(), ('To Check', 'warning'))
        # Corrected: a new domain is checked again
        vals, errors, others = Tenant._portal_form_values(
            'new', dict(NEW, domain='portal-adatum-2'), tenant)
        self.assertEqual((errors, others), ({}, []))
        tenant._portal_save(vals)
        self.assertEqual(tenant.domain, 'portal-adatum-2.onmicrosoft.com')
        calls, pc_request = partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            tenant._portal_check()
        self.assertEqual((tenant.provision_state, tenant._portal_status()),
                         ('ready', ('Ready', 'primary')))
        # Created by the scheduled action of the backend; the audit log names the user
        self.assertEqual(tenant._portal_create_in_microsoft(), tenant)
        self.assertEqual((tenant.provision_state, tenant.queued_by), ('queued', self.owner))
        self.assertTrue(tenant._portal_busy())
        self.assertEqual(tenant._portal_operations(), set())
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self.env['m365.tenant']._cron_provision_tenants()
        self.assertEqual((tenant.provision_state, tenant.admin_username, tenant.state),
                         ('done', 'admin@portal-adatum-2.onmicrosoft.com', 'draft'))
        log = self.env['m365.audit.log'].search([('tenant_id', '=', tenant.id),
                                                 ('action', '=', 'provision_tenant')])
        self.assertEqual((log.status, log.user_id), ('success', self.owner))
        # Tested once its app registration is entered
        self.assertEqual(tenant._portal_operations(), {'connect', 'password'})
        tenant._portal_clear_password()
        self.assertFalse(tenant.sudo().admin_password)
        # Connected with its app registration
        vals, errors, _others = Tenant._portal_form_values(
            'connection', {'client_id': CLIENT, 'client_secret': 'new-secret'}, tenant)
        self.assertEqual(errors, {})
        tenant._portal_save(vals)
        with patch(GRAPH_PATH, side_effect=graph_ok):
            tenant._portal_test_connection()
        self.assertEqual(tenant._portal_status(), ('Connected', 'success'))
        log = self.env['m365.audit.log'].search([('tenant_id', '=', tenant.id),
                                                 ('action', '=', 'test_connection')])
        self.assertEqual((log.status, log.user_id), ('success', self.owner))

    def test_failed_creation(self):
        Tenant = self._portal()
        vals, _errors, _others = Tenant._portal_form_values('new', NEW)
        tenant = Tenant._portal_create([dict(vals, tenant_type='new')])
        tenant.sudo().write({'provision_state': 'failed', 'pc_request_id': 'req-1'})
        # It may exist in Microsoft 365: retried, not removed
        self.assertEqual(tenant._portal_operations(), {'edit', 'create', 'reset'})
        tenant.sudo().pc_request_id = False
        self.assertIn('remove', tenant._portal_operations())
        tenant._portal_reset()
        self.assertEqual(tenant.provision_state, 'draft')
        tenant._portal_remove()
        self.assertFalse(tenant.active)
        self.assertFalse(Tenant.search([('id', '=', tenant.id)]))

    def test_existing_tenant(self):
        tenant = self._portal()._portal_create([dict(EXISTING, tenant_type='existing')])
        self.assertEqual(tenant._portal_operations(), {'connect', 'test', 'remove'})
        with patch(GRAPH_PATH, side_effect=graph_denied):
            tenant._portal_test_connection()
        self.assertEqual(tenant._portal_status(), ('Connection Error', 'danger'))
        self.assertIn('bad secret', tenant.last_error)
        with patch(GRAPH_PATH, side_effect=graph_ok):
            tenant._portal_test_connection()
        self.assertEqual(tenant.state, 'connected')

    def test_background_jobs(self):
        Tenant = self._portal()
        vals, _errors, _others = Tenant._portal_form_values('new', NEW)
        new = Tenant._portal_create([dict(vals, tenant_type='new', name='Portal Adatum %s' % i,
                                          domain='portal-adatum-%s.onmicrosoft.com' % i)
                                     for i in range(3)])
        existing = Tenant._portal_create([dict(EXISTING, tenant_type='existing')])
        new._portal_queue('check')
        existing._portal_queue('test')
        self.assertEqual(new[0]._portal_status(), ('Checking…', 'info'))
        self.assertEqual(new[0]._portal_operations(), set())
        cron = self.env.ref('m365_license_management.ir_cron_m365_portal_jobs')
        Trigger = self.env['ir.cron.trigger']
        triggers = Trigger.search_count([('cron_id', '=', cron.id)])
        _calls, pc_request = partner_center()
        with patch.object(m365_tenant_portal, 'PORTAL_JOB_BATCH', 2), \
                patch(PC_PATH, autospec=True, side_effect=pc_request), \
                patch(GRAPH_PATH, side_effect=graph_ok):
            self.env['m365.tenant']._cron_portal_jobs()
            self.assertEqual(new.mapped('provision_state'), ['ready', 'ready', 'draft'])
            self.assertEqual((new[2] | existing).mapped('portal_job'), ['check', 'test'])
            self.assertEqual(Trigger.search_count([('cron_id', '=', cron.id)]), triggers + 1)
            self.env['m365.tenant']._cron_portal_jobs()
        self.assertEqual(new.mapped('provision_state'), ['ready'] * 3)
        self.assertFalse((new | existing).filtered('portal_job'))
        self.assertEqual(existing.state, 'connected')
        log = self.env['m365.audit.log'].search([('tenant_id', '=', existing.id),
                                                 ('action', '=', 'test_connection')])
        self.assertEqual(log.user_id, self.owner)

    def test_import(self):
        Tenant = self._portal()
        content = (EXISTING_HEADER
                   + 'Bulk A,bulk-a.com,%s,%s,secret-a\n' % (TENANT_1, CLIENT)
                   + 'Bulk B,bulk-b.com,%s,%s,\n' % (TENANT_3, CLIENT)).encode()
        wizard = Tenant._portal_import_wizard('existing', content, 'tenants.csv')
        self.assertEqual(wizard.create_uid, self.owner)
        rows = Tenant._portal_parse(wizard)
        self.assertEqual([row['errors'] for row in rows], [[], []])
        # The columns of the template only
        content = (EXISTING_HEADER.rstrip('\n') + ',Enable License Operations\n'
                   + 'Bulk A,bulk-a.com,%s,%s,secret-a,no\n' % (TENANT_1, CLIENT)).encode()
        with self.assertRaisesRegex(UserError, 'cannot be imported on the portal: '
                                               'Enable License Operations'):
            Tenant._portal_parse(Tenant._portal_import_wizard('existing', content, 'a.csv'))
        self.env['ir.config_parameter'].sudo().set_param('m365.portal_import_max_rows', '1')
        with self.assertRaisesRegex(UserError, 'The file has 2 tenants: at most 1'):
            Tenant._portal_parse(wizard)
        # New tenants: the Partner Center account of the portal
        content = (NEW_HEADER + 'Bulk C,bulkc,Adele,Vance,adele@c.com,1 MG Road,Bengaluru,'
                                '560001,IN,U72900KA2020PTC000000\n').encode()
        wizard = Tenant._portal_import_wizard('new', content, 'Customers.csv')
        rows = Tenant._portal_parse(wizard)
        self.assertEqual((rows[0]['errors'], rows[0]['vals']['partner_account_id']),
                         ([], self.account.id))
        content, filename = Tenant._portal_import_template('new')
        self.assertEqual(filename, 'm365_customers_template.xlsx')
        sheet = openpyxl.load_workbook(io.BytesIO(content)).active
        self.assertEqual(sheet['A1'].value, 'CompanyName')


@tagged('post_install', '-at_install')
class TestM365PortalHttp(HttpCase, M365PortalCase):

    def _open(self, url, **kwargs):
        # The requests run in another thread on the cursor of the test
        self.env.flush_all()
        resp = self.url_open(url, allow_redirects=False, **kwargs)
        self.env.invalidate_all()
        return resp

    def _post(self, url, data=None, files=None):
        data = dict(data or {}, csrf_token=Request.csrf_token(self))
        return self._open(url, data=data, files=files)

    def _login(self, user=None):
        user = user or self.owner
        self.authenticate(user.login, user.login)

    def assertRedirect(self, resp, url):
        # Not redirected: the errors of the form
        errors = resp.text[resp.text.find('alert-danger'):][:500]
        self.assertEqual(resp.status_code, 303, errors)
        self.assertTrue(resp.headers['Location'].endswith(url), resp.headers['Location'])

    def test_access(self):
        self.assertIn('/web/login', self._open(URL).headers['Location'])
        # Portal users without the self-service
        self._login(self.plain)
        self.assertRedirect(self._open(URL), '/my')
        self.assertNotIn('/my/m365/tenants', self._open('/my').text)
        self._login()
        self.assertIn('/my/m365/tenants', self._open('/my').text)
        resp = self._open(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('Other Fabrikam', resp.text)
        # The tenants of the other users do not exist
        tenant_url = self.other_tenant._portal_url()
        self.assertEqual(self._open(tenant_url).status_code, 404)
        self.assertEqual(self._post(tenant_url + '/remove').status_code, 404)
        self.assertTrue(self.other_tenant.active)

    def test_existing_tenant(self):
        self._login()
        # Errors: shown with the form, nothing created
        resp = self._post(URL + '/new', dict(EXISTING, tenant_type='existing',
                                              tenant_id='not-a-guid'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('is not a valid ID', resp.text)
        self.assertFalse(self._tenants())
        with patch(GRAPH_PATH, side_effect=graph_ok):
            resp = self._post(URL + '/new', dict(EXISTING, tenant_type='existing'))
        tenant = self._tenants()
        self.assertRedirect(resp, tenant._portal_url())
        self.assertEqual((tenant.name, tenant.tenant_type, tenant.state),
                         ('Portal Contoso', 'existing', 'connected'))
        page = self._open(tenant._portal_url()).text
        self.assertIn('Connected to Microsoft 365.', page)
        self.assertNotIn('portal-secret', page)
        # The secret is never sent back
        page = self._open(tenant._portal_url('/connect')).text
        self.assertIn(TENANT_1, page)
        self.assertNotIn('portal-secret', page)
        with patch(GRAPH_PATH, side_effect=graph_denied):
            resp = self._post(tenant._portal_url('/connect'), dict(
                EXISTING, client_secret='', domain='contoso-portal.com'))
        self.assertRedirect(resp, tenant._portal_url())
        self.assertEqual((tenant.domain, tenant.sudo().client_secret, tenant.state),
                         ('contoso-portal.com', 'portal-secret', 'error'))
        self.assertIn('bad secret', self._open(tenant._portal_url()).text)
        self.assertRedirect(self._post(tenant._portal_url('/remove')), URL)
        self.assertFalse(tenant.active)

    def test_new_tenant(self):
        self._login()
        resp = self._open(URL + '/new?tenant_type=new')
        self.assertIn('Save & Check', resp.text)
        _calls, pc_request = partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            resp = self._post(URL + '/new', dict(NEW, tenant_type='new'))
        tenant = self._tenants()
        self.assertRedirect(resp, tenant._portal_url())
        self.assertEqual((tenant.domain, tenant.provision_state),
                         ('portaladatum.onmicrosoft.com', 'ready'))
        self.assertIn('The tenant is ready', self._open(tenant._portal_url()).text)
        # Steps not available in the current status are refused
        resp = self._post(tenant._portal_url('/password'))
        self.assertRedirect(resp, tenant._portal_url())
        self.assertIn('not available', self._open(tenant._portal_url()).text)
        resp = self._post(tenant._portal_url('/create'))
        self.assertRedirect(resp, tenant._portal_url())
        self.assertEqual((tenant.provision_state, tenant.queued_by), ('queued', self.owner))
        self.assertEqual(self._open(tenant._portal_url('/edit')).status_code, 303)
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self.env['m365.tenant']._cron_provision_tenants()
        page = self._open(tenant._portal_url()).text
        self.assertIn('Temp=Pass1', page)
        self.assertRedirect(self._post(tenant._portal_url('/password')), tenant._portal_url())
        self.assertFalse(tenant.sudo().admin_password)
        self.assertNotIn('Temp=Pass1', self._open(tenant._portal_url()).text)

    def test_edit_new_tenant(self):
        self._login()
        _calls, pc_request = partner_center(domain_status=200)
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self._post(URL + '/new', dict(NEW, tenant_type='new'))
        tenant = self._tenants()
        self.assertEqual(tenant.provision_state, 'draft')
        self.assertIn('already used in Microsoft 365', self._open(tenant._portal_url()).text)
        self.assertIn('portaladatum.onmicrosoft.com',
                      self._open(tenant._portal_url('/edit')).text)
        _calls, pc_request = partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            resp = self._post(tenant._portal_url('/edit'), dict(NEW, domain='portaladatum2'))
        self.assertRedirect(resp, tenant._portal_url())
        self.assertEqual((tenant.domain, tenant.provision_state),
                         ('portaladatum2.onmicrosoft.com', 'ready'))

    def test_bulk_existing(self):
        self._login()
        bad = (EXISTING_HEADER + 'Bulk A,bulk-a.com,%s,%s,secret\n' % (TENANT_1, CLIENT)
               + 'Bulk B,bulk-b.com,not-a-guid,%s,\n' % CLIENT)
        resp = self._post(URL + '/import', {'tenant_type': 'existing'},
                          files={'import_file': ('tenants.csv', bad, 'text/csv')})
        self.assertIn('Nothing is imported until every row is valid', resp.text)
        resp = self._post(URL + '/import/submit')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._tenants())
        good = (EXISTING_HEADER + 'Bulk A,bulk-a.com,%s,%s,secret\n' % (TENANT_1, CLIENT)
                + 'Bulk B,bulk-b.com,%s,%s,\n' % (TENANT_3, CLIENT))
        resp = self._post(URL + '/import', {'tenant_type': 'existing'},
                          files={'import_file': ('tenants.csv', good, 'text/csv')})
        self.assertIn('All rows are valid', resp.text)
        self.assertRedirect(self._post(URL + '/import/submit'), URL)
        tenants = self._tenants()
        self.assertEqual(tenants.mapped('name'), ['Bulk A', 'Bulk B'])
        # Tested in the background when the secret is known
        self.assertEqual(tenants.mapped('portal_job'), ['test', False])
        self.assertIn('Add the client secret', self._open(URL).text)
        # Imported once
        self.assertRedirect(self._post(URL + '/import/submit'), URL + '/import')

    def test_bulk_new(self):
        self._login()
        content = (NEW_HEADER + 'Bulk C,bulkc,Adele,Vance,adele@c.com,1 MG Road,Bengaluru,'
                                '560001,IN,U72900KA2020PTC000000\n')
        resp = self._post(URL + '/import', {'tenant_type': 'new'},
                          files={'import_file': ('Customers.csv', content, 'text/csv')})
        self.assertIn('All rows are valid', resp.text)
        self.assertRedirect(self._post(URL + '/import/submit'), URL)
        tenant = self._tenants()
        self.assertEqual((tenant.tenant_type, tenant.partner_account_id, tenant.portal_job),
                         ('new', self.account, 'check'))
        self.assertIn('Checking…', self._open(URL).text)
        _calls, pc_request = partner_center()
        with patch(PC_PATH, autospec=True, side_effect=pc_request):
            self.env['m365.tenant']._cron_portal_jobs()
        self.assertEqual(tenant.provision_state, 'ready')
        # Create Selected (the other user's tenant is ignored)
        resp = self._post(URL + '/batch', {'operation': 'create',
                                           'tenant_ids': [tenant.id, self.other_tenant.id]})
        self.assertRedirect(resp, URL)
        self.assertEqual(tenant.provision_state, 'queued')
        # The template of the files
        resp = self._open(URL + '/import/template?tenant_type=existing')
        self.assertIn('spreadsheetml', resp.headers['Content-Type'])
        self.assertIn('m365_tenant_import_template.xlsx', resp.headers['Content-Disposition'])

    def test_batch(self):
        self._login()
        tenants = self.env['m365.tenant'].with_user(self.owner)._portal_create(
            [dict(EXISTING, tenant_type='existing')])
        resp = self._post(URL + '/batch', {'operation': 'test', 'tenant_ids': tenants.ids})
        self.assertRedirect(resp, URL)
        self.assertEqual(tenants.portal_job, 'test')
        self.assertIn('Testing…', self._open(URL).text)
        self.assertEqual(self._post(URL + '/batch', {'operation': 'unlink'}).status_code, 404)
