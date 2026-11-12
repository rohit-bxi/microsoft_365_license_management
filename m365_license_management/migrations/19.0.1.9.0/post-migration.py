# -*- coding: utf-8 -*-
"""19.0.1.9.0: subscription periods of the Ingram Micro plans.

The plans read before this version keep their periods only as text (``term``, e.g.
"1 months, 1 year"): the period records are created from it. The next catalog
synchronisation replaces them with the values of Ingram Micro.
"""
import logging
import re

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)
PERIOD_RE = re.compile(r'(\d+)\s*([a-z]+)', re.I)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    plans = env['m365.ingram.plan'].with_context(active_test=False).search(
        [('term', '!=', False), ('term_ids', '=', False)])
    for plan in plans:
        plan._set_terms([{'subscriptionPeriod': n, 'subscriptionPeriodUnit': unit}
                         for n, unit in PERIOD_RE.findall(plan.term or '')])
    _logger.info("Microsoft 365 19.0.1.9.0: periods created for %s Ingram Micro plan(s)",
                 len(plans))
