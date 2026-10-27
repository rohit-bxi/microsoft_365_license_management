# -*- coding: utf-8 -*-
import json

import requests

from odoo import Command
from odoo.tests import TransactionCase

GUID_A = '11111111-2222-3333-4444-555555555555'
GUID_B = '66666666-7777-8888-9999-000000000000'
USER_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
USER_ID_2 = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
MODELS = 'odoo.addons.m365_license_management.models'
GRAPH_PATH = MODELS + '.m365_tenant.M365Tenant._graph_request'
ADMIN_GROUP = 'm365_license_management.group_m365_admin'
USER_GROUP = 'm365_license_management.group_m365_user'


def graph_stub(method, path, params=None, json_body=None, paginate=False):
    """Microsoft Graph returning the inventory of the test tenant unchanged."""
    if path == '/subscribedSkus':
        return [{'skuId': GUID_A, 'skuPartNumber': 'SPE_E3', 'capabilityStatus': 'Enabled',
                 'consumedUnits': 4, 'prepaidUnits': {'enabled': 10}}]
    return [] if paginate else {}


def http_response(status=200, payload=None, headers=None):
    """A ``requests.Response`` with a JSON (or raw bytes) body."""
    resp = requests.Response()
    resp.status_code = status
    if isinstance(payload, bytes):
        resp._content = payload
    else:
        resp._content = json.dumps(payload).encode() if payload is not None else b''
    resp.headers.update(headers or {})
    return resp


def map_tenants(user, tenants):
    """Let the Microsoft 365 user see these tenants: map their customers to the user,
    a tenant without customer gets its own."""
    for tenant in tenants.filtered(lambda t: not t.tenant_customer_id):
        tenant.tenant_customer_id = tenants.env['customer.customer'].create(
            {'name': '%s Customer' % tenant.name})
    mapping = tenants.env['m365.user.customer.mapping'].search([('user_id', '=', user.id)])
    if mapping:
        mapping.customer_ids = [Command.link(customer.id)
                                for customer in tenants.tenant_customer_id]
    else:
        map_customers(user, tenants.tenant_customer_id)
    return user


def map_customers(user, customers):
    """Let the Microsoft 365 user see these customers and their tenants."""
    return customers.env['m365.user.customer.mapping'].create({
        'user_id': user.id, 'customer_ids': [Command.set(customers.ids)]})


class M365Common(TransactionCase):
    """A connected tenant with one subscription (4 of 10 licenses assigned) and two
    users: Adele without license and Megan with the subscription."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env['m365.tenant'].create({
            'name': 'Contoso', 'domain': 'contoso.com', 'tenant_id': 'tid-contoso',
            'client_id': 'cid', 'client_secret': 'secret', 'state': 'connected',
            'allow_license_operations': True, 'auto_approve_admins': False,
            # Full user reads (GET /users); delta queries are tested in test_m365_live
            'use_delta_sync': False,
        })
        cls.sku = cls.env['m365.sku'].create({
            'tenant_id': cls.tenant.id, 'sku_id': GUID_A, 'sku_part_number': 'SPE_E3',
            'product_id': cls.env.ref('m365_license_management.m365_product_spe_e3').id,
            'enabled_units': 10, 'consumed_units': 4,
        })
        cls.adele = cls.env['m365.license.user'].create({
            'name': 'Adele Vance', 'tenant_id': cls.tenant.id, 'graph_id': USER_ID,
            'user_principal_name': 'adele.vance@contoso.com', 'usage_location': 'IN',
        })
        cls.megan = cls.env['m365.license.user'].create({
            'name': 'Megan Bowen', 'tenant_id': cls.tenant.id, 'graph_id': USER_ID_2,
            'user_principal_name': 'megan.bowen@contoso.com',
            'sku_ids': [Command.link(cls.sku.id)],
        })

    def _logs(self, action, tenant=None):
        return self.env['m365.audit.log'].search([
            ('tenant_id', '=', (tenant or self.tenant).id), ('action', '=', action)])
