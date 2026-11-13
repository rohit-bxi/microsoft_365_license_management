# -*- coding: utf-8 -*-
"""19.0.1.10.2: subscriptions are no longer bought through Partner Center (disabled): the
scheduled action following the Partner Center orders is deactivated. Its record is
``noupdate``, so the data file alone does not change existing databases."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_cron SET active = FALSE
         WHERE active AND id IN (SELECT res_id FROM ir_model_data
                                  WHERE module = 'm365_license_management'
                                    AND name = 'ir_cron_m365_pc_orders'
                                    AND model = 'ir.cron')
    """)
    _logger.info("Microsoft 365 19.0.1.10.2: %s Partner Center order scheduled action(s) "
                 "deactivated", cr.rowcount)
