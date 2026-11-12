# -*- coding: utf-8 -*-
import base64
import io
import string
from unittest.mock import patch

import openpyxl

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from ..models.m365_user_create import SYMBOLS, generate_password
from .common import ADMIN_GROUP, GRAPH_PATH, USER_GROUP, FakeGraph, M365Common, map_tenants

def created_user(body):
    """POST /users answer: the user with a new object ID."""
    if body['userPrincipalName'].startswith('fail'):
        raise M365GraphError("HTTP 400 - Request_BadRequest: Another object with the same "
                             "value for property userPrincipalName already exists.")
    created_user.count += 1
    return {'id': 'dddddddd-0000-0000-0000-%012d' % created_user.count,
            'displayName': body['displayName'], 'userPrincipalName': body['userPrincipalName']}


created_user.count = 0


class UserCreateCommon(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_create_admin', groups=ADMIN_GROUP)
        Domain = cls.env['m365.tenant.domain']
        cls.contoso = Domain.create({'tenant_id': cls.tenant.id, 'name': 'contoso.com',
                                     'is_verified': True, 'is_default': True})
        Domain.create([
            {'tenant_id': cls.tenant.id, 'name': 'contoso.onmicrosoft.com', 'is_verified': True,
             'is_initial': True},
            {'tenant_id': cls.tenant.id, 'name': 'pending.com', 'is_verified': False},
        ])
        cls.tenant.config_profile_id = cls.env['m365.config.profile'].create({
            'name': 'India', 'usage_location_id': cls.env.ref('base.in').id})


@tagged('post_install', '-at_install')
class TestM365UserCreate(UserCreateCommon):

    def _wizard(self, **vals):
        return self.env['m365.user.create.wizard'].with_user(self.admin).create(dict({
            'tenant_id': self.tenant.id, 'given_name': 'Lee', 'surname': 'Gu',
            'full_name': 'Lee Gu', 'user_name': 'lee.gu', 'domain_id': self.contoso.id,
            'usage_location_id': self.env.ref('base.in').id, 'department': 'IT',
        }, **vals))

    def test_password_generator(self):
        passwords = {generate_password() for _i in range(50)}
        self.assertEqual(len(passwords), 50)
        for password in passwords:
            self.assertEqual(len(password), 16)
            for pool in (string.ascii_lowercase, string.ascii_uppercase, string.digits, SYMBOLS):
                self.assertTrue(set(password) & set(pool), password)

    def test_create_user(self):
        graph = FakeGraph({('POST', '/users'): created_user})
        wizard = self._wizard(sku_ids=[self.sku.id], reason='New joiner')
        with patch(GRAPH_PATH, side_effect=graph):
            wizard.action_create()
        body, = graph.bodies('POST', '/users')
        password = body.pop('passwordProfile')
        self.assertEqual(body, {
            'accountEnabled': True, 'displayName': 'Lee Gu', 'mailNickname': 'lee.gu',
            'userPrincipalName': 'lee.gu@contoso.com', 'givenName': 'Lee', 'surname': 'Gu',
            'usageLocation': 'IN', 'department': 'IT',
        })
        self.assertTrue(password['forceChangePasswordNextSignIn'])
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(wizard.password, password['password'])
        user = wizard.user_id
        self.assertEqual((user.user_principal_name, user.usage_location, user.tenant_id),
                         ('lee.gu@contoso.com', 'IN', self.tenant))
        self.assertTrue(user.graph_id.startswith('dddddddd'))
        request = self.env['m365.license.request'].search([('user_id', '=', user.id)])
        self.assertEqual((request.sku_id, request.state, request.reason),
                         (self.sku, 'submitted', 'New joiner'))
        self.assertIn(request.name, wizard.result)
        self.assertEqual(self._logs('create_user').status, 'success')
        wizard.action_done()
        self.assertFalse(wizard.password)

    def test_create_user_checks(self):
        graph = FakeGraph({('POST', '/users'): created_user})
        pending = self.tenant.domain_ids.filtered(lambda d: d.name == 'pending.com')
        cases = [
            ({'domain_id': pending.id}, 'pending.com is not a verified domain'),
            ({'user_name': 'adele.vance'}, 'adele.vance@contoso.com already exists'),
            ({'user_name': 'bad name'}, 'is not a valid user principal name'),
            ({'usage_location_id': False, 'sku_ids': [self.sku.id]},
             'A usage location is required'),
        ]
        with patch(GRAPH_PATH, side_effect=graph):
            for vals, error in cases:
                with self.assertRaisesRegex(ValidationError, error):
                    self._wizard(**vals).action_create()
            self.tenant.allow_license_operations = False
            with self.assertRaisesRegex(M365GraphError, 'License operations are disabled'):
                self._wizard().action_create()
            self.tenant.allow_license_operations = True
            refused = self._wizard(user_name='fail.user')
            refused.action_create()
        # Shown in the wizard: the audit log line is kept
        self.assertEqual(refused.state, 'draft')
        self.assertIn('already exists', refused.error)
        self.assertEqual(self._logs('create_user').status, 'failed')
        self.assertEqual(len(graph.bodies('POST', '/users')), 1)  # the refused one only
        self.assertFalse(self.env['m365.license.user'].search(
            [('user_principal_name', 'in', ('lee.gu@contoso.com', 'fail.user@contoso.com'))]))

    def test_wizard_defaults(self):
        wizard = self.env['m365.user.create.wizard'].with_user(self.admin).new(
            {'tenant_id': self.tenant.id})
        wizard._onchange_tenant_id()
        self.assertEqual(wizard.domain_id, self.contoso)
        self.assertEqual(wizard.usage_location_id.code, 'IN')
        wizard.given_name, wizard.surname = 'Megan', 'Bowen'
        wizard._onchange_names()
        self.assertEqual((wizard.full_name, wizard.user_name), ('Megan Bowen', 'megan.bowen'))

    def _import(self, rows):
        header = ('Tenant', 'User Principal Name', 'Display Name', 'Usage Location', 'Licenses',
                  'Business Justification')
        text = "\n".join(",".join(row) for row in [header] + rows)
        return self.env['m365.user.import.wizard'].with_user(self.admin).create({
            'file': base64.b64encode(text.encode()), 'filename': 'users.csv'})

    def test_bulk_create_users(self):
        wizard = self._import([
            ('tid-contoso', 'lee.gu@contoso.com', 'Lee Gu', 'IN', 'SPE_E3', 'New joiner'),
            ('Contoso', 'alex.wilber@contoso.com', 'Alex Wilber', '', '', ''),
            ('tid-contoso', 'joni@pending.com', 'Joni Sherman', 'India', '', ''),
            ('tid-contoso', 'fail.user@contoso.com', 'Fail User', '', 'UNKNOWN_SKU', ''),
        ])
        wizard.action_verify()
        self.assertEqual(wizard.line_ids.mapped('status'), ['valid', 'valid', 'error', 'error'])
        self.assertIn('pending.com is not a verified domain', wizard.line_ids[2].message)
        self.assertIn('License "UNKNOWN_SKU" not found', wizard.line_ids[3].message)
        with self.assertRaisesRegex(ValidationError, 'No user has been created'):
            wizard.action_execute()

        wizard = self._import([
            ('tid-contoso', 'lee.gu@contoso.com', 'Lee Gu', 'IN', 'SPE_E3', 'New joiner'),
            ('Contoso', 'alex.wilber@contoso.com', 'Alex Wilber', '', '', ''),
            ('tid-contoso', 'fail.user@contoso.com', 'Fail User', '', '', ''),
        ])
        graph = FakeGraph({('POST', '/users'): created_user})
        with patch(GRAPH_PATH, side_effect=graph):
            wizard.action_verify()
            wizard.action_execute()
        self.assertEqual(wizard.line_ids.mapped('status'), ['done', 'done', 'failed'])
        self.assertIn('already exists', wizard.line_ids[2].message)
        lee = wizard.line_ids[0].user_id
        self.assertEqual(self.env['m365.license.request'].search(
            [('user_id', '=', lee.id)]).sku_id, self.sku)
        # Default usage location of the profile
        self.assertEqual(wizard.line_ids[1].user_id.usage_location, 'IN')
        book = openpyxl.load_workbook(io.BytesIO(base64.b64decode(wizard.result_file)))
        rows = list(book.active.iter_rows(min_row=2, values_only=True))
        self.assertEqual([(r[0], r[3]) for r in rows], [
            ('lee.gu@contoso.com', 'done'), ('alex.wilber@contoso.com', 'done'),
            ('fail.user@contoso.com', 'failed')])
        self.assertEqual(rows[0][2], graph.bodies('POST', '/users')[0]['passwordProfile']['password'])
        self.assertFalse(rows[2][2])
        wizard.action_done()
        self.assertFalse(wizard.result_file)

    def test_template(self):
        wizard = self.env['m365.user.import.wizard'].with_user(self.admin).create({})
        action = wizard.action_download_template()
        self.assertIn('m365_create_users_template.xlsx', action['url'])
        book = openpyxl.load_workbook(io.BytesIO(base64.b64decode(wizard.template_file)))
        self.assertEqual(book.active['B1'].value, 'User Principal Name')


@tagged('post_install', '-at_install')
class TestM365UserCreateApi(HttpCase, UserCreateCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = map_tenants(new_test_user(cls.env, 'm365_create_api_user', groups=USER_GROUP),
                               cls.tenant)
        Keys = cls.env['res.users.apikeys']
        cls.admin_key = Keys.with_user(cls.admin).sudo()._generate(None, 'Tests', None)
        cls.user_key = Keys.with_user(cls.user).sudo()._generate(None, 'Tests', None)

    def _post(self, key, body):
        self.env.flush_all()
        resp = self.url_open('/m365/api/v1/tenants/%s/users' % self.tenant.id, json=body,
                             headers={'Authorization': 'Bearer %s' % key})
        self.env.invalidate_all()
        return resp.status_code, resp.json()

    def test_api_create_user(self):
        body = {'user_principal_name': 'lee.gu@contoso.com', 'display_name': 'Lee Gu',
                'licenses': ['SPE_E3'], 'reason': 'New joiner'}
        self.assertEqual(self._post(self.user_key, body)[0], 403)
        self.assertEqual(self._post(self.admin_key, dict(body, licenses=['NOPE']))[0], 400)
        with patch(GRAPH_PATH, side_effect=FakeGraph({('POST', '/users'): created_user})):
            status, data = self._post(self.admin_key, body)
        self.assertEqual(status, 200, data)
        data = data['data']
        self.assertEqual(data['user']['user_principal_name'], 'lee.gu@contoso.com')
        self.assertEqual(data['user']['usage_location'], 'IN')
        self.assertEqual(len(data['password']), 16)
        self.assertEqual([r['state'] for r in data['requests']], ['submitted'])
