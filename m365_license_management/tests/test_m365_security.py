# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, new_test_user, tagged

from .common import ADMIN_GROUP, USER_GROUP

MENUS = ['menu_m365_dashboard', 'menu_m365_inventory', 'menu_m365_users',
         'menu_m365_requests', 'menu_m365_audit']
CONFIG_MENUS = ['menu_m365_config', 'menu_m365_tenants_root', 'menu_m365_tenants',
                'menu_m365_tenants_import', 'menu_m365_partner_account',
                'menu_m365_products', 'menu_m365_snapshots']


@tagged('post_install', '-at_install')
class TestM365Security(TransactionCase):

    def _visible(self, user):
        """Menus shown in the web client: the menu and all its parents are visible."""
        visible = self.env['ir.ui.menu'].with_user(user)._visible_menu_ids()
        shown = set()
        for xmlid in MENUS + CONFIG_MENUS:
            menu = self.env.ref('m365_license_management.' + xmlid)
            if all(int(menu_id) in visible for menu_id in menu.parent_path.split('/')[:-1]):
                shown.add(xmlid)
        return shown

    def test_user_sees_all_menus_except_configuration(self):
        user = new_test_user(self.env, 'm365_menu_user', groups=USER_GROUP)
        self.assertEqual(self._visible(user), set(MENUS))
        # The audit log opened from its menu is readable, not editable
        logs = self.env['m365.audit.log'].with_user(user)
        self.assertTrue(logs.has_access('read'))
        self.assertFalse(logs.has_access('write'))

    def test_administrator_sees_all_menus(self):
        admin = new_test_user(self.env, 'm365_menu_admin', groups=ADMIN_GROUP)
        self.assertEqual(self._visible(admin), set(MENUS + CONFIG_MENUS))
