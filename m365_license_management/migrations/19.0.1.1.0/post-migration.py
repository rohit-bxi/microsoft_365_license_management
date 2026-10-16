# -*- coding: utf-8 -*-
def migrate(cr, version):
    """Tighten the default 6 hour synchronisation to 30 minutes (if not customised)."""
    cr.execute("""
        UPDATE ir_cron SET interval_number = 30, interval_type = 'minutes'
         WHERE interval_number = 6 AND interval_type = 'hours'
           AND id = (SELECT res_id FROM ir_model_data
                      WHERE module = 'm365_license_management'
                        AND name = 'ir_cron_m365_sync_tenants' AND model = 'ir.cron')
    """)
