# -*- coding: utf-8 -*-
"""19.0.1.8.0: bulk provisioning.

* Domains: status from the verification already known.
* Partner Center offers: the catalog is read per country; the offers read so far come from
  the catalog country of their account.
* GDAP roles: accounts still on the former default get Privileged Role Administrator
  (needed to install the shared app in the customers).
"""
import logging

from odoo.addons.m365_license_management.models.m365_tenant_gdap import (
    DEFAULT_GDAP_ROLES, OLD_DEFAULT_GDAP_ROLES)

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("UPDATE m365_tenant_domain SET state = 'verified' WHERE is_verified")
    cr.execute("UPDATE m365_tenant_domain SET state = 'pending' "
               "WHERE NOT COALESCE(is_verified, FALSE)")
    cr.execute("""
        UPDATE m365_pc_offer o SET country = UPPER(COALESCE(a.catalog_country, 'IN'))
          FROM m365_partner_account a
         WHERE o.account_id = a.id AND o.country IS NULL
    """)
    cr.execute("UPDATE m365_partner_account SET gdap_role_ids = %s WHERE gdap_role_ids = %s",
               (DEFAULT_GDAP_ROLES, OLD_DEFAULT_GDAP_ROLES))
    _logger.info("Microsoft 365 19.0.1.8.0: %s Partner Center account(s) get the Privileged "
                 "Role Administrator GDAP role", cr.rowcount)
