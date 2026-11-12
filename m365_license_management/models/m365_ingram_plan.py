# -*- coding: utf-8 -*-
"""Microsoft subscription plans of the Ingram Micro catalog.

Each plan is mapped to its catalogue product (Graph ``skuPartNumber``), so that Odoo
sees when the bought licenses arrive in the tenant. The mapping is suggested by name on
the catalog synchronisation and confirmed by an administrator.
"""
import re

from odoo import _, api, fields, models

from .m365_ingram_account import M365IngramError, term_key

PRICE_BATCH = 50  # products per price and availability call


class M365IngramPlan(models.Model):
    _name = 'm365.ingram.plan'
    _description = 'Ingram Micro Subscription Plan'
    _order = 'name, id'

    account_id = fields.Many2one('m365.ingram.account', string="Ingram Micro Account",
                                 required=True, ondelete='cascade', index=True)
    plan_id = fields.Char(string="Plan ID", required=True, index=True)
    name = fields.Char(string="Plan", required=True)
    group_name = fields.Char(string="Group")
    description = fields.Text()
    term = fields.Char(string="Term", help="Subscription period, e.g. 1 months or 1 year.")
    billing_period = fields.Char(string="Billing")
    ingram_part_number = fields.Char(string="Ingram Part Number")
    vendor_part_number = fields.Char(string="Vendor Part Number")
    resource_id = fields.Char(string="Resource ID")
    min_units = fields.Integer(string="Minimum Units")
    max_units = fields.Integer(string="Maximum Units")
    unit_price = fields.Float(string="Unit Price", digits=(16, 2))
    currency_id = fields.Many2one('res.currency', string="Currency")
    price_date = fields.Datetime(string="Price Read On", readonly=True)
    product_id = fields.Many2one(
        'm365.product', string="Catalogue Product", ondelete='set null',
        help="Product of the licenses in Microsoft 365: used to see when they arrive.")
    product_suggested = fields.Boolean(
        string="Suggested Product", readonly=True,
        help="Matched by name on the catalog synchronisation: check it.")
    term_ids = fields.One2many('m365.ingram.plan.term', 'plan_id', string="Subscription Periods")
    active = fields.Boolean(default=True)
    last_refresh = fields.Datetime(string="Last Refresh", readonly=True)

    _account_plan_uniq = models.Constraint(
        'unique(account_id, plan_id)', 'This plan already exists for the account.')

    @api.depends('name', 'term')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = ("%s [%s]" % (rec.name, rec.term) if rec.term and rec.name
                                else rec.name)

    def write(self, vals):
        if 'product_id' in vals and 'product_suggested' not in vals:
            vals = dict(vals, product_suggested=False)  # chosen by an administrator
        return super().write(vals)

    def action_confirm_product(self):
        self.write({'product_suggested': False})
        return True

    def _check_admin(self):
        self.env['m365.tenant']._check_admin()

    def _set_terms(self, periods):
        """Subscription periods of the catalog (``subscriptionPeriodSummary``)."""
        self.ensure_one()
        Term = self.env['m365.ingram.plan.term'].sudo()
        existing = {t.key: t for t in self.sudo().term_ids}
        seen = set()
        for sequence, period in enumerate(periods or []):
            if not isinstance(period, dict):
                continue
            number, unit = period.get('subscriptionPeriod'), period.get('subscriptionPeriodUnit')
            key = term_key(number, unit)
            if not key or key in seen:
                continue
            seen.add(key)
            if key not in existing:
                existing[key] = Term.create({
                    'plan_id': self.id, 'key': key, 'sequence': sequence,
                    'code': ("%s %s" % (number, unit or '')).strip()})
        Term.browse([t.id for k, t in existing.items() if k not in seen]).unlink()

    def _default_term(self):
        """Period of a new order line: the default period of the account, else the only
        (or first) one of the plan."""
        self.ensure_one()
        terms = self.term_ids
        return terms.filtered(lambda t: t.key == self.account_id.default_term)[:1] or terms[:1]

    # ------------------------------------------------------------------
    # Details and prices
    # ------------------------------------------------------------------
    def _refresh_details(self):
        """Part numbers and unit limits of the plan."""
        for plan in self:
            _status, data = plan.account_id._im_request(
                'GET', '/resellers/v6/catalog/details', params={'planId': plan.plan_id})
            details = (data.get('subscriptionDetails') or [{}])[0] or {}
            option = (details.get('options') or [{}])[0] or {}
            billing = details.get('billingPeriod') or {}
            vals = {
                'ingram_part_number': data.get('ingramPartNumber') or plan.ingram_part_number,
                'vendor_part_number': (data.get('vendorPartNumber') or option.get('vendorPartNumber')
                                       or plan.vendor_part_number),
                'resource_id': option.get('resourceId') or plan.resource_id,
                'last_refresh': fields.Datetime.now(),
            }
            if billing:
                vals['billing_period'] = ("%s %s" % (billing.get('billingPeriod') or '',
                                                     (billing.get('billingPeriodUnit') or '').lower())
                                          ).strip() or False
            for key, field in (('minUnits', 'min_units'), ('maxUnits', 'max_units')):
                try:
                    vals[field] = int(float(option.get(key) or 0))
                except (TypeError, ValueError):
                    pass
            plan.sudo().write(vals)

    def _refresh_prices(self):
        """Prices of the plans (price and availability API), 50 per call."""
        for account in self.account_id:
            plans = self.filtered(lambda p: p.account_id == account)
            for start in range(0, len(plans), PRICE_BATCH):
                batch = plans[start:start + PRICE_BATCH]
                _status, data = account._im_request(
                    'POST', '/resellers/v6/catalog/priceandavailability',
                    params={'includePricing': 'true', 'includeAvailability': 'false'},
                    json_body={'products': [{'planID': p.plan_id, 'quantityRequested': '1'}
                                            for p in batch]})
                items = data if isinstance(data, list) else data.get('products') or [data]
                for plan, item in self._match_price_items(batch, items):
                    plan._write_price(item)

    @staticmethod
    def _match_price_items(plans, items):
        """(plan, P&A item) pairs, matched on the plan ID or the position."""
        by_plan = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for price in item.get('subscriptionPrice') or []:
                if isinstance(price, dict) and price.get('planId'):
                    by_plan[str(price['planId'])] = item
            by_plan.setdefault(('index', index), item)
        for index, plan in enumerate(plans):
            item = by_plan.get(plan.plan_id) or by_plan.get(('index', index))
            if item:
                yield plan, item

    def _write_price(self, item):
        self.ensure_one()
        if str(item.get('productStatusCode') or '').upper() == 'E':
            raise M365IngramError(_("No price for %(plan)s: %(msg)s", plan=self.name,
                                    msg=item.get('productStatusMessage') or '-'))
        price, currency = None, None
        for sub in item.get('subscriptionPrice') or []:
            if str(sub.get('planId') or self.plan_id) != self.plan_id:
                continue
            for option in sub.get('options') or []:
                for pricing in option.get('resourcePricing') or []:
                    if pricing.get('unitPrice') is not None:
                        price, currency = pricing['unitPrice'], pricing.get('currencyCode')
                        break
                if price is not None:
                    break
            if price is not None:
                break
        if price is None:
            pricing = item.get('pricing') or {}
            price, currency = pricing.get('customerPrice'), pricing.get('currencyCode')
        if price is None:
            raise M365IngramError(_("Ingram Micro returned no price for %s.", self.name))
        currency_rec = self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', (currency or '').upper())], limit=1)
        self.sudo().write({'unit_price': float(price), 'currency_id': currency_rec.id or False,
                           'price_date': fields.Datetime.now()})

    def action_refresh(self):
        self._check_admin()
        self._refresh_details()
        self._refresh_prices()
        return self.env['m365.tenant']._notify(_("Plan details and prices refreshed."))


class M365IngramPlanTerm(models.Model):
    """Subscription period a plan can be bought for (1 month, 1 year, 3 years)."""
    _name = 'm365.ingram.plan.term'
    _description = 'Ingram Micro Plan Subscription Period'
    _order = 'plan_id, sequence, id'

    plan_id = fields.Many2one('m365.ingram.plan', string="Plan", required=True,
                              ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    key = fields.Char(string="Period", required=True,
                      help="ISO duration: P1M one month, P1Y one year, P3Y three years.")
    name = fields.Char(compute='_compute_name', store=True)
    months = fields.Integer(compute='_compute_name', store=True)
    code = fields.Char(
        string="Value Sent", required=True,
        help="Value of the vendor field Subscription Period sent to Ingram Micro, as read "
             "from the catalog (e.g. 1 YEARS). Change it if Ingram Micro asks another "
             "format.")

    _plan_key_uniq = models.Constraint('unique(plan_id, key)',
                                       'This period already exists for the plan.')

    @api.depends('key')
    def _compute_name(self):
        for rec in self:
            match = re.match(r'^P(\d+)([MY])$', rec.key or '')
            if not match:
                rec.name, rec.months = rec.key or '', 0
                continue
            number, unit = int(match.group(1)), match.group(2)
            rec.months = number * (12 if unit == 'Y' else 1)
            rec.name = ("%s %s" % (number, _("Year") if number == 1 else _("Years"))
                        if unit == 'Y' else
                        "%s %s" % (number, _("Month") if number == 1 else _("Months")))
