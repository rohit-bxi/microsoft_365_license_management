# -*- coding: utf-8 -*-
import logging
import time
from datetime import timedelta

import requests
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)

SKU_STATUSES = ('Enabled', 'Warning', 'Suspended', 'Deleted', 'LockedOut')


class M365GraphError(UserError):
    """Raised when authentication or a Microsoft Graph call fails."""


class M365Tenant(models.Model):
    _name = 'm365.tenant'
    _description = 'Microsoft 365 Tenant'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, name'

    # ------------------------------------------------------------------
    # Master data
    # ------------------------------------------------------------------
    name = fields.Char(string="Tenant Name", required=True, tracking=True)
    tenant_type = fields.Selection([
        ('existing', 'Existing'),
        ('new', 'New'),
    ], string="Type", default='existing', required=True, tracking=True,
        help="Existing: connect a tenant that already exists in Microsoft 365.\n"
             "New: create the tenant through Microsoft Partner Center first, then connect "
             "it.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    domain = fields.Char(string="Primary Domain", required=True, tracking=True,
                         help="e.g. bxitech.com. For a new tenant, its initial domain, e.g. "
                              "contoso.onmicrosoft.com (\"contoso\" is completed).")
    color = fields.Integer(string="Color Index")
    company_id = fields.Many2one('res.company', string="Company",
                                 default=lambda self: self.env.company)

    # ------------------------------------------------------------------
    # Entra ID app registration / OAuth 2.0
    # ------------------------------------------------------------------
    # Required for existing tenants; a new tenant gets its ID from Microsoft when created
    tenant_id = fields.Char(string="Directory (Tenant) ID", copy=False, tracking=True)
    client_id = fields.Char(string="Application (Client) ID", copy=False)
    client_secret = fields.Char(
        string="Client Secret", copy=False,
        groups='m365_license_management.group_m365_admin',
        help="Client secret value of the Microsoft Entra app registration. "
             "Only visible to Microsoft 365 administrators.")
    authority_url = fields.Char(string="Authority URL",
                                default='https://login.microsoftonline.com', required=True,
                                help="Change only for sovereign clouds, e.g. "
                                     "https://login.microsoftonline.us")
    graph_url = fields.Char(string="Graph API URL", default='https://graph.microsoft.com/v1.0',
                            required=True)
    graph_scope = fields.Char(string="Graph Scope",
                              default='https://graph.microsoft.com/.default', required=True)
    access_token = fields.Char(copy=False, groups='base.group_system')
    token_expiry = fields.Datetime(copy=False, groups='base.group_system')

    # ------------------------------------------------------------------
    # Behaviour
    # ------------------------------------------------------------------
    state = fields.Selection([
        ('draft', 'Not Tested'),
        ('connected', 'Connected'),
        ('error', 'Connection Error'),
    ], string="Status", default='draft', readonly=True, tracking=True, copy=False)
    allow_license_operations = fields.Boolean(
        string="Enable License Operations", default=True, tracking=True,
        help="Allow assignment / removal of licenses through Microsoft Graph. Keep disabled "
             "for read-only (inventory only) integration. Requires write permissions "
             "(LicenseAssignment.ReadWrite.All or User.ReadWrite.All) on the app registration.")
    auto_approve_admins = fields.Boolean(
        string="Skip Approval for Administrators", default=True,
        help="Requests raised by Microsoft 365 administrators are approved automatically.")
    sync_enabled = fields.Boolean(string="Scheduled Synchronisation", default=True)
    sync_users = fields.Boolean(string="Synchronise Users", default=True,
                                help="Requires the User.Read.All application permission.")
    low_availability_percent = fields.Float(
        string="Low Availability Threshold (%)", default=10.0,
        help="Notify administrators when the available licenses of a product drop to this "
             "percentage of the purchased quantity or lower.")
    low_availability_units = fields.Integer(
        string="Low Availability Threshold (Units)", default=0,
        help="Also notify when the available licenses of a product drop to this number "
             "or lower (0 = disabled).")
    last_sync_date = fields.Datetime(string="Last Inventory Sync", readonly=True, copy=False)
    last_user_sync_date = fields.Datetime(string="Last User Sync", readonly=True, copy=False)
    last_error = fields.Text(string="Last Error", readonly=True, copy=False)
    failure_count = fields.Integer(string="Consecutive Sync Failures", readonly=True,
                                   copy=False)

    # ------------------------------------------------------------------
    # Relations / statistics
    # ------------------------------------------------------------------
    sku_ids = fields.One2many('m365.sku', 'tenant_id', string="Subscriptions")
    user_ids = fields.One2many('m365.license.user', 'tenant_id', string="Users")
    request_ids = fields.One2many('m365.license.request', 'tenant_id', string="Requests")
    sku_count = fields.Integer(compute='_compute_stats')
    user_count = fields.Integer(compute='_compute_stats')
    request_count = fields.Integer(compute='_compute_stats')
    total_licenses = fields.Integer(compute='_compute_stats')
    assigned_licenses = fields.Integer(compute='_compute_stats')
    available_licenses = fields.Integer(compute='_compute_stats')

    _tenant_id_uniq = models.Constraint(
        'unique(tenant_id)', 'This Microsoft tenant is already configured.')

    @api.depends('sku_ids.enabled_units', 'sku_ids.consumed_units', 'sku_ids.available_units',
                 'user_ids', 'request_ids')
    def _compute_stats(self):
        for rec in self:
            rec.sku_count = len(rec.sku_ids)
            rec.user_count = len(rec.user_ids)
            rec.request_count = len(rec.request_ids)
            rec.total_licenses = sum(rec.sku_ids.mapped('enabled_units'))
            rec.assigned_licenses = sum(rec.sku_ids.mapped('consumed_units'))
            rec.available_licenses = sum(rec.sku_ids.mapped('available_units'))

    @api.constrains('low_availability_percent')
    def _check_threshold(self):
        for rec in self:
            if not 0 <= rec.low_availability_percent <= 100:
                raise UserError(_("The low availability threshold must be between 0 and 100."))

    @api.constrains('tenant_type', 'tenant_id', 'client_id')
    def _check_connection_ids(self):
        for rec in self.filtered(lambda t: t.tenant_type == 'existing'):
            if not (rec.tenant_id and rec.client_id):
                raise UserError(_("Directory (Tenant) ID and Application (Client) ID are "
                                  "required for the existing tenant %s.", rec.name))

    @api.model
    def _live_domain(self):
        """Tenants that exist in Microsoft 365: existing ones and created new ones."""
        return ['|', ('tenant_type', '=', 'existing'), ('provision_state', '=', 'done')]

    def write(self, vals):
        # Changing credentials invalidates the cached token
        if {'tenant_id', 'client_id', 'client_secret', 'authority_url', 'graph_scope'} & set(vals):
            vals = dict(vals, access_token=False, token_expiry=False, state='draft')
        return super().write(vals)

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------
    def _log(self, action, status, message, request=None, user=None):
        self.ensure_one()
        return self.env['m365.audit.log'].sudo().create({
            'tenant_id': self.id,
            'action': action,
            'status': status,
            'message': message,
            'request_id': request.id if request else False,
            'user_id': (user or self.env.user).id,
        })

    # ------------------------------------------------------------------
    # Microsoft Graph client
    # ------------------------------------------------------------------
    @staticmethod
    def _format_error(resp):
        try:
            data = resp.json()
        except ValueError:
            return "HTTP %s: %s" % (resp.status_code, (resp.text or '')[:300])
        err = data.get('error')
        if isinstance(err, dict):
            return "HTTP %s - %s: %s" % (resp.status_code, err.get('code'), err.get('message'))
        return "HTTP %s - %s: %s" % (
            resp.status_code, data.get('error'), data.get('error_description') or '')

    def _get_access_token(self, force=False):
        """Return a (cached) OAuth 2.0 client-credentials access token."""
        self.ensure_one()
        rec = self.sudo()
        now = fields.Datetime.now()
        if (not force and rec.access_token and rec.token_expiry
                and rec.token_expiry > now + timedelta(minutes=5)):
            return rec.access_token
        if not (rec.tenant_id and rec.client_id and rec.client_secret):
            raise M365GraphError(_("Tenant ID, Client ID and Client Secret are required."))
        url = "%s/%s/oauth2/v2.0/token" % (rec.authority_url.rstrip('/'), rec.tenant_id.strip())
        try:
            resp = requests.post(url, data={
                'client_id': rec.client_id.strip(),
                'client_secret': rec.client_secret.strip(),
                'scope': rec.graph_scope.strip(),
                'grant_type': 'client_credentials',
            }, timeout=30)
        except requests.RequestException as exc:
            raise M365GraphError(_("Unable to reach the Microsoft identity platform: %s", exc))
        if resp.status_code != 200:
            raise M365GraphError(_("Authentication failed. %s", self._format_error(resp)))
        payload = resp.json()
        rec.write({
            'access_token': payload['access_token'],
            'token_expiry': now + timedelta(seconds=int(payload.get('expires_in', 3600))),
        })
        return rec.access_token

    def _graph_request(self, method, path, params=None, json_body=None, paginate=False):
        """Call Microsoft Graph. Handles token refresh, throttling and paging."""
        self.ensure_one()
        base = (self.graph_url or '').rstrip('/')
        url = path if path.startswith('http') else "%s/%s" % (base, path.lstrip('/'))
        results = []
        retries = 0
        refreshed = False
        while url:
            headers = {
                'Authorization': 'Bearer %s' % self._get_access_token(),
                'Content-Type': 'application/json',
            }
            try:
                resp = requests.request(method, url, headers=headers, params=params,
                                        json=json_body, timeout=60)
            except requests.RequestException as exc:
                raise M365GraphError(_("Microsoft Graph request failed: %s", exc))
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._get_access_token(force=True)
                continue
            if resp.status_code in (429, 503, 504) and retries < 4:
                retries += 1
                try:
                    wait = int(resp.headers.get('Retry-After', 2))
                except ValueError:
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                continue
            if resp.status_code >= 400:
                raise M365GraphError(self._format_error(resp))
            data = resp.json() if resp.content else {}
            if not paginate:
                return data
            results.extend(data.get('value', []))
            url = data.get('@odata.nextLink')
            params = None
        return results

    # ------------------------------------------------------------------
    # Synchronisation
    # ------------------------------------------------------------------
    def _sync_inventory(self):
        """Pull GET /subscribedSkus and update SKU masters + snapshots."""
        self.ensure_one()
        items = self._graph_request('GET', '/subscribedSkus', paginate=True)
        Sku = self.env['m365.sku'].sudo()
        Product = self.env['m365.product'].sudo()
        products = {p.sku_part_number: p for p in Product.with_context(active_test=False).search([])}
        existing = {s.sku_id: s for s in Sku.with_context(active_test=False).search(
            [('tenant_id', '=', self.id)])}
        now = fields.Datetime.now()
        returned = set()
        for item in items:
            part = item.get('skuPartNumber') or item['skuId']
            product = products.get(part)
            if not product:
                product = Product.create({
                    'name': part.replace('_', ' ').title(),
                    'sku_part_number': part,
                    'auto_created': True,
                })
                products[part] = product
            prepaid = item.get('prepaidUnits') or {}
            status = item.get('capabilityStatus')
            vals = {
                'sku_part_number': part,
                'product_id': product.id,
                'enabled_units': prepaid.get('enabled', 0),
                'suspended_units': prepaid.get('suspended', 0),
                'warning_units': prepaid.get('warning', 0),
                'consumed_units': item.get('consumedUnits', 0),
                'status': status if status in SKU_STATUSES else 'Enabled',
                'applies_to': item.get('appliesTo'),
                'last_refresh': now,
            }
            sku = existing.get(item['skuId'])
            if sku:
                sku.write(vals)
            else:
                sku = Sku.create(dict(vals, tenant_id=self.id, sku_id=item['skuId']))
                existing[item['skuId']] = sku
            returned.add(item['skuId'])
            sku._record_snapshot(now)
        # Subscriptions that disappeared from the tenant
        for sku_guid, sku in existing.items():
            if sku_guid not in returned and sku.status != 'Deleted':
                sku.write({'status': 'Deleted', 'enabled_units': 0, 'consumed_units': 0,
                           'suspended_units': 0, 'warning_units': 0, 'last_refresh': now})
                sku._record_snapshot(now)
        self.write({'last_sync_date': now, 'state': 'connected', 'last_error': False})
        self._check_low_availability()
        return len(items)

    def _sync_users_from_graph(self):
        """Pull GET /users with assigned licenses."""
        self.ensure_one()
        items = self._graph_request('GET', '/users', params={
            '$select': 'id,displayName,userPrincipalName,mail,accountEnabled,department,'
                       'jobTitle,usageLocation,assignedLicenses',
            '$top': '999',
        }, paginate=True)
        User = self.env['m365.license.user'].sudo()
        sku_map = {s.sku_id: s.id for s in self.env['m365.sku'].sudo().search(
            [('tenant_id', '=', self.id)])}
        existing = {u.graph_id: u for u in User.with_context(active_test=False).search(
            [('tenant_id', '=', self.id)])}
        now = fields.Datetime.now()
        to_create, seen = [], set()
        for item in items:
            seen.add(item['id'])
            lic_ids = [sku_map[lic['skuId']] for lic in item.get('assignedLicenses', [])
                       if lic.get('skuId') in sku_map]
            vals = {
                'name': item.get('displayName') or item.get('userPrincipalName'),
                'user_principal_name': item.get('userPrincipalName'),
                'mail': item.get('mail'),
                'account_enabled': item.get('accountEnabled', True),
                'department': item.get('department'),
                'job_title': item.get('jobTitle'),
                'usage_location': item.get('usageLocation'),
                'active': True,
                'last_sync': now,
                'sku_ids': [(6, 0, lic_ids)],
            }
            user = existing.get(item['id'])
            if user:
                user.write(vals)
            else:
                to_create.append(dict(vals, tenant_id=self.id, graph_id=item['id']))
        if to_create:
            User.create(to_create)
        gone = [u for gid, u in existing.items() if gid not in seen and u.active]
        if gone:
            User.browse([u.id for u in gone]).write({'active': False})
        self.write({'last_user_sync_date': now})
        return len(items)

    def _try_lock(self):
        """Transaction-level lock so a cron run and a manual sync never overlap."""
        self.ensure_one()
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                            (73650, self.id))
        return self.env.cr.fetchone()[0]

    def _sync_all_safe(self):
        """Synchronise inventory (+ users). Never raises; failures are logged.

        :return: (ok: bool, messages: list[str])
        """
        self.ensure_one()
        if not self.filtered_domain(self._live_domain()):
            return False, [_("%s: create the tenant in Microsoft 365 first.", self.name)]
        if not self._try_lock():
            return False, [_("%s: a synchronisation is already running.", self.name)]
        messages, ok = [], True
        try:
            count = self._sync_inventory()
            msg = _("%(count)s subscription(s) synchronised.", count=count)
            self._log('sync_inventory', 'success', msg)
            messages.append("%s: %s" % (self.name, msg))
        except M365GraphError as exc:
            self._register_failure(str(exc))
            self._log('sync_inventory', 'failed', str(exc))
            return False, ["%s: %s" % (self.name, exc)]
        if self.sync_users:
            try:
                count = self._sync_users_from_graph()
                msg = _("%(count)s user(s) synchronised.", count=count)
                self._log('sync_users', 'success', msg)
                messages.append("%s: %s" % (self.name, msg))
            except M365GraphError as exc:
                ok = False
                self.write({'last_error': str(exc)})
                self._log('sync_users', 'failed', str(exc))
                messages.append("%s: %s" % (self.name, exc))
        if ok:
            self.failure_count = 0
        return ok, messages

    FAILURE_ALERT_THRESHOLD = 3

    def _register_failure(self, error):
        """Store the error and alert administrators when failures repeat."""
        self.ensure_one()
        count = self.failure_count + 1
        self.write({'state': 'error', 'last_error': error, 'failure_count': count})
        if count == self.FAILURE_ALERT_THRESHOLD:
            self.message_post(
                body=Markup("<p>%s</p><p>%s</p>") % (
                    escape(_("Synchronisation of tenant %(name)s failed %(count)s times in a "
                             "row.", name=self.name, count=count)), escape(error)),
                subject=_("Microsoft 365 synchronisation failing - %s", self.name),
                partner_ids=self._admin_partners().ids, subtype_xmlid='mail.mt_comment')

    @api.model
    def _cron_sync_all(self):
        tenants = self.search([('sync_enabled', '=', True),
                               ('state', 'in', ('connected', 'error'))])
        for tenant in tenants:
            try:
                tenant._sync_all_safe()
            except Exception:  # unexpected (DB, bug): keep the other tenants running
                self.env.cr.rollback()
                _logger.exception("Microsoft 365 sync crashed for tenant %s", tenant.id)
            if not config['test_enable']:
                self.env.cr.commit()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def _admin_partners(self):
        group = 'm365_license_management.group_m365_admin'
        users = self.env['res.users'].sudo().search([('share', '=', False)])
        return users.filtered(lambda u: u.has_group(group)).partner_id

    def _check_low_availability(self):
        """Alert administrators (once) when a product runs low on licenses."""
        self.ensure_one()
        low = []
        for sku in self.sku_ids:
            is_low = False
            if sku.status == 'Enabled' and sku.enabled_units > 0:
                pct = sku.available_units * 100.0 / sku.enabled_units
                is_low = (pct <= self.low_availability_percent or
                          (self.low_availability_units and
                           sku.available_units <= self.low_availability_units))
            if is_low and not sku.low_alert_sent:
                low.append(sku)
                sku.sudo().low_alert_sent = True
            elif not is_low and sku.low_alert_sent:
                sku.sudo().low_alert_sent = False
        if not low:
            return
        partners = self._admin_partners()
        rows = Markup('').join(
            Markup("<li><b>%s</b>: %s available of %s (%.0f%% utilised)</li>") % (
                escape(s.name), s.available_units, s.enabled_units, s.utilization)
            for s in low)
        body = Markup("<p>%s</p><ul>%s</ul>") % (
            escape(_("Low license availability detected for tenant %s:", self.name)), rows)
        self.message_post(body=body, subject=_("Low license availability - %s", self.name),
                          partner_ids=partners.ids, subtype_xmlid='mail.mt_comment')
        self._log('notification', 'info', _("Low availability alert sent for: %s",
                                            ", ".join(s.name for s in low)))

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _notify(self, message, ntype='success', reload=True):
        params = {
            'title': _("Microsoft 365"),
            'message': message,
            'type': ntype,
            'sticky': ntype == 'danger',
        }
        if reload:
            params['next'] = {'type': 'ir.actions.client', 'tag': 'soft_reload'}
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': params}

    def _probe_permissions(self):
        """Check the Graph permissions actually granted to the app.

        :return: list of human readable warnings (empty = all good)
        """
        self.ensure_one()
        warnings = []
        probes = [
            ('/subscribedSkus', {}, _("license inventory (Organization.Read.All)")),
        ]
        if self.sync_users:
            probes.append(('/users', {'$top': '1', '$select': 'id'},
                           _("user directory (User.Read.All)")))
        for path, params, label in probes:
            try:
                self._graph_request('GET', path, params=params or None)
            except M365GraphError as exc:
                warnings.append(_("No access to the %(what)s: %(err)s", what=label, err=exc))
        return warnings

    def action_test_connection(self):
        messages, ok = [], True
        connected, partial, failed = [], [], []
        # New tenants are connected once created in Microsoft 365
        pending = self - self.filtered_domain(self._live_domain())
        for tenant in self - pending:
            try:
                org = tenant._graph_request('GET', '/organization',
                                            params={'$select': 'id,displayName'}, paginate=True)
                tenant.write({'state': 'connected', 'last_error': False, 'failure_count': 0})
                label = org[0].get('displayName') if org else tenant.name
                msg = _("Connected to Microsoft 365 organisation \"%s\".", label)
                warnings = tenant._probe_permissions()
                if warnings:
                    ok = False
                    msg = "%s %s" % (msg, " ".join(warnings))
                (partial if warnings else connected).append(tenant.name)
                tenant._log('test_connection', 'failed' if warnings else 'success', msg)
                messages.append("%s: %s" % (tenant.name, msg))
            except M365GraphError as exc:
                ok = False
                failed.append(tenant.name)
                tenant.write({'state': 'error', 'last_error': str(exc)})
                tenant._log('test_connection', 'failed', str(exc))
                messages.append("%s: %s" % (tenant.name, exc))
        for tenant in pending:
            ok = False
            messages.append(_("%s: create the tenant in Microsoft 365 first.", tenant.name))
        if len(self) > 1:
            # Bulk test from the list view: one line per result, details in the audit log
            groups = [(_("Connected"), connected),
                      (_("Connected with missing permissions"), partial),
                      (_("Failed"), failed),
                      (_("Not created in Microsoft 365 yet"), pending.mapped('name'))]
            messages = ["%s: %s." % (label, ", ".join(names)) for label, names in groups
                        if names]
            if not ok:
                messages.append(_("Open the tenant or the Audit Logs for the details."))
        return self._notify(" ".join(messages), 'success' if ok else 'danger')

    def action_sync_inventory(self):
        messages, ok = [], True
        for tenant in self:
            t_ok, t_msgs = tenant._sync_all_safe()
            ok = ok and t_ok
            messages.extend(t_msgs)
        return self._notify("\n".join(messages), 'success' if ok else 'danger')

    def action_view_skus(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_sku')
        action['domain'] = [('tenant_id', '=', self.id)]
        action['context'] = {'default_tenant_id': self.id}
        return action

    def action_view_users(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_license_user')
        action['domain'] = [('tenant_id', '=', self.id)]
        action['context'] = {'default_tenant_id': self.id}
        return action

    def action_view_requests(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_license_request')
        action['domain'] = [('tenant_id', '=', self.id)]
        action['context'] = {'default_tenant_id': self.id}
        return action

    @api.model
    def sync_from_dashboard(self, tenant_ids=None):
        """Called by the dashboard "Refresh" button (administrators only)."""
        if not self.env.user.has_group('m365_license_management.group_m365_admin'):
            raise UserError(_("Only Microsoft 365 administrators can refresh the inventory."))
        domain = [('state', 'in', ('connected', 'error', 'draft'))] + self._dashboard_domain()
        if tenant_ids:
            domain.append(('id', 'in', tenant_ids))
        ok, messages = True, []
        for tenant in self.search(domain):
            t_ok, t_msgs = tenant._sync_all_safe()
            ok = ok and t_ok
            messages.extend(t_msgs)
        return {'ok': ok, 'messages': messages}
