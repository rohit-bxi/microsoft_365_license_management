# -*- coding: utf-8 -*-
"""End-to-end provisioning of a customer, step by step:

    Excel/CSV -> Odoo customer -> Microsoft tenant -> GDAP -> Customer Agreement ->
    connection -> custom domains -> subscriptions -> users -> licenses -> verification

Each step has a status on the tenant (Provisioning menu and report). **Next Step** runs
the next step that Odoo can run; the steps that need a person (admin consent, GDAP
approval, order approval, the users file) are reported as such.

**Run Automatically** (or the option of the customer file): a scheduled action runs every
step Odoo can run, as the administrator who started it, and stops only where a person is
needed. A Microsoft error is retried later (5, 15, 60, then 240 minutes); after 5 failed
runs in a row, or an error that needs a person, the automatic run stops and the
administrator gets an activity.

License verification (``GET /users/{id}/licenseDetails`` and ``GET /users/{id}``): every
executed license request is checked a few minutes later by a scheduled action (and with
*Verify*): the user exists and is enabled, the license is (or is no longer) assigned, and
its service plans are provisioned.
"""
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from .m365_tenant import M365GraphError

_logger = logging.getLogger(__name__)

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
STEP_STATES = [('na', 'Not Needed'), ('pending', 'Pending'), ('action', 'Action Needed'),
               ('done', 'Done'), ('failed', 'Failed')]
STEPS = ('prov_tenant', 'prov_gdap', 'prov_agreement', 'prov_connection', 'prov_domains',
         'prov_subscriptions', 'prov_users', 'prov_licenses', 'prov_verification')
# A step runs once the steps it needs are done (or not needed)
STEP_NEEDS = {
    'prov_gdap': ('prov_tenant',),
    'prov_agreement': ('prov_tenant',),
    'prov_connection': ('prov_tenant',),
    'prov_domains': ('prov_connection',),
    'prov_subscriptions': ('prov_agreement',),
    'prov_users': ('prov_connection',),
    'prov_licenses': ('prov_users',),
    'prov_verification': ('prov_licenses',),
}
# Service plan statuses meaning "not provisioned"
PLAN_ERRORS = ('Error', 'ErrorStatus')
PLAN_PENDING = ('PendingInput', 'PendingActivation', 'PendingProvisioning')
VERIFY_AFTER = timedelta(minutes=2)
SUBSCRIPTION_SPLIT_RE = re.compile(r'[;\n]')
SUBSCRIPTION_LINE_RE = re.compile(r'^(?P<offer>.+?)\s*(?:=|\sx\s|\sX\s)\s*(?P<qty>\d+)\s*$')
# Automatic run
AUTO_BATCH = 20
AUTO_MAX_ERRORS = 5
AUTO_RETRY = (5, 15, 60, 240)  # minutes, after 1, 2, 3, 4+ errors in a row
AUTO_WAIT = timedelta(minutes=5)        # something is in progress at Microsoft
AUTO_IDLE = timedelta(minutes=30)       # only steps waiting for a person
DEFAULT_GDAP_WAIT = timedelta(hours=1)  # default relationship of a tenant we created



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
             "'Microsoft 365 Business Basic (no Teams) = 10'. A quantity alone buys the "
             "default offer of the Partner Center account.")
    prov_tenant = fields.Selection(STEP_STATES, string="Step: Tenant",
                                   compute='_compute_provisioning')
    prov_gdap = fields.Selection(STEP_STATES, string="Step: GDAP", compute='_compute_provisioning')
    prov_agreement = fields.Selection(STEP_STATES, string="Step: Agreement",
                                      compute='_compute_provisioning')
    prov_connection = fields.Selection(STEP_STATES, string="Step: Connection",
                                       compute='_compute_provisioning')
    prov_domains = fields.Selection(STEP_STATES, string="Step: Domains",
                                    compute='_compute_provisioning')
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
    # Automatic run
    auto_provision = fields.Boolean(
        string="Run Automatically", copy=False, tracking=True,
        help="A scheduled action runs every provisioning step Odoo can run, as the "
             "administrator below, until the tenant is completely provisioned.")
    prov_user_id = fields.Many2one(
        'res.users', string="Run As", copy=False,
        help="Microsoft 365 administrator in whose name the automatic run acts (orders, "
             "license requests, audit log).")
    prov_approve_orders = fields.Boolean(
        string="Approve Orders Automatically", copy=False, tracking=True,
        help="The Subscriptions to Buy are ordered in Partner Center without another "
             "approval (option of the customer file).")
    prov_waiting = fields.Text(string="Waiting For", readonly=True, copy=False)
    prov_error = fields.Text(string="Automatic Run Error", readonly=True, copy=False)
    prov_attempts = fields.Integer(string="Errors in a Row", readonly=True, copy=False)
    prov_next_run = fields.Datetime(string="Next Automatic Run", readonly=True, copy=False)
    prov_last_run = fields.Datetime(string="Last Automatic Run", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Status of the steps
    # ------------------------------------------------------------------
    @api.depends('tenant_type', 'provision_state', 'state', 'gdap_status',
                 'gdap_assignment_status', 'partner_account_id.graph_client_id',
                 'partner_account_id.gdap_group_id', 'partner_account_id.pc_user_state',
                 'pc_order_ids.state', 'requested_subscriptions', 'prov_approve_orders',
                 'sku_ids', 'user_ids', 'request_ids.state', 'request_ids.verification_state',
                 'mca_accepted', 'mca_date', 'mca_email', 'mca_first_name', 'mca_last_name',
                 'mca_customer_confirmed', 'mca_error', 'custom_domains', 'domain_ids.state',
                 'pending_user_ids.state', 'app_install_state', 'connection_mode',
                 'client_id', 'tenant_id')
    def _compute_provisioning(self):
        Request = self.env['m365.license.request'].sudo()
        for rec in self:
            sudo = rec.sudo()
            account = sudo.partner_account_id
            steps = {}
            # 1. Tenant in Microsoft 365
            if rec.tenant_type == 'existing' or rec.provision_state == 'done':
                steps['prov_tenant'] = 'done'
            elif rec.provision_state == 'failed':
                steps['prov_tenant'] = 'failed'
            else:
                steps['prov_tenant'] = 'pending'
            # 2. GDAP, with the access of the security group of the account
            if not account.graph_client_id:
                steps['prov_gdap'] = 'na'
            else:
                steps['prov_gdap'] = {
                    'active': 'done', 'expiring': 'done', 'activating': 'pending',
                    'approvalPending': 'action', 'created': 'action', 'expired': 'failed',
                    'terminated': 'failed', 'terminating': 'failed',
                    'terminationRequested': 'failed',
                }.get(rec.gdap_status, 'pending')
                if steps['prov_gdap'] == 'done' and account.gdap_group_id:
                    steps['prov_gdap'] = {'active': 'done', 'error': 'failed'}.get(
                        rec.gdap_assignment_status, 'pending')
            # 3. Microsoft Customer Agreement (to buy subscriptions)
            orders = sudo.pc_order_ids.filtered(lambda o: o.state not in ('cancelled',
                                                                          'rejected'))
            if sudo._mca_recorded():
                steps['prov_agreement'] = 'done'
            elif not orders and not rec.requested_subscriptions:
                steps['prov_agreement'] = 'na'
            elif rec.mca_customer_confirmed and account and account._partner_signed_in():
                steps['prov_agreement'] = 'failed' if rec.mca_error else 'pending'
            else:
                steps['prov_agreement'] = 'action'
            # 4. Connection (Graph)
            if rec.state == 'connected':
                steps['prov_connection'] = 'done'
            elif rec.state == 'error':
                steps['prov_connection'] = 'failed'
            elif steps['prov_tenant'] != 'done':
                steps['prov_connection'] = 'pending'
            elif (rec.app_install_state == 'done' or sudo._can_install_shared_app()
                  or (rec.connection_mode == 'own_app' and rec.client_id
                      and sudo.client_secret)):
                steps['prov_connection'] = 'pending'
            else:
                steps['prov_connection'] = 'action'
            # 5. Custom domains
            names = rec._custom_domain_names()
            if not names:
                steps['prov_domains'] = 'na'
            else:
                domains = sudo.domain_ids.filtered(lambda d: d.name in names)
                states = set(domains.mapped('state'))
                steps['prov_domains'] = (
                    'failed' if 'failed' in states else 'action' if 'action' in states else
                    'done' if len(domains) == len(set(names)) and states == {'verified'}
                    else 'pending')
            # 6. Subscriptions
            if not orders and not rec.requested_subscriptions:
                steps['prov_subscriptions'] = 'na' if rec.sku_ids else 'pending'
            elif orders.filtered(lambda o: o.state == 'failed'):
                steps['prov_subscriptions'] = 'failed'
            elif orders and all(o.state == 'done' for o in orders):
                steps['prov_subscriptions'] = 'done'
            elif orders.filtered(lambda o: o.state == 'draft') or (
                    orders.filtered(lambda o: o.state == 'submitted')
                    and not rec.prov_approve_orders):
                steps['prov_subscriptions'] = 'action'
            else:
                steps['prov_subscriptions'] = 'pending'
            # 7. Users
            pending = sudo.pending_user_ids
            if pending:
                states = set(pending.mapped('state'))
                steps['prov_users'] = ('failed' if 'failed' in states else
                                       'pending' if 'waiting' in states else 'done')
            else:
                steps['prov_users'] = 'done' if rec.user_ids else 'pending'
            # 8. Licenses and 9. verification
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
                "%s: %s" % (rec._step_label(nxt), labels[steps[nxt]]) if nxt else False)))

    def _step_label(self, step):
        return self._fields[step].string.replace("Step: ", "")

    def _prov_step_ready(self, step):
        """The steps this step needs are done (or not needed)."""
        self.ensure_one()
        return all(self[need] in ('done', 'na') for need in STEP_NEEDS.get(step, ()))

    # ------------------------------------------------------------------
    # Requested subscriptions
    # ------------------------------------------------------------------
    def _catalog_country(self, account):
        """Country of the catalog the tenant buys from: its own, else the account's."""
        self.ensure_one()
        return ((self.country_id.code or account.catalog_country or 'IN')).upper()

    def _parse_requested_subscriptions(self):
        """:return: (list of (offer, quantity), list of errors)"""
        self.ensure_one()
        account = self.sudo().partner_account_id or self._default_partner_account()
        Offer = self.env['m365.pc.offer']
        country = self._catalog_country(account)
        domain = [('account_id', '=', account.id), ('country', 'in', (country, False))]
        result, errors = [], []
        for text in filter(None, (t.strip() for t in SUBSCRIPTION_SPLIT_RE.split(
                self.requested_subscriptions or ''))):
            if text.isdigit():
                name, quantity = None, int(text)
            else:
                match = SUBSCRIPTION_LINE_RE.match(text)
                if not match:
                    errors.append(_("\"%s\": expected <offer> = <quantity>.", text))
                    continue
                name, quantity = match.group('offer').strip(), int(match.group('qty'))
            if name is None:
                default = account.default_offer_id
                if not default:
                    errors.append(_("\"%s\": choose the default offer of the Partner Center "
                                    "account, or name the offer.", text))
                    continue
                offers = Offer.search(domain + [('product_code', '=', default.product_code),
                                                ('sku_code', '=', default.sku_code)])
                offers = (offers.filtered(lambda o: o.term_duration == default.term_duration
                                          and o.billing_cycle == default.billing_cycle)
                          or offers)
                name = default.display_name
            else:
                offers = Offer.search(domain + [('catalog_item_id', '=ilike', name)]) or \
                    Offer.search(domain + ['|', ('name', '=ilike', name),
                                           ('sku_title', '=ilike', name)])
            if not offers:
                errors.append(_("Offer \"%(offer)s\" not found in the Partner Center catalog "
                                "of %(country)s.", offer=name, country=country))
                continue
            skus = {}
            for offer in offers:
                skus.setdefault((offer.product_code or offer.name,
                                 offer.sku_code or offer.sku_title), offer)
            if len(skus) > 1:
                errors.append(_("\"%(offer)s\" matches several offers: %(list)s. Use the "
                                "catalog item ID.", offer=name, list="; ".join(
                                    "%s (%s)" % (o.display_name, o.catalog_item_id)
                                    for o in skus.values())))
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
    # Steps: each one returns (status, message[, action]); status is
    # 'progress' (done now), 'waiting' (Microsoft is working on it) or
    # 'action' (a person must act)
    # ------------------------------------------------------------------
    def _prov_handle(self, step, interactive=False):
        self.ensure_one()
        result = getattr(self, '_prov_run_%s' % step[len('prov_'):])(interactive)
        return result[0], result[1], (result[2] if len(result) > 2 else None)

    def _prov_account(self):
        return self.sudo().partner_account_id or self._default_partner_account()

    def _prov_run_tenant(self, interactive):
        if self.provision_state == 'failed' and not self.pc_request_id:
            if not interactive:
                return 'action', _("Partner Center refused the creation: %s",
                                   self.provision_error or '-')
            self.action_reset_creation()  # nothing created: check the data again
        if self.provision_state == 'draft':
            self.action_check()
            if self.provision_state != 'ready':
                return 'action', _("Check failed: %s", self.provision_error or '-')
        if self.provision_state == 'failed' and not interactive:
            # No answer from Partner Center: same request ID, no second tenant
            error = self.provision_error
            self.action_create()
            raise M365GraphError(error or _("Tenant creation failed."))
        if self.provision_state in ('ready', 'failed'):
            self.action_create()
        return 'waiting', _("Creation of the tenant in Microsoft 365")

    def _prov_run_gdap(self, interactive):
        status = self._refresh_gdap()
        if status in ('active', 'expiring'):
            assignment = self._ensure_gdap_assignment()
            if assignment in (False, 'active'):
                return 'progress', _("GDAP active.")
            if assignment == 'error':
                return 'action', _("Microsoft could not give the GDAP roles to the security "
                                   "group: check the group and assign it again.")
            return 'waiting', _("Activation of the GDAP access of the security group")
        if status == 'activating':
            return 'waiting', _("Activation of the GDAP relationship")
        if status in ('approvalPending', 'created'):
            return 'action', _("Approval of the GDAP relationship by the customer: %s",
                               self.gdap_approval_url or '-')
        sudo = self.sudo()
        if (status in (False, 'none') and self.tenant_type == 'new' and sudo.enable_gdap
                and sudo.created_date
                and sudo.created_date > fields.Datetime.now() - DEFAULT_GDAP_WAIT):
            return 'waiting', _("Default GDAP relationship of the new tenant")
        self.action_request_gdap()
        if self.gdap_status != 'approvalPending':
            raise M365GraphError(_("GDAP relationship not requested: see the audit log."))
        return 'action', _("Approval of the GDAP relationship by the customer: %s",
                           self.gdap_approval_url or '-')

    def _prov_run_agreement(self, interactive):
        if self.sudo()._mca_recorded():
            return 'progress', _("Customer Agreement recorded.")
        account = self._prov_account()
        if not (account and account._partner_signed_in()):
            return 'action', _("Acceptance of the Microsoft Customer Agreement: record it "
                               "(Purchasing tab), or sign in the partner user")
        if not self.mca_customer_confirmed:
            return 'action', _("Confirmation that the customer accepted the Microsoft "
                               "Customer Agreement")
        try:
            self._attest_mca()
        except UserError as exc:
            self.sudo().mca_error = str(exc)[:250]
            self._log('agreement', 'failed', str(exc))
            raise
        return 'progress', _("Customer Agreement recorded.")

    def _prov_run_connection(self, interactive):
        if self.state == 'connected':
            return 'progress', _("Connected.")
        account = self._prov_account()
        if self.app_install_state == 'done' or self._can_install_shared_app():
            if self.app_install_state != 'done' and self.prov_gdap != 'done':
                # The partner user needs its GDAP roles in the customer first
                return ('action' if self.prov_gdap in ('action', 'failed') else 'waiting',
                        _("GDAP access, before the installation of the shared app"))
            try:
                connected = self._install_shared_app()
            except UserError as exc:  # M365GraphError included
                self.sudo().write({'app_install_state': 'failed',
                                   'app_install_error': str(exc)})
                self._log('consent', 'failed', _("Shared app not installed: %s", exc))
                raise
            if connected:
                return 'progress', _("Connected through the shared app.")
            return 'waiting', _("Microsoft to apply the installation of the shared app")
        sudo = self.sudo()
        if (self.connection_mode == 'own_app' and self.client_id and sudo.client_secret) \
                or (self.connection_mode == 'shared_app' and self.state != 'draft'):
            self.action_test_connection()
            if self.state == 'connected':
                return 'progress', _("Connected.")
            raise M365GraphError(self.last_error or _("Connection failed."))
        if interactive and account and account.graph_client_id and self.tenant_id:
            return ('action', _("Grant consent of the shared app in %s: a Global "
                                "Administrator signs in on the page that opens.", self.name),
                    self.action_grant_consent())
        return 'action', _("Admin consent of the shared app (Grant Consent), or sign in the "
                           "partner user to install it")

    def _ensure_custom_domains(self):
        """Domain records of the custom domains, created To Add when missing."""
        self.ensure_one()
        names = self._custom_domain_names()
        Domain = self.env['m365.tenant.domain'].sudo()
        if names and not self.domain_ids:
            try:
                self._sync_domains()
            except M365GraphError as exc:
                _logger.info("Domains of %s not read: %s", self.name, exc)
        existing = {d.name: d for d in self.sudo().domain_ids}
        for name in names:
            if name not in existing:
                existing[name] = Domain.create({
                    'tenant_id': self.id, 'name': name, 'state': 'to_add',
                    'make_default': self.custom_domain_default and name == names[0]})
        return Domain.browse([existing[name].id for name in names])

    def _prov_run_domains(self, interactive):
        domains = self._ensure_custom_domains()
        now = fields.Datetime.now()
        for domain in domains.filtered(lambda d: d.state in ('to_add', 'pending')
                                       and (not d.next_check or d.next_check <= now)):
            domain._auto_step()
        failed = domains.filtered(lambda d: d.state == 'failed')
        if failed:
            return 'action', _("Domain %(domain)s: %(err)s", domain=failed[0].name,
                               err=failed[0].error or '-')
        late = domains.filtered(lambda d: d.state == 'action')
        if late:
            return 'action', _("TXT record of %s at the DNS host of the customer, then "
                               "Retry", ", ".join(late.mapped('name')))
        waiting = domains.filtered(lambda d: d.state != 'verified')
        if not waiting:
            return 'progress', _("Domains verified.")
        return 'waiting', _("DNS record of %s", ", ".join(
            "%s (TXT %s)" % (d.name, d.txt_record) if d.txt_record else d.name
            for d in waiting))

    def _prov_run_subscriptions(self, interactive):
        Order = self.env['m365.pc.order']
        orders = Order.browse(self.sudo().pc_order_ids.filtered(
            lambda o: o.state not in ('cancelled', 'rejected')).ids)
        if not orders:
            if not self.requested_subscriptions:
                return 'action', _("Subscriptions to buy (Buy Licenses, or Subscriptions to "
                                   "Buy)")
            orders = self._order_requested_subscriptions()
        if orders.filtered(lambda o: o.state == 'draft'):
            return 'action', _("Submit the order(s) %s",
                               ", ".join(orders.filtered(lambda o: o.state == 'draft')
                                         .mapped('name')))
        submitted = orders.filtered(lambda o: o.state == 'submitted')
        if submitted:
            if not self.prov_approve_orders:
                return 'action', _("Approval of the order(s) %s",
                                   ", ".join(submitted.mapped('name')))
            for order in submitted:
                order._log('info', _("approved automatically (option of the customer file "
                                     "imported by %s).", self.env.user.name))
            submitted.action_approve()
        failed = orders.filtered(lambda o: o.state == 'failed')
        if failed:
            failed.action_retry()
            still = failed.filtered(lambda o: o.state == 'failed')
            if still and not interactive:
                raise M365GraphError(still[0].error_message or _("Order failed."))
            if still:
                return 'action', _("Order(s) %s failed again.", ", ".join(still.mapped('name')))
        orders.filtered(lambda o: o.state == 'ordered')._check_progress()
        if all(o.state == 'done' for o in orders):
            return 'progress', _("Subscriptions provisioned.")
        return 'waiting', _("Partner Center to provision the subscriptions")

    def _prov_run_users(self, interactive):
        pending = self.env['m365.tenant.pending.user'].browse(self.sudo().pending_user_ids.ids)
        if not pending:
            if self.user_ids:
                return 'progress', _("Users created.")
            if interactive:
                return ('action', _("Create the users of %s (Bulk Create Users).", self.name),
                        self.env['ir.actions.act_window']._for_xml_id(
                            'm365_license_management.action_m365_user_import'))
            return 'action', _("Users to create (Bulk Create Users)")
        pending._create_waiting()
        failed = pending.filtered(lambda u: u.state == 'failed')
        if failed:
            return 'action', _("User(s) not created: %s",
                               "; ".join("%s: %s" % (u.upn, u.error) for u in failed[:3]))
        waiting = pending.filtered(lambda u: u.state == 'waiting')
        if waiting:
            return 'waiting', _("%(count)s user(s) waiting for: %(reason)s",
                                count=len(waiting), reason=waiting[0].wait_reason or '-')
        return 'progress', _("Users created.")

    def _prov_run_licenses(self, interactive):
        requests = self.env['m365.license.request'].search([
            ('tenant_id', '=', self.id), ('request_type', '=', 'assign')])
        if not requests:
            return 'action', _("License requests for the users")
        submitted = requests.filtered(lambda r: r.state == 'submitted')
        if submitted:
            return 'action', _("Approval of the license request(s) %s",
                               ", ".join(submitted.mapped('name')))
        failed = requests.filtered(lambda r: r.state == 'failed')
        if failed:
            return 'action', _("License request(s) failed: %s",
                               ", ".join(failed.mapped('name')))
        if requests.filtered(lambda r: r.state == 'done'):
            return 'progress', _("Licenses assigned.")
        return 'waiting', _("Assignment of the licenses")

    def _prov_run_verification(self, interactive):
        requests = self.env['m365.license.request'].search([
            ('tenant_id', '=', self.id), ('state', '=', 'done'),
            ('request_type', '=', 'assign')])
        requests.filtered(lambda r: r.verification_state != 'verified')._verify()
        if requests.filtered(lambda r: r.verification_state in ('mismatch', 'error')):
            return 'action', _("Licenses not as requested: see the verification of the "
                               "requests")
        if requests.filtered(lambda r: r.verification_state != 'verified'):
            return 'waiting', _("Provisioning of the service plans")
        return 'progress', _("Licenses verified.")

    # ------------------------------------------------------------------
    # Next step (manual)
    # ------------------------------------------------------------------
    def _provisioning_next(self):
        """Run the next step Odoo can run. :return: (message, action or None)"""
        self.ensure_one()
        todo = [s for s in STEPS if self[s] not in ('done', 'na')]
        if not todo:
            return _("%s is completely provisioned.", self.name), None
        step = next((s for s in todo if self._prov_step_ready(s)), todo[0])
        _status, message, action = self._prov_handle(step, interactive=True)
        return "%s - %s: %s" % (self.name, self._step_label(step), message), action

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

    # ------------------------------------------------------------------
    # Automatic run
    # ------------------------------------------------------------------
    def _schedule_prov_activity(self, summary, note):
        """To-do for the administrator of the automatic run (else every administrator),
        once per summary."""
        self.ensure_one()
        if self.sudo().activity_ids.filtered(lambda a: a.summary == summary):
            return
        users = self.prov_user_id.filtered(lambda u: u.active and u.has_group(ADMIN_GROUP))
        if not users:
            users = self.env['res.users'].sudo().search([('share', '=', False)]).filtered(
                lambda u: u.has_group(ADMIN_GROUP))
        for user in users:
            self.sudo().activity_schedule('mail.mail_activity_data_todo', user_id=user.id,
                                          summary=summary, note=note)

    def _auto_stop(self, message):
        """Stop the automatic run: a person must act."""
        self.ensure_one()
        self.sudo().write({'auto_provision': False, 'prov_error': message,
                           'prov_next_run': False, 'prov_last_run': fields.Datetime.now()})
        self._log('auto_provision', 'failed', _("Automatic provisioning stopped: %s", message),
                  user=self.prov_user_id or None)
        self._schedule_prov_activity(_("Provisioning of %s stopped", self.name), message)

    def _auto_error(self, message):
        """Microsoft error: retried later, stopped after AUTO_MAX_ERRORS in a row."""
        self.ensure_one()
        attempts = self.prov_attempts + 1
        if attempts >= AUTO_MAX_ERRORS:
            self.sudo().prov_attempts = attempts
            return self._auto_stop(_("%(count)s errors in a row, the last one: %(err)s",
                                     count=attempts, err=message))
        now = fields.Datetime.now()
        self.sudo().write({'prov_attempts': attempts, 'prov_error': message,
                           'prov_last_run': now, 'prov_next_run': now + timedelta(
                               minutes=AUTO_RETRY[min(attempts, len(AUTO_RETRY)) - 1])})
        self._log('auto_provision', 'failed', _("Automatic provisioning: %s (retried later)",
                                                message), user=self.prov_user_id or None)

    def _auto_provision_run(self):
        """Run every step Odoo can run, in the name of the administrator of the run."""
        self.ensure_one()
        user = self.prov_user_id
        if not (user and user.active and user.has_group(ADMIN_GROUP)):
            return self._auto_stop(_("The administrator who started the automatic run (%s) "
                                     "is not a Microsoft 365 administrator any more.",
                                     user.name or '-'))
        tenant = self.with_user(user)
        computed = list(STEPS) + ['prov_state', 'prov_summary']
        waiting, statuses, step = {}, set(), None
        try:
            for _pass in range(3):
                progressed = False
                for step in STEPS:
                    tenant.invalidate_recordset(computed)
                    if tenant[step] in ('done', 'na') or not tenant._prov_step_ready(step):
                        continue
                    status, message, _action = tenant._prov_handle(step)
                    if status == 'progress':
                        progressed = True
                        waiting.pop(step, None)
                    else:
                        waiting[step] = message
                        statuses.add(status)
                if not progressed:
                    break
        except M365GraphError as exc:
            return self._auto_error("%s: %s" % (self._step_label(step), exc))
        except (UserError, AccessError) as exc:
            return self._auto_stop("%s: %s" % (self._step_label(step), exc))
        tenant.invalidate_recordset(computed)
        now = fields.Datetime.now()
        if tenant.prov_state == 'done':
            self.sudo().write({'auto_provision': False, 'prov_waiting': False,
                               'prov_error': False, 'prov_attempts': 0, 'prov_next_run': False,
                               'prov_last_run': now})
            self._log('auto_provision', 'success', _("Provisioning complete."), user=user)
            return True
        self.sudo().write({
            'prov_waiting': "\n".join("%s: %s" % (self._step_label(s), message)
                                      for s, message in waiting.items()) or False,
            'prov_error': False, 'prov_attempts': 0, 'prov_last_run': now,
            'prov_next_run': now + (AUTO_IDLE if statuses == {'action'} else AUTO_WAIT),
        })
        return True

    @api.model
    def _cron_auto_provision(self):
        now = fields.Datetime.now()
        tenants = self.sudo().search([
            ('auto_provision', '=', True),
            '|', ('prov_next_run', '=', False), ('prov_next_run', '<=', now),
        ], order='prov_next_run, id', limit=AUTO_BATCH + 1)
        for tenant in tenants[:AUTO_BATCH]:
            try:
                tenant._auto_provision_run()
            except Exception:  # unexpected (DB, bug): keep the other tenants running
                self.env.cr.rollback()
                _logger.exception("Automatic provisioning crashed for tenant %s", tenant.id)
                tenant.write({'prov_error': _("Unexpected error: see the server log."),
                              'prov_next_run': now + timedelta(hours=1)})
            if not config['test_enable']:
                self.env.cr.commit()
        if len(tenants) > AUTO_BATCH:
            self.env.ref('m365_license_management.ir_cron_m365_auto_provision')._trigger()

    def action_auto_provision_start(self):
        self._check_admin()
        todo = self.filtered(lambda t: t.prov_state != 'done')
        todo.sudo().write({'auto_provision': True, 'prov_user_id': self.env.uid,
                           'prov_attempts': 0, 'prov_error': False, 'prov_next_run': False})
        for tenant in todo:
            tenant._log('auto_provision', 'info', _("Automatic provisioning started by %s.",
                                                    self.env.user.name))
        cron = self.env.ref('m365_license_management.ir_cron_m365_auto_provision',
                            raise_if_not_found=False)
        if cron and todo:
            cron.sudo()._trigger()
        return self._notify(_("%s tenant(s) are provisioned automatically from now; the "
                              "first run starts within a few minutes.", len(todo)))

    def action_auto_provision_stop(self):
        self._check_admin()
        running = self.filtered('auto_provision')
        running.sudo().write({'auto_provision': False, 'prov_next_run': False})
        for tenant in running:
            tenant._log('auto_provision', 'info', _("Automatic provisioning stopped by %s.",
                                                    self.env.user.name))
        return self._notify(_("Automatic provisioning stopped for %s tenant(s).",
                              len(running)))
