# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from ..models.m365_tenant_graph import USER_SELECT
from .common import GRAPH_PATH, GUID_A, USER_GROUP, USER_ID


@tagged('post_install', '-at_install')
class TestM365Live(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = cls.env['m365.tenant'].create({
            'name': 'Live', 'domain': 'live.com', 'tenant_id': 'tid2',
            'client_id': 'cid', 'client_secret': 's', 'state': 'connected'})
        cls.sku = cls.env['m365.sku'].create({
            'tenant_id': cls.tenant.id, 'sku_id': GUID_A, 'sku_part_number': 'X'})

    def _isolate(self, fname):
        """Leave the tenants of the database out of the scheduled actions."""
        (self.env['m365.tenant'].search([(fname, '=', True)]) - self.tenant)[fname] = False

    # ------------------------------------------------------------------
    # Delta synchronisation
    # ------------------------------------------------------------------
    def test_delta_initial_then_incremental_and_removed(self):
        calls = []

        def graph(method, path, params=None, **kw):
            calls.append(path)
            if path == '/users/delta':
                return {'value': [{'id': USER_ID, 'displayName': 'Jane',
                                   'userPrincipalName': 'j@live.com', 'accountEnabled': True,
                                   'assignedLicenses': [{'skuId': GUID_A}]}],
                        '@odata.deltaLink': 'https://graph/delta?token=1'}
            if 'token=1' in path:
                return {'value': [{'id': USER_ID, 'assignedLicenses': []}],
                        '@odata.deltaLink': 'https://graph/delta?token=2'}
            return {'value': [{'id': USER_ID, '@removed': {'reason': 'deleted'}}],
                    '@odata.deltaLink': 'https://graph/delta?token=3'}

        with patch(GRAPH_PATH, side_effect=graph):
            self.tenant._sync_users_delta()
            user = self.tenant.user_ids
            self.assertEqual(user.sku_ids, self.sku)
            self.tenant._sync_users_delta()          # partial item: licenses removed only
            self.assertFalse(user.sku_ids)
            self.assertEqual(user.name, 'Jane')
            self.tenant._sync_users_delta()          # @removed -> archived
        self.assertFalse(user.active)
        self.assertIn('token=2', calls[2])

    def test_delta_pages_and_unknown_users(self):
        calls = []

        def graph(method, path, params=None, **kw):
            calls.append((path, params))
            if path == '/users/delta':
                return {'value': [{'id': 'graph-adele', 'displayName': 'Adele',
                                   'userPrincipalName': 'adele@live.com'}],
                        '@odata.nextLink': 'https://graph/delta?page=2'}
            if path == 'https://graph/delta?page=2':
                # Partial item of a user that is not known yet, unknown removed user
                return {'value': [{'id': USER_ID, 'assignedLicenses': [{'skuId': GUID_A}]},
                                  {'id': 'graph-unknown', '@removed': {'reason': 'changed'}}],
                        '@odata.deltaLink': 'https://graph/delta?token=1'}
            if path == '/users/%s' % USER_ID:
                return {'id': USER_ID, 'displayName': 'Jane', 'userPrincipalName': 'j@live.com',
                        'usageLocation': 'IN', 'assignedLicenses': [{'skuId': GUID_A}]}
            raise AssertionError('Unexpected call %s' % path)

        with patch(GRAPH_PATH, side_effect=graph):
            self.assertEqual(self.tenant._sync_users_delta(), 3)
        # The query parameters are only sent with the first page
        self.assertEqual(calls, [('/users/delta', {'$select': USER_SELECT}),
                                 ('https://graph/delta?page=2', None),
                                 ('/users/%s' % USER_ID, {'$select': USER_SELECT})])
        adele, jane = self.tenant.user_ids.sorted('name')
        self.assertEqual((adele.graph_id, adele.user_principal_name, adele.sku_ids),
                         ('graph-adele', 'adele@live.com', self.sku.browse()))
        self.assertEqual((jane.name, jane.usage_location, jane.sku_ids), ('Jane', 'IN', self.sku))
        self.assertEqual(self.tenant.sudo().user_delta_link, 'https://graph/delta?token=1')
        self.assertTrue(self.tenant.last_user_sync_date)

    def test_delta_restarts_when_token_expired(self):
        self.tenant.sudo().user_delta_link = 'https://graph/delta?token=old'
        gone = self.env['m365.license.user'].create({
            'name': 'Gone', 'tenant_id': self.tenant.id, 'graph_id': 'graph-gone'})
        calls = []

        def graph(method, path, params=None, **kw):
            calls.append(path)
            if 'token=old' in path:
                raise M365GraphError('HTTP 410 - SyncStateNotFound: The sync state is invalid.')
            return {'value': [{'id': USER_ID, 'displayName': 'Jane',
                               'userPrincipalName': 'j@live.com'}],
                    '@odata.deltaLink': 'https://graph/delta?token=new'}

        with patch(GRAPH_PATH, side_effect=graph):
            self.assertEqual(self.tenant._sync_users_delta(), 1)
        self.assertEqual(calls, ['https://graph/delta?token=old', '/users/delta'])
        self.assertEqual(self.tenant.sudo().user_delta_link, 'https://graph/delta?token=new')
        # A full read archives the users that no longer exist
        self.assertFalse(gone.active)
        # Other errors are raised
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 403 - Forbidden')), \
                self.assertRaisesRegex(M365GraphError, 'HTTP 403'):
            self.tenant._sync_users_delta()

    def test_reset_delta(self):
        self.tenant.sudo().user_delta_link = 'https://graph/delta?token=1'
        user = new_test_user(self.env, 'm365_live_user', groups=USER_GROUP)
        with self.assertRaises(AccessError):
            self.tenant.with_user(user).action_reset_delta()
        action = self.tenant.action_reset_delta()
        self.assertEqual(action['params']['message'],
                         'The next synchronisation will read all users again.')
        self.assertFalse(self.tenant.sudo().user_delta_link)

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------
    def test_webhook_client_state(self):
        self.tenant.write({'webhook_enabled': True, 'webhook_subscription_id': 'sub1',
                           'webhook_client_state': 'secret-state'})
        Tenant = self.env['m365.tenant']
        bad = [{'subscriptionId': 'sub1', 'clientState': 'nope'}]
        good = [{'subscriptionId': 'sub1', 'clientState': 'secret-state'}]
        self.assertEqual(Tenant._webhook_receive(self.tenant.id, bad), 0)
        self.assertFalse(self.tenant.webhook_pending)
        self.assertEqual(Tenant._webhook_receive(self.tenant.id, good), 1)
        self.assertTrue(self.tenant.webhook_pending)

    def test_webhook_ignored(self):
        Tenant = self.env['m365.tenant']
        self.tenant.write({'webhook_subscription_id': 'sub1',
                           'webhook_client_state': 'secret-state'})
        good = [{'subscriptionId': 'sub1', 'clientState': 'secret-state'}]
        # Subscription removed, unknown tenant, other subscription
        self.assertEqual(Tenant._webhook_receive(self.tenant.id, good), 0)
        self.tenant.webhook_enabled = True
        last = Tenant.with_context(active_test=False).search([], order='id desc', limit=1)
        self.assertEqual(Tenant._webhook_receive(last.id + 1, good), 0)
        other = [{'subscriptionId': 'sub2', 'clientState': 'secret-state'}]
        self.assertEqual(Tenant._webhook_receive(self.tenant.id, other), 0)
        self.assertFalse(self.tenant.webhook_pending)

    def test_subscribe_requires_https(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://localhost')
        with self.assertRaises(Exception):
            self.tenant.action_webhook_subscribe()

    def test_webhook_subscribe_and_unsubscribe(self):
        self.env['ir.config_parameter'].sudo().set_param('m365.webhook_base_url',
                                                         'https://odoo.example.com/')
        self.tenant.invalidate_recordset(['webhook_url'])
        self.tenant.write({'webhook_enabled': True, 'webhook_subscription_id': 'old-sub'})

        def graph(method, path, **kw):
            return {'id': 'new-sub'} if method == 'POST' else {}

        now = fields.Datetime.now()
        with patch(GRAPH_PATH, side_effect=graph) as mocked:
            self.tenant.action_webhook_subscribe()
        # The previous subscription is replaced
        delete, post = mocked.call_args_list
        self.assertEqual(delete.args, ('DELETE', '/subscriptions/old-sub'))
        self.assertEqual(post.args, ('POST', '/subscriptions'))
        body = post.kwargs['json_body']
        client_state = self.tenant.sudo().webhook_client_state
        self.assertGreaterEqual(len(client_state), 32)
        self.assertEqual(body, {
            'changeType': 'created,updated,deleted',
            'notificationUrl': 'https://odoo.example.com/m365/webhook/%s' % self.tenant.id,
            'resource': 'users',
            'expirationDateTime': self.tenant.webhook_expiry.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'clientState': client_state,
        })
        self.assertEqual((self.tenant.webhook_enabled, self.tenant.webhook_subscription_id),
                         (True, 'new-sub'))
        self.assertEqual((self.tenant.webhook_expiry - now).days, 28)
        # A subscription already removed at Microsoft is removed in Odoo as well
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 404 - ResourceNotFound')):
            self.tenant.action_webhook_unsubscribe()
        self.assertEqual((self.tenant.webhook_enabled, self.tenant.webhook_subscription_id,
                          self.tenant.webhook_expiry), (False, False, False))
        self.assertEqual(self._webhook_logs().mapped('message'), [
            'Webhook subscription removed.', 'Subscribed to user change notifications.'])

    def test_unsubscribe_error(self):
        self.tenant.write({'webhook_enabled': True, 'webhook_subscription_id': 'sub1'})
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 403 - Forbidden')), \
                self.assertRaisesRegex(M365GraphError, 'HTTP 403'):
            self.tenant.action_webhook_unsubscribe()
        self.assertTrue(self.tenant.webhook_enabled)

    def _webhook_logs(self):
        return self.env['m365.audit.log'].search([('tenant_id', '=', self.tenant.id),
                                                  ('action', '=', 'webhook')])

    def test_webhook_renewal(self):
        self._isolate('webhook_enabled')
        now = fields.Datetime.now()
        self.tenant.write({'webhook_enabled': True, 'webhook_subscription_id': 'sub1',
                           'webhook_expiry': now + timedelta(days=2)})
        later = self.env['m365.tenant'].create({
            'name': 'Later', 'domain': 'later.com', 'tenant_id': 'tid-later',
            'client_id': 'cid', 'webhook_enabled': True, 'webhook_subscription_id': 'sub9',
            'webhook_expiry': now + timedelta(days=20)})
        Tenant = self.env['m365.tenant']
        with patch(GRAPH_PATH, return_value={}) as mocked:
            Tenant._cron_webhook_maintenance()
        # Only the subscriptions expiring within 3 days are renewed
        self.assertEqual(mocked.call_args.args, ('PATCH', '/subscriptions/sub1'))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual((self.tenant.webhook_expiry - now).days, 28)
        self.assertEqual(later.webhook_expiry, now + timedelta(days=20))

        # Subscription removed at Microsoft: a new one is created
        self.tenant.webhook_expiry = now

        def graph(method, path, **kw):
            if method == 'PATCH':
                raise M365GraphError('HTTP 404 - ResourceNotFound')
            return {'id': 'sub2'}

        with patch(GRAPH_PATH, side_effect=graph):
            Tenant._cron_webhook_maintenance()
        self.assertEqual(self.tenant.webhook_subscription_id, 'sub2')
        self.assertEqual(self._webhook_logs().mapped('message'),
                         ['Subscription recreated.', 'Subscription renewed.'])

        # Other errors are logged and count as a failure of the tenant
        self.tenant.webhook_expiry = now
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 403 - Forbidden')):
            Tenant._cron_webhook_maintenance()
        self.assertEqual((self.tenant.state, self.tenant.failure_count),
                         ('error', 1))
        self.assertEqual(self._webhook_logs()[0].message, 'Renewal failed: HTTP 403 - Forbidden')

    def test_process_webhooks(self):
        self._isolate('webhook_pending')
        self.tenant.webhook_pending = True

        def graph(method, path, **kw):
            return [] if path == '/subscribedSkus' else {'value': []}

        with patch(GRAPH_PATH, side_effect=graph) as mocked:
            self.env['m365.tenant']._cron_process_webhooks()
        self.assertEqual([call.args[1] for call in mocked.call_args_list],
                         ['/subscribedSkus', '/users/delta'])
        self.assertFalse(self.tenant.webhook_pending)
        log = self._webhook_logs()
        self.assertEqual((log.status, log.message),
                         ('success', 'Synchronised after change notification.'))
