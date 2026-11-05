/** @odoo-module **/

/**
 * NavBar Patch — Custom app icon button with home-menu toggle.
 *
 * This patch modifies the NavBar to:
 *   1. Replace the default dropdown apps menu with a custom app-icon button
 *      (handled by the navbar.xml template extension).
 *   2. Add an `onAppIconClick` method that toggles the home menu
 *      by triggering the HOME_MENU:TOGGLE bus event.
 *   3. Track whether the home menu is active to control navbar visuals
 *      (e.g., hiding the current app brand when home menu is shown).
 *
 * Design decisions:
 *   - All event listeners use `useBus()` for automatic cleanup on unmount.
 *   - We don't manipulate `router.pushState` directly — the WebClient patch
 *     handles showing/hiding the home menu, and the ActionManager handles
 *     URL updates. Manual URL manipulation causes sync issues with
 *     Odoo 19's debounced router (see router.js makeDebouncedPush).
 *   - The `currentApp` getter override returns null when the home menu
 *     is active, which causes the navbar template to hide the app brand
 *     and section menus (matching Enterprise behavior).
 */

import { NavBar } from "@web/webclient/navbar/navbar";
import { patch } from "@web/core/utils/patch";
import { useState } from "@odoo/owl";

patch(NavBar.prototype, {
    setup() {
        super.setup(...arguments);
        if (this.env.services.home_menu) {
            this.hm = useState(this.env.services.home_menu);
        }
    },

    /**
     * Override the currentApp getter.
     * When the home menu is active, we return null.
     */
    get currentApp() {
        if (this.hm && this.hm.hasHomeMenu) {
            return null;
        }
        return super.currentApp;
    },

    /**
     * Override currentAppSections to hide section menus when Home Menu is active.
     */
    get currentAppSections() {
        if (this.hm && this.hm.hasHomeMenu) {
            return [];
        }
        return super.currentAppSections;
    },

    /**
     * Handle click on the app icon in the navbar.
     */
    onAppIconClick() {
        if (this.hm) {
            this.hm.toggle();
        }
    },
});
