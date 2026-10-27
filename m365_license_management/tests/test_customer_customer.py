# -*- coding: utf-8 -*-
from contextlib import contextmanager
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, new_test_user, tagged
from odoo.tools import mute_logger

from .common import ADMIN_GROUP, USER_GROUP, M365Common


@tagged('post_install', '-at_install')
class TestCustomerCustomer(M365Common):
    """Contoso Ltd, customer of the test tenant, with its contact Megan Bowen, and
    Fabrikam Inc without tenant."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Customer = cls.env['customer.customer']
        cls.contoso = Customer.create({
            'name': 'Contoso Ltd', 'email': 'info@contoso.com', 'vat': 'BE0477472701',
            'category_ids': [Command.create({'name': 'Enterprise'})],
        })
        cls.megan = Customer.create({'name': 'Megan Bowen', 'parent_id': cls.contoso.id})
        cls.fabrikam = Customer.create({'name': 'Fabrikam Inc'})
        cls.customers = cls.contoso | cls.megan | cls.fabrikam
        cls.tenant.tenant_customer_id = cls.contoso

    @contextmanager
    def _raises(self, message):
        """A ``UserError`` containing this message (in a savepoint)."""
        with self.assertRaises(UserError) as cm:
            yield
        self.assertIn(message, str(cm.exception))

    def test_contacts(self):
        self.assertEqual(self.contoso.child_ids, self.megan)
        self.assertEqual(self.contoso.display_name, 'Contoso Ltd')
        self.assertEqual(self.megan.display_name, 'Contoso Ltd, Megan Bowen')
        # The customer is found by its name, reference, email or tax ID
        Customer = self.env['customer.customer']
        for value in ('contoso ltd', 'info@contoso', 'BE0477'):
            self.assertIn(self.contoso, Customer.search([('display_name', 'ilike', value)]))

    def test_tenants(self):
        self.assertEqual(self.contoso.tenant_ids, self.tenant)
        self.assertEqual((self.contoso.tenant_count, self.fabrikam.tenant_count), (1, 0))
        action = self.contoso.action_view_tenants()
        self.assertEqual(action['res_model'], 'm365.tenant')
        self.assertEqual(action['domain'], [('tenant_customer_id', '=', self.contoso.id)])
        self.assertEqual(action['context'], {'default_tenant_customer_id': self.contoso.id})
        # "With Tenant" / "Without Tenant" filters
        Customer = self.env['customer.customer']
        self.assertEqual(Customer.search([('id', 'in', self.customers.ids),
                                          ('tenant_ids', '!=', False)]), self.contoso)
        self.assertEqual(Customer.search([('id', 'in', self.customers.ids),
                                          ('tenant_ids', '=', False)]),
                         self.megan | self.fabrikam)
        # Tenants are also added and removed on the customer (tenant tags)
        self.fabrikam.tenant_ids = [Command.link(self.tenant.id)]
        self.assertEqual(self.tenant.tenant_customer_id, self.fabrikam)
        self.fabrikam.tenant_ids = [Command.unlink(self.tenant.id)]
        self.assertFalse(self.tenant.tenant_customer_id)
        self.assertFalse(self.fabrikam.tenant_ids)

    def test_email_check(self):
        with self._raises('not valid'):
            self.fabrikam.email = 'not an email'
        self.fabrikam.email = 'Sales <sales@fabrikam.com>'

    def test_parent_check(self):
        with self._raises('contact of itself'):
            self.contoso.parent_id = self.megan
        with self._raises('contact of itself'):
            self.contoso.parent_id = self.contoso
        # The tenant belongs to a customer, not to one of its contacts
        Customer = self.env['customer.customer']
        self.assertEqual(Customer.search([('id', 'in', self.customers.ids)]
                                         + self.tenant._fields['tenant_customer_id'].domain),
                         self.contoso | self.fabrikam)

    def test_unlink(self):
        with self._raises('customer of the tenant'):
            self.contoso.unlink()
        # An archived tenant still belongs to its customer
        self.tenant.active = False
        with self._raises('customer of the tenant'):
            self.contoso.unlink()
        self.fabrikam.unlink()
        self.assertFalse(self.fabrikam.exists())

    def test_address_onchange(self):
        california = self.env.ref('base.state_us_5')
        with Form(self.env['customer.customer']) as form:
            form.name = 'Northwind Traders'
            form.state_id = california
            self.assertEqual(form.country_id, california.country_id)
            form.country_id = self.env.ref('base.in')
            self.assertFalse(form.state_id)

    def test_access(self):
        user = new_test_user(self.env, 'm365_customer_user', groups=USER_GROUP)
        admin = new_test_user(self.env, 'm365_customer_admin', groups=ADMIN_GROUP)

        def visible(login):
            return self.env['customer.customer'].with_user(login).search(
                [('id', 'in', self.customers.ids)])

        # Users see the customers mapped to them and their contacts only
        self.assertFalse(visible(user))
        mapping = self.env['m365.user.customer.mapping'].create({
            'user_id': user.id, 'customer_ids': [Command.link(self.contoso.id)]})
        self.assertEqual(mapping.display_name, user.name)
        self.assertEqual(self.contoso.user_mapping_ids, mapping)
        self.assertEqual(visible(user), self.contoso | self.megan)
        # A change of the mapping applies at once
        mapping.customer_ids = [Command.link(self.fabrikam.id)]
        self.assertEqual(mapping.customer_count, 2)
        self.assertEqual(visible(user), self.customers)
        mapping.unlink()
        self.assertFalse(visible(user))
        self.assertEqual(visible(admin), self.customers)
        # Users read, administrators manage
        with self.assertRaises(AccessError):
            self.contoso.with_user(user).name = 'Contoso Group'
        self.contoso.with_user(admin).name = 'Contoso Group'
        self.assertEqual(self.megan.display_name, 'Contoso Group, Megan Bowen')

    def test_one_customer_mapping_per_user(self):
        user = new_test_user(self.env, 'm365_customer_mapped', groups=USER_GROUP)
        Mapping = self.env['m365.user.customer.mapping']
        Mapping.create({'user_id': user.id, 'customer_ids': [Command.link(self.contoso.id)]})
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            Mapping.create({'user_id': user.id})

    def test_portal_access(self):
        """Grant portal access, as a Microsoft 365 administrator (not a settings admin)."""
        admin = new_test_user(self.env, 'm365_customer_portal_admin', groups=ADMIN_GROUP)
        wizard = self.env['customer.portal.wizard'].with_user(admin).with_context(
            active_model='customer.customer', active_ids=self.contoso.ids).create({})
        # The customer and its contacts
        self.assertEqual(wizard.customer_ids, self.contoso | self.megan)
        line, megan_line = (wizard.user_ids.filtered(lambda l, c=customer: l.customer_id == c)
                            for customer in (self.contoso, self.megan))
        self.assertEqual((line.email_state, megan_line.email_state), ('ok', 'ko'))
        with self._raises('no valid email'):
            megan_line.action_grant_access()

        Mail = type(self.env['mail.mail'])
        with patch.object(Mail, 'send', autospec=True):
            action = line.action_grant_access()
        self.assertEqual((action['res_model'], action['res_id']), (wizard._name, wizard.id))
        user = self.contoso.portal_user_id
        self.assertTrue(user._is_portal())
        self.assertEqual((user.name, user.login, user.email),
                         ('Contoso Ltd', 'info@contoso.com', 'info@contoso.com'))
        self.assertTrue(line.is_portal)
        # Invited by email to set the password
        invitation = self.env['mail.mail'].sudo().search(
            [('model', '=', 'res.users'), ('res_id', '=', user.id)])
        self.assertEqual(invitation.recipient_ids, user.partner_id)
        with self._raises('already has the portal access'):
            line.action_grant_access()

        # The email of another user cannot be used
        megan_line.email = 'Info <info@contoso.com>'
        self.assertEqual(megan_line.email_state, 'exist')
        # A new email is saved on the customer and its portal user
        line.email = 'it@contoso.com'
        with patch.object(Mail, 'send', autospec=True):
            line.action_invite_again()
        self.assertEqual((self.contoso.email, user.email), ('it@contoso.com', 'it@contoso.com'))

        line.action_revoke_access()
        # As in the tests of the portal wizard: the modal is reloaded after each button
        line.invalidate_recordset()
        self.assertFalse(user.active)
        self.assertFalse(line.is_portal)
        with self._raises('Grant the portal access'):
            line.action_invite_again()
        # Granted again: the same user is reactivated
        with patch.object(Mail, 'send', autospec=True):
            line.action_grant_access()
        self.assertEqual(self.contoso.portal_user_id, user)
        self.assertTrue(user.active)
        self.assertEqual(self.env['customer.customer'].search(
            [('id', 'in', self.customers.ids), ('portal_user_id.active', '=', True)]),
            self.contoso)
