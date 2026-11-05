/** @odoo-module **/

import { Component, useRef, onMounted } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { computeAppsAndMenuItems } from "@web/webclient/menus/menu_helpers";
import { hasTouch } from "@web/core/browser/feature_detection";

export class HomeMenu extends Component {
    static template = "community_enterprise_theme_lite_pr.HomeMenu";
    static props = {
        onAppClick: { type: Function, optional: true },
    };

    setup() {
        this.menuService = useService("menu");
        this.ui = useService("ui");

        this.searchInputRef = useRef("searchInput");

        onMounted(() => {
            if (!hasTouch()) {
                this._focusInput();
            }
        });
    }

    get appsAndMenuItems() {
        const menuTree = this.menuService.getMenuAsTree("root");
        return computeAppsAndMenuItems(menuTree);
    }

    get displayedApps() {
        const { apps } = this.appsAndMenuItems;
        return apps.map(app => {
            if (app.webIconData && !app.webIconData.startsWith("data:image")) {
                const prefix = app.webIconData.startsWith("P")
                    ? "data:image/svg+xml;base64,"
                    : "data:image/png;base64,";
                app.webIconData = prefix + app.webIconData.replace(/\s/g, "");
            }
            return app;
        });
    }

    // Lite: always light background — no dark mode
    get backgroundImageStyle() {
        const url = `/community_enterprise_theme_lite_pr/static/img/light/background-light.jpg`;
        return `background-image: url("${url}") !important; background-size: cover !important; background-position: center !important; background-repeat: no-repeat !important;`;
    }

    _focusInput() {
        if (this.searchInputRef.el) {
            this.searchInputRef.el.focus({ preventScroll: true });
        }
    }

    onSearchBlur() {
        if (hasTouch()) { return; }
        setTimeout(() => {
            if (document.activeElement === document.body && this.ui.activeElement === document) {
                this._focusInput();
            }
        }, 0);
    }

    onAppClicked(app) {
        this.menuService.selectMenu(app);
    }
}
