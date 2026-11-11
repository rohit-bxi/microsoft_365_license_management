# -*- coding: utf-8 -*-
"""End-to-end provisioning of a customer, step by step:

    Excel/CSV -> Odoo customer -> Microsoft tenant -> connection -> GDAP -> subscriptions
    -> users -> licenses -> verification

Each step has a status on the tenant (Provisioning menu and report). **Next Step** runs
the next step that Odoo can run; the steps that need a person (admin consent, GDAP
approval, order approval, the users file) are reported as such.

License verification (``GET /users/{id}/licenseDetails`` and ``GET /users/{id}``): every
executed license request is checked a few minutes later by a scheduled action (and with
*Verify*): the user exists and is enabled, the license is (or is no longer) assigned, and
its service plans are provisioned.
"""
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
STEP_STATES = [('na', 'Not Needed'), ('pending', 'Pending'), ('action', 'Action Needed'),
               ('done', 'Done'), ('failed', 'Failed')]
STEPS = ('prov_tenant', 'prov_connection', 'prov_gdap', 'prov_subscriptions', 'prov_users',
         'prov_licenses', 'prov_verification')
# Service plan statuses meaning "not provisioned"
PLAN_ERRORS = ('Error', 'ErrorStatus')
PLAN_PENDING = ('PendingInput', 'PendingActivation', 'PendingProvisioning')
VERIFY_AFTER = timedelta(minutes=2)
SUBSCRIPTION_SPLIT_RE = re.compile(r'[;\n]')
SUBSCRIPTION_LINE_RE = re.compile(r'^(?P<offer>.+?)\s*(?:=|\sx\s|\sX\s)\s*(?P<qty>\d+)\s*$')


class M365LicenseRequestVerification(models.Model):
    _inherit = 'm365.license.request'

    verification_state = fields.Selection([
        ('verified', 'Verified'),
        ('pending', 'Provisioning'),
        ('mismatch', 'Mismatch'),
        ('error', 'Not Verified'),
    ], string="Verification", readonly=True, copy=False, index=True,
        help="Result of the check in Microsoft 365 after the request was executed.")
    verification_date = fields.Datetime(string="Verified On", readonly=True, copy=False)
    verification_message = fields.Text(string="Verification Details", readonly=True,
                                       copy=False)

    def _verify(self):
        """Check the result of an executed license request in Microsoft 365."""
        for rec in self.filtered(lambda r: r.state == 'done'
                                 and r.request_type in ('assign', 'remove')):
            tenant, user, sku = rec.tenant_id, rec.user_id, rec.sku_id
            try:
                item = tenant.graph_get_user(user.graph_id)
                details = tenant.graph_get_license_details(user.graph_id)
            except M365GraphError as exc:
                state = 'mismatch' if 'HTTP 404' in str(exc) else 'error'
                rec.write({'verification_state': state, 'verification_message': str(exc),
                           'verification_date': fields.Datetime.now()})
                tenant._log('verify_license', 'failed', "%s: %s" % (rec.name, exc),
                            request=rec)
                continue
            problems, pending = [], []
            if not item.get('accountEnabled', True):
                problems.append(_("The user account is disabled."))
            found = [d for d in details if (d.get('sku_id') or '').lower() == sku.sku_id.lower()]
            if rec.request_type == 'assign':
                if not found:
                    problems.append(_("%s is not assigned.", sku.name))
                for plan in (found[0]['service_plans'] if found else []):
                    status = plan.get('provisioning_status') or ''
                    if status in PLAN_ERRORS:
                        problems.append(_("Service plan %(plan)s: %(status)s.",
                                          plan=plan.get('name'), status=status))
                    elif status in PLAN_PENDING:
                        pending.append(plan.get('name'))
            elif found:
                problems.append(_("%s is still assigned.", sku.name))
            if problems:
                state, message = 'mismatch', " ".join(problems)
            elif pending:
                state = 'pending'
                message = _("Service plans still being provisioned: %s", ", ".join(pending))
            else:
                state = 'verified'
                message = _("User enabled; %(sku)s %(what)s.", sku=sku.name,
                            what=_("assigned") if rec.request_type == 'assign'
                            else _("removed"))
            rec.write({'verification_state': state, 'verification_message': message,
                       'verification_date': fields.Datetime.now()})
            tenant._log('verify_license', 'failed' if state == 'mismatch' else 'success',
                        "%s: %s" % (rec.name, message), request=rec)

    def action_verify(self):
        self._check_admin()
        self._verify()
        return True

    @api.model
    def _cron_verify(self):
        """Verify the executed requests not verified yet, and the ones still provisioning."""
        limit = fields.Datetime.now() - VERIFY_AFTER
        requests = self.search([
            ('state', '=', 'done'), ('request_type', 'in', ('assign', 'remove')),
            ('executed_date', '<', limit),
            '|', ('verification_state', '=', False), ('verification_state', '=', 'pending'),
        ], limit=200)
        requests._verify()


class M365TenantProvisioning(models.Model):
    _inherit = 'm365.tenant'

    requested_subscriptions = fields.Text(
        string="Subscriptions to Buy", copy=False,
        help="One per line or separated by ; as <offer> = <quantity>. The offer is the "
             "catalog item ID or the product name of the Partner Center catalog, e.g. "
             "'Microsoft 365 Business Basic = 10'.")
    prov_tenant = fields.Selection(STEP_STATES, string="Step: Tenant",
                                   compute='_compute_provisioning')
    prov_connection = fields.Selection(STEP_STATES, string="Step: Connection",
                                       compute='_compute_provisioning')
    prov_gdap = fields.Selection(STEP_STATES, string="Step: GDAP", compute='_compute_provisioning')
    prov_subscriptions = fields.Selection(STEP_STATES, string="Step: Subscriptions",
                                          compute='_compute_provisioning')
    prov_users = fields.Selection(STEP_STATES, string="Step: Users", compute='_compute_provisioning')
    prov_licenses = fields.Selection(STEP_STATES, string="Step: Licenses",
                                     compute='_compute_provisioning')
    prov_verification = fields.Selection(STEP_STATES, string="Step: Verification",
                                         compute='_compute_provisioning')
    prov_state = fields.Selection([('done', 'Complete'), ('progress', 'In Progress'),
                                   ('failed', 'Failed')], string="Provisioning",
                                  compute='_compute_provisioning')
    prov_summary = fields.Char(string="Next", compute='_compute_provisioning')

    # ------------------------------------------------------------------
    # Status of the steps
    # ------------------------------------------------------------------
    @api.depends('tenant_type', 'provision_state', 'state', 'gdap_status',
                 'partner_account_id.graph_client_id', 'pc_order_ids.state',
                 'requested_subscriptions', 'sku_ids', 'user_ids', 'request_ids.state',
                 'request_ids.verification_state')
    def _compute_provisioning(self):
        Request = self.env['m365.license.request'].sudo()
        for rec in self:
            sudo = rec.sudo()
            steps = {}
            # 1. Tenant in Microsoft 365
            if rec.tenant_type == 'existing' or rec.provision_state == 'done':
                steps['prov_tenant'] = 'done'
            elif rec.provision_state == 'failed':
                steps['prov_tenant'] = 'failed'
            else:
                steps['prov_tenant'] = 'pending'
            # 2. Connection (Graph)
            steps['prov_connection'] = {'connected': 'done', 'error': 'failed'}.get(
                rec.state, 'pending' if steps['prov_tenant'] != 'done' else 'action')
            # 3. GDAP
            if not sudo.partner_account_id.graph_client_id:
                steps['prov_gdap'] = 'na'
            else:
                steps['prov_gdap'] = {
                    'active': 'done', 'expiring': 'done', 'activating': 'pending',
                    'approvalPending': 'action', 'created': 'action', 'expired': 'failed',
                    'terminated': 'failed', 'terminating': 'failed',
                    'terminationRequested': 'failed',
                }.get(rec.gdap_status, 'pending')
            # 4. Subscriptions
            orders = sudo.pc_order_ids.filtered(lambda o: o.state not in ('cancelled',
                                                                          'rejected'))
            if not orders and not rec.requested_subscriptions:
                steps['prov_subscriptions'] = 'na' if rec.sku_ids else 'pending'
            elif orders.filtered(lambda o: o.state == 'failed'):
                steps['prov_subscriptions'] = 'failed'
            elif orders and all(o.state == 'done' for o in orders):
                steps['prov_subscriptions'] = 'done'
            elif orders.filtered(lambda o: o.state in ('draft', 'submitted')):
                steps['prov_subscriptions'] = 'action'
            else:
                steps['prov_subscriptions'] = 'pending'
            # 5. Users
            steps['prov_users'] = 'done' if rec.user_ids else 'pending'
            # 6. Licenses and 7. verification
            requests = Request.search([('tenant_id', '=', rec.id),
                                       ('request_type', '=', 'assign')])
            if not requests:
                steps['prov_licenses'] = steps['prov_verification'] = 'pending'
            else:
                states = set(requests.mapped('state'))
                steps['prov_licenses'] = ('failed' if 'failed' in states else
                                          'action' if 'submitted' in states else
                                          'done' if 'done' in states else 'pending')
                done = requests.filtered(lambda r: r.state == 'done')
                checks = set(done.mapped('verification_state'))
                steps['prov_verification'] = (
                    'failed' if checks & {'mismatch', 'error'} else
                    'done' if done and checks == {'verified'} else 'pending')
            values = [steps[s] for s in STEPS]
            prov_state = ('failed' if 'failed' in values else
                          'done' if all(v in ('done', 'na') for v in values) else 'progress')
            nxt = next((s for s in STEPS if steps[s] not in ('done', 'na')), False)
            labels = dict(STEP_STATES)
            rec.update(dict(steps, prov_state=prov_state, prov_summary=(
                "%s: %s" % (rec._fields[nxt].string.replace("Step: ", ""), labels[steps[nxt]]) if nxt else False)))

    # ------------------------------------------------------------------
    # Requested subscriptions
    # ------------------------------------------------------------------
    def _parse_requested_subscriptions(self):
        """:return: (list of (offer, quantity), list of errors)"""
        self.ensure_one()
        account = self.sudo().partner_account_id or self._default_partner_account()
        Offer = self.env['m365.pc.offer']
        result, errors = [], []
        for text in filter(None, (t.strip() for t in SUBSCRIPTION_SPLIT_RE.split(
                self.requested_subscriptions or ''))):
            match = SUBSCRIPTION_LINE_RE.match(text)
            if not match:
                errors.append(_("\"%s\": expected <offer> = <quantity>.", text))
                continue
            name, quantity = match.group('offer').strip(), int(match.group('qty'))
            domain = [('account_id', '=', account.id)]
            offers = Offer.search(domain + [('catalog_item_id', '=ilike', name)]) or \
                Offer.search(domain + ['|', ('name', '=ilike', name), ('sku_title', '=ilike', name)])
            if not offers:
                errors.append(_("Offer \"%s\" not found in the Partner Center catalog.", name))
                continue
            # Prefer the monthly term billed monthly (no annual commitment)
            offer = (offers.filtered(lambda o: o.term_duration == 'P1M') or
                     offers.filtered(lambda o: o.billing_cycle == 'monthly') or offers)[:1]
            result.append((offer, quantity))
        return result, errors

    def _order_requested_subscriptions(self):
        """Partner Center order of the requested subscriptions, submitted for approval."""
        self.ensure_one()
        lines, errors = self._parse_requested_subscriptions()
        if errors:
            raise UserError("\n".join(errors))
        if not lines:
            raise UserError(_("No subscription to buy for %s.", self.name))
        account = self.sudo().partner_account_id or self._default_partner_account()
        order = self.env['m365.pc.order'].create({
            'tenant_id': self.id, 'account_id': account.id,
            'reason': _("Provisioning of %s", self.name),
            'line_ids': [(0, 0, {'offer_id': offer.id, 'quantity': qty})
                         for offer, qty in lines],
        })
        order.action_submit()
        return order

    # ------------------------------------------------------------------
    # Next step
    # ------------------------------------------------------------------
    def _provisioning_next(self):
        """Run the next step Odoo can run. :return: (message, action or None)"""
        self.ensure_one()
        step = next((s for s in STEPS if self[s] not in ('done', 'na')), None)
        if not step:
            return _("%s is completely provisioned.", self.name), None
        if step == 'prov_tenant':
            if self.provision_state == 'failed' and not self.pc_request_id:
                self.action_reset_creation()  # nothing created: check the data again
            if self.provision_state == 'draft':
                self.action_check()
                return _("%s checked: see its creation status.", self.name), None
            if self.provision_state in ('ready', 'failed'):
                # Failed with a pending request ID: same ID, no second tenant
                self.action_create()
            return _("%s is being created in Microsoft 365.", self.name), None
        if step == 'prov_connection':
            account = self.sudo().partner_account_id or self._default_partner_account()
            if self.tenant_id and account.graph_client_id and self.connection_mode != 'shared_app':
                return (_("Grant consent of the shared app in %s: a Global Administrator "
                          "signs in on the page that opens.", self.name),
                        self.action_grant_consent())
            self.action_test_connection()
            return _("Connection of %s tested.", self.name), None
        if step == 'prov_gdap':
            status = self._refresh_gdap()
            if status in (False, 'none', 'expired', 'terminated'):
                self.action_request_gdap()
                return _("GDAP relationship requested for %s: send the approval link to "
                         "the customer.", self.name), None
            if status == 'approvalPending':
                return _("GDAP of %s waits for the customer's approval: %s",
                         self.name, self.gdap_approval_url or '-'), None
            return _("GDAP of %(tenant)s: %(status)s.", tenant=self.name,
                     status=status), None
        if step == 'prov_subscriptions':
            orders = self.sudo().pc_order_ids.filtered(
                lambda o: o.state not in ('cancelled', 'rejected'))
            if not orders and self.requested_subscriptions:
                order = self._order_requested_subscriptions()
                return _("Order %s created; it waits for the approval of an administrator.",
                         order.name), None
            failed = orders.filtered(lambda o: o.state == 'failed')
            if failed:
                failed.action_retry()
                return _("Order(s) %s sent again.", ", ".join(failed.mapped('name'))), None
            ordered = orders.filtered(lambda o: o.state == 'ordered')
            ordered._check_progress()
            return _("Subscriptions of %s: waiting for Partner Center or the approval.",
                     self.name), None
        if step in ('prov_users', 'prov_licenses'):
            if step == 'prov_users' or not self.user_ids:
                return (_("Create the users of %s (Bulk Create Users).", self.name),
                        self.env['ir.actions.act_window']._for_xml_id(
                            'm365_license_management.action_m365_user_import'))
            return _("License requests of %s wait for approval or failed: see Requests.",
                     self.name), None
        requests = self.env['m365.license.request'].search([
            ('tenant_id', '=', self.id), ('state', '=', 'done'),
            ('request_type', '=', 'assign')])
        requests._verify()
        return _("Licenses of %s verified.", self.name), None

    def action_provisioning_next(self):
        self._check_admin()
        messages, action = [], None
        for tenant in self:
            try:
                message, tenant_action = tenant._provisioning_next()
            except (M365GraphError, UserError) as exc:
                message, tenant_action = "%s: %s" % (tenant.name, exc), None
            messages.append(message)
            action = action or (tenant_action if len(self) == 1 else None)
        if action:
            return action
        return self._notify("\n".join(messages), reload=True)

    def action_provisioning_report(self):
        return {
            'type': 'ir.actions.act_url',
            'url': '/m365/export/provisioning?tenant_ids=%s' % ','.join(map(str, self.ids)),
            'target': 'download',
        }
