# -*- coding: utf-8 -*-
from psycopg2 import IntegrityError

from odoo import Command
from odoo.tests import Form, tagged
from odoo.tools import mute_logger

from .common import M365Common


@tagged('post_install', '-at_install')
class TestCustomerTenantMapping(M365Common):
    """Contoso Ltd and Fabrikam Inc, the customers, and the tenants Contoso and Fabrikam
    without customer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Customer = cls.env['customer.customer']
        cls.contoso = Customer.create({'name': 'Contoso Ltd'})
        cls.fabrikam = Customer.create({'name': 'Fabrikam Inc'})
        cls.fabrikam_tenant = cls.env['m365.tenant'].create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'client_id': 'cid', 'client_secret': 'secret',
        })
        cls.Mapping = cls.env['customer.tenant.mapping']

    def _mapping(self, customer):
        return self.Mapping.search([('customer_id', '=', customer.id)])

    def test_map_tenants(self):
        with Form(self.Mapping) as form:
            form.customer_id = self.contoso
            form.tenant_ids.add(self.tenant)
            form.tenant_ids.add(self.fabrikam_tenant)
        mapping = form.record
        self.assertEqual(mapping.tenant_ids, self.tenant | self.fabrikam_tenant)
        self.assertEqual(mapping.tenant_count, 2)
        self.assertEqual(self.fabrikam_tenant.tenant_customer_id, self.contoso)
        # The cross removes the customer of the tenant
        mapping.tenant_ids = [Command.unlink(self.fabrikam_tenant.id)]
        self.assertFalse(self.fabrikam_tenant.tenant_customer_id)
        self.assertEqual(self.contoso.tenant_ids, self.tenant)
        # "With Tenant" / "Without Tenant" filters
        empty = self.Mapping.create({'customer_id': self.fabrikam.id})
        mappings = mapping | empty
        self.assertEqual(self.Mapping.search([('id', 'in', mappings.ids),
                                              ('tenant_ids', '!=', False)]), mapping)
        self.assertEqual(self.Mapping.search([('id', 'in', mappings.ids),
                                              ('tenant_ids', '=', False)]), empty)

    @mute_logger('odoo.sql_db')
    def test_one_mapping_per_customer(self):
        self.Mapping.create({'customer_id': self.contoso.id})
        with self.assertRaises(IntegrityError):
            self.Mapping.create({'customer_id': self.contoso.id})

    def test_customer_selected_elsewhere(self):
        """A mapping is created for the customer selected on the tenant or the customer."""
        self.tenant.tenant_customer_id = self.contoso
        mapping = self._mapping(self.contoso)
        self.assertEqual(mapping.tenant_ids, self.tenant)
        self.contoso.tenant_ids = [Command.link(self.fabrikam_tenant.id)]
        self.assertEqual(self._mapping(self.contoso), mapping)
        self.assertEqual(mapping.tenant_ids, self.tenant | self.fabrikam_tenant)
        tenant = self.env['m365.tenant'].create({
            'name': 'Northwind', 'domain': 'northwind.com', 'tenant_id': 'tid-northwind',
            'client_id': 'cid', 'client_secret': 'secret',
            'tenant_customer_id': self.fabrikam.id,
        })
        self.assertEqual(self._mapping(self.fabrikam).tenant_ids, tenant)

    def test_delete_mapping(self):
        """Deleting the mapping removes the customer of its tenants, archived ones too."""
        mapping = self.Mapping.create({
            'customer_id': self.contoso.id,
            'tenant_ids': [Command.link(self.tenant.id), Command.link(self.fabrikam_tenant.id)],
        })
        self.fabrikam_tenant.active = False
        mapping.unlink()
        self.assertFalse(self.tenant.tenant_customer_id)
        self.assertFalse(self.fabrikam_tenant.tenant_customer_id)
        self.assertFalse(self._mapping(self.contoso))

    def test_create_missing_all(self):
        """The customers of the existing tenants get their mapping on module update."""
        self.tenant.tenant_customer_id = self.contoso
        self.fabrikam_tenant.write({'tenant_customer_id': self.fabrikam.id, 'active': False})
        # Tenants with their customer before the mappings existed
        self.env.cr.execute("DELETE FROM customer_tenant_mapping")
        self.Mapping.invalidate_model()
        self.Mapping._create_missing_all()
        self.assertEqual(self._mapping(self.contoso).tenant_ids, self.tenant)
        self.assertTrue(self._mapping(self.fabrikam))
