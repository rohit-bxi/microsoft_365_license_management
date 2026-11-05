/** @odoo-module **/

import { WebClient } from "@web/webclient/webclient";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";

patch(WebClient.prototype, {
    setup() {
        super.setup(...arguments);
        this.hm = useService("home_menu");
    },

    /**
     * Override the default app loading behavior.
     *
     * In base Odoo Community, when loadRouterState() can't find an action
     * in the URL, it calls _loadDefaultApp() which navigates to the first
     * installed app.
     *
     * We intercept this to show the home menu instead (Enterprise behavior).
     */
    _loadDefaultApp() {
        return this.hm.toggle(true);
    },
});
