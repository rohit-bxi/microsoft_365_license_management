# -*- coding: utf-8 -*-
"""19.0.1.9.1: license requests replaced by a later executed request on the same user and
license are no longer verified (their result is not expected any more): marked
Superseded, so that old mismatches no longer fail the provisioning steps."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        WITH later AS (
            SELECT r.id, (SELECT l.name FROM m365_license_request l
                           WHERE l.id > r.id AND l.tenant_id = r.tenant_id
                             AND l.user_id = r.user_id AND l.sku_id = r.sku_id
                             AND l.state = 'done' AND l.request_type IN ('assign', 'remove')
                           ORDER BY l.id LIMIT 1) AS name
              FROM m365_license_request r
             WHERE r.request_type IN ('assign', 'remove') AND r.state = 'done')
        UPDATE m365_license_request r
           SET verification_state = 'superseded',
               verification_message = 'Changed afterwards by ' || later.name || ': not checked.'
          FROM later
         WHERE later.id = r.id AND later.name IS NOT NULL
    """)
    _logger.info("Microsoft 365 19.0.1.9.1: %s superseded license request(s)", cr.rowcount)
