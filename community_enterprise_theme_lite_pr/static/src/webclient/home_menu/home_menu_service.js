/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { Mutex } from "@web/core/utils/concurrency";
import { useService } from "@web/core/utils/hooks";
import { ControllerNotFoundError, standardActionServiceProps } from "@web/webclient/actions/action_service";
import { HomeMenu } from "@community_enterprise_theme_lite_pr/home_menu/home_menu";

import { Component, onMounted, onWillUnmount, reactive, xml } from "@odoo/owl";

export const homeMenuService = {
    dependencies: ["action"],
    start(env) {
        const state = reactive({
            hasHomeMenu: false,
            hasBackgroundAction: false,
            toggle,
        });
        const mutex = new Mutex();

        class HomeMenuAction extends Component {
            static components = { HomeMenu };
            static target = "current";
            static props = { ...standardActionServiceProps };
            static template = xml`<HomeMenu t-props="homeMenuProps"/>`;
            static displayName = _t("Home");

            setup() {
                this.homeMenuProps = {};
                onMounted(() => this.onMounted());
                onWillUnmount(() => this.onWillUnmount());
            }

            async onMounted() {
                const { breadcrumbs } = this.env.config || { breadcrumbs: [] };
                state.hasHomeMenu = true;
                state.hasBackgroundAction = breadcrumbs && breadcrumbs.length > 0;
                this.env.bus.trigger("HOME-MENU:TOGGLED");
            }

            onWillUnmount() {
                state.hasHomeMenu = false;
                state.hasBackgroundAction = false;
                this.env.bus.trigger("HOME-MENU:TOGGLED");
            }
        }

        registry.category("actions").add("menu", HomeMenuAction);

        env.bus.addEventListener("HOME-MENU:TOGGLED", () => {
            document.body.classList.toggle("o_home_menu_background", state.hasHomeMenu);
        });

        async function toggle(show) {
            return mutex.exec(async () => {
                show = show === undefined ? !state.hasHomeMenu : Boolean(show);
                if (show !== state.hasHomeMenu) {
                    if (show) {
                        await env.services.action.doAction("menu", { clearBreadcrumbs: true });
                    } else {
                        try {
                            await env.services.action.restore();
                        } catch (err) {
                            if (!(err instanceof ControllerNotFoundError)) { throw err; }
                        }
                    }
                }
                return new Promise((r) => setTimeout(r));
            });
        }

        return state;
    },
};

registry.category("services").add("home_menu", homeMenuService);
