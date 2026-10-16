# -*- coding: utf-8 -*-
"""Near real-time synchronisation: Graph delta queries and change notifications.

* **Delta query** (``GET /users/delta``): after the first full read only users that
  changed since the last run are transferred (state kept in ``user_delta_link``).
* **Webhook** (``POST /subscriptions`` on ``users``): Microsoft calls
  ``/m365/webhook/<tenant id>`` when a user (e.g. its licenses) changes. The
  notification schedules a debounced synchronisation of the tenant, so changes
  made directly in the Microsoft 365 admin center appear within about a minute.

``subscribedSkus`` has no change notification; it is re-read on every webhook
triggered run and by the scheduled action (every 30 minutes).

Reference: https://learn.microsoft.com/en-us/graph/change-notifications-overview
           https://learn.microsoft.com/en-us/graph/delta-query-overview
"""
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from .m365_tenant import M365GraphError
from .m365_tenant_graph import USER_SELECT

_logger = logging.getLogger(__name__)

SUBSCRIPTION_DAYS = 28            # Graph allows ~29 days for directory objects
RENEW_BEFORE = timedelta(days=3)
DEBOUNCE_SECONDS = 20
ADMIN_GROUP = 'm365_license_management.group_m365_admin'


def _graph_datetime(value):
    return value.strftime('%Y-%m-%dT%H:%M:%S.000Z')


class M365TenantLive(models.Model):
    _inherit = 'm365.tenant'

    use_delta_sync = fields.Boolean(
        string="Delta Synchronisation", default=True,
        help="Only transfer users changed since the last run (Microsoft Graph delta query).")
    user_delta_link = fields.Text(copy=False, groups=ADMIN_GROUP)

    webhook_enabled = fields.Boolean(string="Webhook Active", readonly=True, copy=False)
    webhook_client_state = fields.Char(copy=False, groups=ADMIN_GROUP)
    webhook_subscription_id = fields.Char(string="Subscription ID", readonly=True, copy=False)
    webhook_expiry = fields.Datetime(string="Subscription Expires", readonly=True, copy=False)
    webhook_last_received = fields.Datetime(string="Last Notification", readonly=True,
                                            copy=False)
    webhook_pending = fields.Boolean(copy=False)
    webhook_url = fields.Char(string="Notification URL", compute='_compute_webhook_url')

    # ------------------------------------------------------------------
    # Delta synchronisation of users
    # ------------------------------------------------------------------
    def _graph_delta(self, start, params=None):
        """Follow a delta query to its end. Returns (items, deltaLink)."""
        self.ensure_one()
        items, link, url, first = [], None, start, True
        while url:
            data = self._graph_request('GET', url, params=params if first else None)
            first = False
            items.extend(data.get('value', []))
            url = data.get('@odata.nextLink')
            link = data.get('@odata.deltaLink') or link
        return items, link

    def _sync_users_from_graph(self):
        if self.use_delta_sync:
            return self._sync_users_delta()
        return super()._sync_users_from_graph()

    def _sync_users_delta(self, _retry=True):
        self.ensure_one()
        saved = self.sudo().user_delta_link
        initial = not saved
        try:
            items, new_link = self._graph_delta(
                saved or '/users/delta',
                None if saved else {'$select': USER_SELECT})
        except M365GraphError as exc:
            # expired / invalid state token -> start over with a full read
            if saved and _retry and ('HTTP 410' in str(exc) or 'syncState' in str(exc)):
                self.sudo().user_delta_link = False
                return self._sync_users_delta(_retry=False)
            raise
        User = self.env['m365.license.user'].sudo()
        sku_map = {s.sku_id: s.id for s in self.env['m365.sku'].sudo().search(
            [('tenant_id', '=', self.id)])}
        existing = {u.graph_id: u for u in User.with_context(active_test=False).search(
            [('tenant_id', '=', self.id)])}
        now = fields.Datetime.now()
        seen = set()
        for item in items:
            gid = item.get('id')
            if not gid:
                continue
            user = existing.get(gid)
            if '@removed' in item:
                if user and user.active:
                    user.write({'active': False, 'last_sync': now})
                continue
            seen.add(gid)
            if not user and not item.get('userPrincipalName'):
                # partial payload for a user we do not know yet -> read it fully
                try:
                    item = self.graph_get_user(gid)
                except M365GraphError:
                    continue
            vals = self._delta_user_values(item, sku_map)
            vals.update({'active': True, 'last_sync': now})
            if user:
                user.write(vals)
            else:
                vals.setdefault('name', item.get('userPrincipalName') or gid)
                existing[gid] = User.create(dict(vals, tenant_id=self.id, graph_id=gid))
        if initial:  # a full read: archive users that no longer exist
            gone = [u.id for gid, u in existing.items() if gid not in seen and u.active]
            if gone:
                User.browse(gone).write({'active': False})
        self.sudo().write({'user_delta_link': new_link or saved,
                           'last_user_sync_date': now})
        return len(items)

    @staticmethod
    def _delta_user_values(item, sku_map):
        """Map only the properties present in a (possibly partial) delta item."""
        mapping = {
            'userPrincipalName': 'user_principal_name', 'mail': 'mail',
            'accountEnabled': 'account_enabled', 'department': 'department',
            'jobTitle': 'job_title', 'usageLocation': 'usage_location',
        }
        vals = {field: item[key] for key, field in mapping.items() if key in item}
        if item.get('displayName'):
            vals['name'] = item['displayName']
        if 'accountEnabled' in vals and vals['accountEnabled'] is None:
            vals['account_enabled'] = True
        if 'assignedLicenses' in item:
            vals['sku_ids'] = [(6, 0, [sku_map[lic['skuId']]
                                       for lic in item['assignedLicenses']
                                       if lic.get('skuId') in sku_map])]
        return vals

    def action_reset_delta(self):
        """Forget the delta state: the next sync performs a full user read."""
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))
        self.sudo().write({'user_delta_link': False})
        return self._notify(_("The next synchronisation will read all users again."),
                            reload=False)

    # ------------------------------------------------------------------
    # Webhook (change notifications)
    # ------------------------------------------------------------------
    @api.depends('use_delta_sync')
    def _compute_webhook_url(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('m365.webhook_base_url') or ICP.get_param('web.base.url')
                or '').rstrip('/')
        for rec in self:
            rec.webhook_url = "%s/m365/webhook/%s" % (base, rec.id) if rec.id else False

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    def _subscription_body(self, expiry):
        return {
            'changeType': 'created,updated,deleted',
            'notificationUrl': self.webhook_url,
            'resource': 'users',
            'expirationDateTime': _graph_datetime(expiry),
            'clientState': self.sudo().webhook_client_state,
        }

    def action_webhook_subscribe(self):
        self._check_admin()
        for tenant in self:
            url = tenant.webhook_url or ''
            if not url.startswith('https://'):
                raise UserError(_(
                    "Microsoft only delivers notifications to a public HTTPS URL. Current "
                    "URL: %s\nSet the system parameter web.base.url (or "
                    "m365.webhook_base_url) to your public https address.", url or '-'))
            if tenant.webhook_subscription_id:
                tenant._webhook_unsubscribe(silent=True)
            tenant.sudo().webhook_client_state = secrets.token_urlsafe(32)
            expiry = fields.Datetime.now() + timedelta(days=SUBSCRIPTION_DAYS)
            try:
                data = tenant._graph_request('POST', '/subscriptions',
                                             json_body=tenant._subscription_body(expiry))
            except M365GraphError as exc:
                tenant._log('webhook', 'failed', str(exc))
                raise
            tenant.write({
                'webhook_enabled': True,
                'webhook_subscription_id': data.get('id'),
                'webhook_expiry': expiry,
            })
            tenant._log('webhook', 'success', _("Subscribed to user change notifications."))
        return self._notify(_("Webhook subscription created."))

    def _webhook_unsubscribe(self, silent=False):
        self.ensure_one()
        sub_id = self.webhook_subscription_id
        try:
            if sub_id:
                self._graph_request('DELETE', '/subscriptions/%s' % sub_id)
        except M365GraphError as exc:
            if 'HTTP 404' not in str(exc) and not silent:
                raise
        self.write({'webhook_enabled': False, 'webhook_subscription_id': False,
                    'webhook_expiry': False})

    def action_webhook_unsubscribe(self):
        self._check_admin()
        for tenant in self:
            tenant._webhook_unsubscribe()
            tenant._log('webhook', 'info', _("Webhook subscription removed."))
        return self._notify(_("Webhook subscription removed."))

    def _webhook_renew(self):
        self.ensure_one()
        expiry = fields.Datetime.now() + timedelta(days=SUBSCRIPTION_DAYS)
        try:
            self._graph_request('PATCH', '/subscriptions/%s' % self.webhook_subscription_id,
                                json_body={'expirationDateTime': _graph_datetime(expiry)})
            self.webhook_expiry = expiry
            self._log('webhook', 'info', _("Subscription renewed."))
        except M365GraphError as exc:
            if 'HTTP 404' not in str(exc):
                raise
            # subscription vanished on the Microsoft side: create a new one
            data = self._graph_request('POST', '/subscriptions',
                                       json_body=self._subscription_body(expiry))
            self.write({'webhook_subscription_id': data.get('id'), 'webhook_expiry': expiry})
            self._log('webhook', 'info', _("Subscription recreated."))

    @api.model
    def _cron_webhook_maintenance(self):
        """Renew subscriptions that are about to expire (daily)."""
        limit = fields.Datetime.now() + RENEW_BEFORE
        for tenant in self.search([('webhook_enabled', '=', True)]):
            if tenant.webhook_expiry and tenant.webhook_expiry > limit:
                continue
            try:
                tenant._webhook_renew()
            except M365GraphError as exc:
                tenant._log('webhook', 'failed', _("Renewal failed: %s", exc))
                tenant._register_failure(str(exc))
            if not config['test_enable']:
                self.env.cr.commit()

    @api.model
    def _webhook_receive(self, tenant_id, notifications):
        """Validate notifications and schedule a debounced synchronisation.

        :return: number of accepted notifications
        """
        tenant = self.sudo().browse(tenant_id).exists()
        if not tenant or not tenant.webhook_enabled or not tenant.webhook_client_state:
            return 0
        accepted = 0
        for note in notifications[:1000]:
            state = note.get('clientState') or ''
            if (note.get('subscriptionId') == tenant.webhook_subscription_id
                    and hmac.compare_digest(state.encode(),
                                            tenant.webhook_client_state.encode())):
                accepted += 1
        if accepted:
            tenant.write({'webhook_pending': True,
                          'webhook_last_received': fields.Datetime.now()})
            cron = self.env.ref('m365_license_management.ir_cron_m365_process_webhooks',
                                raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger(at=datetime.now(timezone.utc).replace(tzinfo=None)
                                     + timedelta(seconds=DEBOUNCE_SECONDS))
        return accepted

    @api.model
    def _cron_process_webhooks(self):
        """Synchronise tenants that received a change notification."""
        for tenant in self.search([('webhook_pending', '=', True)]):
            tenant.webhook_pending = False  # new notifications during the run re-arm it
            if not config['test_enable']:
                self.env.cr.commit()
            try:
                ok, _msgs = tenant._sync_all_safe()
                tenant._log('webhook', 'success' if ok else 'failed',
                            _("Synchronised after change notification."))
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Webhook sync crashed for tenant %s", tenant.id)
            if not config['test_enable']:
                self.env.cr.commit()
