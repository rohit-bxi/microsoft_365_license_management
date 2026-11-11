# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from ..models.m365_tenant_config import SECURITY_DEFAULTS_PATH
from ..models.m365_tenant_domain import NEVER_EXPIRES
from .common import ADMIN_GROUP, GRAPH_PATH, USER_GROUP, FakeGraph, M365Common, map_tenants

DOMAINS = [
    {'id': 'contoso.onmicrosoft.com', 'isVerified': True, 'isInitial': True,
     'authenticationType': 'Managed', 'passwordValidityPeriodInDays': 90,
     'supportedServices': ['Email', 'OfficeCommunicationsOnline']},
    {'id': 'contoso.com', 'isVerified': True, 'isDefault': True, 'authenticationType': 'Managed',
     'passwordValidityPeriodInDays': NEVER_EXPIRES},
    {'id': 'sso.contoso.com', 'isVerified': True, 'authenticationType': 'Federated',
     'passwordValidityPeriodInDays': 90},
    {'id': 'new.contoso.com', 'isVerified': False, 'authenticationType': 'Managed'},
]
ORG = [{'id': 'org-1', 'technicalNotificationMails': ['it@contoso.com']}]


@tagged('post_install', '-at_install')
class TestM365TenantConfig(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(cls.env, 'm365_config_admin', groups=ADMIN_GROUP)
        cls.profile = cls.env['m365.config.profile'].create({
            'name': 'Standard', 'security_defaults': 'disable', 'password_never_expires': True,
            'technical_email': 'noc@bxitech.com',
            'usage_location_id': cls.env.ref('base.in').id,
        })
        cls.tenant.write({'config_profile_id': cls.profile.id,
                          'allow_security_operations': True})

    def _graph(self, routes=None):
        base = {
            ('GET', SECURITY_DEFAULTS_PATH): {'isEnabled': True},
            ('PATCH', SECURITY_DEFAULTS_PATH): {},
            ('GET', '/domains'): DOMAINS,
            ('GET', '/organization'): ORG,
        }
        base.update(routes or {})
        return FakeGraph(base)

    def test_apply_configuration(self):
        graph = self._graph()
        with patch(GRAPH_PATH, side_effect=graph):
            action = self.tenant.with_user(self.admin).action_apply_configuration()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual(graph.bodies('PATCH', SECURITY_DEFAULTS_PATH), [{'isEnabled': False}])
        # Only the verified, managed domain that still expires
        self.assertEqual([(p, b) for m, p, b in graph.calls
                          if m == 'PATCH' and p.startswith('/domains/')],
                         [('/domains/contoso.onmicrosoft.com',
                           {'passwordValidityPeriodInDays': NEVER_EXPIRES})])
        self.assertEqual(graph.bodies('PATCH', '/organization/org-1'),
                         [{'technicalNotificationMails': ['it@contoso.com', 'noc@bxitech.com']}])
        tenant = self.tenant
        self.assertEqual((tenant.config_state, tenant.security_defaults_state),
                         ('applied', 'off'))
        self.assertEqual(len(tenant.domain_ids), 4)
        initial = tenant.domain_ids.filtered('is_initial')
        self.assertEqual(initial.name, 'contoso.onmicrosoft.com')
        self.assertTrue(initial.password_never_expires)
        self.assertEqual(self._logs('security_defaults').mapped('status'), ['success'])
        self.assertEqual(len(self._logs('configure_tenant').filtered(
            lambda l: l.status == 'success')), 2)

        # Applied again: everything is as the profile, nothing is written
        graph = self._graph({
            ('GET', SECURITY_DEFAULTS_PATH): {'isEnabled': False},
            ('GET', '/domains'): [dict(d, passwordValidityPeriodInDays=NEVER_EXPIRES)
                                  for d in DOMAINS],
            ('GET', '/organization'): [{'id': 'org-1',
                                        'technicalNotificationMails': ['NOC@bxitech.com']}],
        })
        with patch(GRAPH_PATH, side_effect=graph):
            self.tenant.with_user(self.admin).action_apply_configuration()
        self.assertFalse(graph.bodies('PATCH'))
        self.assertIn('nothing to change', self._logs('configure_tenant')[0].message)

    def test_turn_off_needs_security_operations(self):
        self.tenant.allow_security_operations = False
        graph = self._graph()
        with patch(GRAPH_PATH, side_effect=graph):
            action = self.tenant.with_user(self.admin).action_apply_configuration()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertFalse(graph.bodies('PATCH'))
        self.assertEqual(self.tenant.config_state, 'failed')
        self.assertIn('Security operations are disabled', self.tenant.config_error)
        self.assertEqual(self._logs('configure_tenant').status, 'failed')

    def test_configuration_stops_at_first_failure(self):
        self.profile.security_defaults = 'keep'
        denied = M365GraphError('HTTP 403 - Authorization_RequestDenied: Insufficient privileges.')
        graph = self._graph({
            ('GET', SECURITY_DEFAULTS_PATH): denied,  # informative only when kept
            ('PATCH', '/domains/contoso.onmicrosoft.com'): denied,
        })
        with patch(GRAPH_PATH, side_effect=graph):
            ok, message = self.tenant._apply_configuration_safe()
        self.assertFalse(ok)
        self.assertIn('HTTP 403', message)
        self.assertFalse(self.tenant.security_defaults_state)
        self.assertNotIn(('GET', '/organization'), [(m, p) for m, p, _b in graph.calls])
        self.assertEqual(self.tenant.config_state, 'failed')

    def test_profiles(self):
        other = self.env['m365.config.profile'].create({'name': 'Other', 'is_default': True})
        third = self.env['m365.config.profile'].create({'name': 'Third', 'is_default': True})
        self.assertFalse(other.is_default)
        self.assertEqual(self.env['m365.config.profile']._get_default(), third)
        tenant = self.env['m365.tenant'].create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'client_id': 'cid'})
        self.assertEqual(tenant.config_profile_id, third)
        self.tenant.config_state = 'applied'
        self.tenant.config_profile_id = other
        self.assertEqual(self.tenant.config_state, 'not_applied')
        with self.assertRaises(UserError):
            other.technical_email = 'not an email'

    def test_applied_after_consent(self):
        graph = self._graph()
        with patch(GRAPH_PATH, side_effect=graph):
            self.tenant._after_shared_app_connected()
        self.assertEqual(self.tenant.config_state, 'applied')
        with patch(GRAPH_PATH, side_effect=graph):
            self.tenant._after_shared_app_connected()  # already applied: not again
        self.assertEqual(len(graph.bodies('PATCH', SECURITY_DEFAULTS_PATH)), 1)

    def test_probe_permissions(self):
        denied = M365GraphError('HTTP 403 - Authorization_RequestDenied: Insufficient privileges.')
        graph = FakeGraph({SECURITY_DEFAULTS_PATH: denied,
                           '/organization': [{'id': 'org-1', 'displayName': 'Contoso'}]})
        with patch(GRAPH_PATH, side_effect=graph):
            action = self.tenant.with_user(self.admin).action_test_connection()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertIn('security policies (Policy.Read.All)', action['params']['message'])

    # ------------------------------------------------------------------
    # Domains
    # ------------------------------------------------------------------
    def test_add_and_verify_domain(self):
        Wizard = self.env['m365.tenant.domain.wizard'].with_user(self.admin)
        for name in ('not a domain', 'contoso2.onmicrosoft.com'):
            with self.assertRaises(UserError):
                Wizard.create({'tenant_id': self.tenant.id, 'name': name}).action_add()
        graph = FakeGraph({
            ('POST', '/domains'): lambda body: {'id': body['id'], 'isVerified': False,
                                                'authenticationType': 'Managed'},
            '/domains/fabrikam.com/verificationDnsRecords': [
                {'recordType': 'Txt', 'label': 'fabrikam.com', 'text': 'MS=ms12345678',
                 'ttl': 3600},
                {'recordType': 'Mx', 'label': 'fabrikam.com', 'mailExchange': 'ms12345678.msv1.invalid',
                 'preference': 32767, 'ttl': 3600, 'isOptional': True},
            ],
            ('POST', '/domains/fabrikam.com/verify'): M365GraphError(
                'HTTP 400 - Request_BadRequest: Domain verification failed.'),
        })
        with patch(GRAPH_PATH, side_effect=graph):
            action = Wizard.create({'tenant_id': self.tenant.id,
                                    'name': ' Fabrikam.COM '}).action_add()
            domain = self.env['m365.tenant.domain'].browse(action['res_id'])
            self.assertEqual((domain.name, domain.is_verified), ('fabrikam.com', False))
            self.assertEqual(graph.bodies('POST', '/domains'), [{'id': 'fabrikam.com'}])
            self.assertEqual(domain.verification_records.splitlines(), [
                'TXT  fabrikam.com  MS=ms12345678  (TTL 3600)',
                'MX  fabrikam.com  ms12345678.msv1.invalid (priority 32767)  (TTL 3600)  '
                'optional',
            ])
            action = domain.with_user(self.admin).action_verify()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertIn('could not verify fabrikam.com yet', action['params']['message'])
        self.assertFalse(domain.is_verified)
        self.assertEqual(self._logs('domain').mapped('status'), ['failed', 'success'])

        graph = FakeGraph({
            ('POST', '/domains/fabrikam.com/verify'): {'id': 'fabrikam.com', 'isVerified': True},
            '/domains/fabrikam.com/serviceConfigurationRecords': [
                {'recordType': 'CName', 'label': 'autodiscover.fabrikam.com',
                 'canonicalName': 'autodiscover.outlook.com', 'ttl': 3600,
                 'supportedService': 'Email'}],
            ('GET', '/domains'): [{'id': 'fabrikam.com', 'isVerified': True, 'isDefault': True}],
        })
        with patch(GRAPH_PATH, side_effect=graph):
            domain.with_user(self.admin).action_verify()
            self.assertTrue(domain.is_verified)
            self.assertIn('autodiscover.outlook.com', domain.service_records)
            domain.with_user(self.admin).action_make_default()
        self.assertEqual(graph.bodies('PATCH', '/domains/fabrikam.com'), [{'isDefault': True}])
        self.assertTrue(domain.is_default)

    def test_sync_domains_and_access(self):
        self.env['m365.tenant.domain'].create({'tenant_id': self.tenant.id, 'name': 'old.com'})
        with patch(GRAPH_PATH, side_effect=FakeGraph({('GET', '/domains'): DOMAINS})):
            self.tenant.with_user(self.admin).action_sync_domains()
        self.assertEqual(sorted(self.tenant.domain_ids.mapped('name')),
                         sorted(d['id'] for d in DOMAINS))
        self.assertFalse(self.tenant.domain_ids.filtered(lambda d: d.name == 'new.contoso.com')
                         .is_verified)
        user = map_tenants(new_test_user(self.env, 'm365_config_user', groups=USER_GROUP),
                           self.tenant)
        self.assertEqual(len(self.env['m365.tenant.domain'].with_user(user).search([])), 4)
        for action in ('action_sync_domains', 'action_apply_configuration'):
            with self.assertRaises(AccessError):
                getattr(self.tenant.with_user(user), action)()
        with self.assertRaises(AccessError):
            self.tenant.domain_ids[:1].with_user(user).action_verify()
