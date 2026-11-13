# -*- coding: utf-8 -*-
"""19.0.1.10.1: the creation of new tenants through Partner Center is disabled (no
Partner Center account): every tenant is set to Create Through Ingram Micro, the only
value left. Tenants already created keep their data."""
import logging

from odoo.tools.sql import column_exists

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not column_exists(cr, 'm365_tenant', 'creation_channel'):
        return  # upgraded from before 19.0.1.10.0: the column is created with 'ingram'
    cr.execute("""UPDATE m365_tenant SET creation_channel = 'ingram'
                   WHERE creation_channel IS DISTINCT FROM 'ingram'""")
    _logger.info("Microsoft 365 19.0.1.10.1: %s tenant(s) set to Create Through Ingram "
                 "Micro", cr.rowcount)
