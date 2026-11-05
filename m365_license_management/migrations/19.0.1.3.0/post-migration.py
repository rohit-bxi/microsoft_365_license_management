# -*- coding: utf-8 -*-
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools.sql import table_exists

_logger = logging.getLogger(__name__)

# m365.tenant.provision column -> m365.tenant field (the tenants to create are now
# m365.tenant records of type New)
FIELDS = {
    'name': 'name', 'domain': 'domain', 'partner_account_id': 'partner_account_id',
    'company_id': 'company_id', 'first_name': 'contact_first_name',
    'last_name': 'contact_last_name', 'email': 'contact_email', 'phone': 'contact_phone',
    'address_line1': 'address_line1', 'address_line2': 'address_line2', 'city': 'city',
    'address_state': 'address_state', 'postal_code': 'postal_code', 'country_id': 'country_id',
    'address_confirmed': 'address_confirmed', 'culture': 'culture', 'language': 'language',
    'organization_registration_number': 'organization_registration_number',
    'enable_gdap': 'enable_gdap', 'state': 'provision_state',
    'address_status': 'address_status', 'error_message': 'provision_error',
    'queued_by': 'queued_by', 'pc_request_id': 'pc_request_id', 'customer_id': 'customer_id',
    'customer_tenant_id': 'tenant_id', 'created_date': 'created_date',
    'admin_username': 'admin_username', 'admin_password': 'admin_password',
}
OLD_TABLES = ('m365_tenant_provision_import_line', 'm365_tenant_provision_import_wizard',
              'm365_tenant_provision')


def migrate(cr, version):
    """Move the tenants to create (m365.tenant.provision) to m365.tenant, type New."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Tenant = env['m365.tenant'].with_context(active_test=False, tracking_disable=True)
    if table_exists(cr, 'm365_tenant_provision'):
        cr.execute("SELECT %s FROM m365_tenant_provision ORDER BY id" % ", ".join(FIELDS))
        rows = cr.dictfetchall()
        registered = set(Tenant.search([]).mapped('tenant_id'))
        vals_list = []
        for row in rows:
            if row['customer_tenant_id'] and row['customer_tenant_id'] in registered:
                # Already registered in Odoo with "Manage Licenses in Odoo"
                _logger.info("Tenant to create %s skipped: tenant %s already in Odoo.",
                             row['domain'], row['customer_tenant_id'])
                continue
            vals = {new: row[old] for old, new in FIELDS.items() if row[old] is not None}
            vals['tenant_type'] = 'new'
            vals_list.append(vals)
        Tenant.create(vals_list)
        _logger.info("%s tenant(s) to create moved to the tenants.", len(vals_list))

    # The scheduled action of m365.tenant.provision (noupdate) is replaced by
    # ir_cron_m365_create_tenants
    cron = env.ref('m365_license_management.ir_cron_m365_provision_tenants',
                   raise_if_not_found=False)
    if cron:
        action = cron.ir_actions_server_id
        cron.unlink()
        action.unlink()
    cr.execute("""
        DELETE FROM ir_model_data
         WHERE module = 'm365_license_management'
           AND name IN ('ir_cron_m365_provision_tenants',
                        'ir_cron_m365_provision_tenants_ir_actions_server')
    """)

    # Odoo does not drop the tables of removed models
    for table in OLD_TABLES:
        cr.execute('DROP TABLE IF EXISTS "%s" CASCADE' % table)
