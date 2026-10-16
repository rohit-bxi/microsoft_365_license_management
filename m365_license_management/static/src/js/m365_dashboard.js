/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { formatDateTime, deserializeDateTime } from "@web/core/l10n/dates";

const PAGE_SIZE = 7;
const CIRC = 2 * Math.PI * 36;

export class M365Dashboard extends Component {
    static template = "m365_license_management.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            data: null,
            selected: [],
            dateRange: 30,
            page: 0,
            syncing: false,
            tab: "assign",
            form: { tenantId: "", userQuery: "", user: null, skuId: "", reason: "" },
            userResults: [],
            skus: [],
            submitting: false,
        });
        this._timer = null;
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.data = await this.orm.call("m365.dashboard", "get_dashboard_data", [], {
            tenant_ids: this.state.selected,
            date_range: this.state.dateRange,
        });
        this.state.loading = false;
        if (!this.state.form.tenantId && this.state.data.tenants.length) {
            this.state.form.tenantId = String(this.state.data.tenants[0].id);
            await this.loadSkus();
        }
    }

    get lastUpdated() {
        const v = this.state.data && this.state.data.last_updated;
        return v ? formatDateTime(deserializeDateTime(v)) : "-";
    }

    get products() {
        return this.state.data ? this.state.data.products : [];
    }
    get pagedProducts() {
        const s = this.state.page * PAGE_SIZE;
        return this.products.slice(s, s + PAGE_SIZE);
    }
    get pageCount() {
        return Math.max(1, Math.ceil(this.products.length / PAGE_SIZE));
    }
    get pageInfo() {
        const n = this.products.length;
        if (!n) {
            return "0 entries";
        }
        const s = this.state.page * PAGE_SIZE;
        return `${s + 1}-${Math.min(n, s + PAGE_SIZE)} of ${n}`;
    }
    prevPage() {
        if (this.state.page > 0) {
            this.state.page--;
        }
    }
    nextPage() {
        if (this.state.page < this.pageCount - 1) {
            this.state.page++;
        }
    }

    // ---- filters ----
    async toggleTenant(id) {
        const sel = this.state.selected;
        const i = sel.indexOf(id);
        if (i >= 0) {
            sel.splice(i, 1);
        } else {
            sel.push(id);
        }
        this.state.page = 0;
        await this.load();
    }
    isSelected(t) {
        return this.state.selected.length ? this.state.selected.includes(t.id) : false;
    }
    async onDateRange(ev) {
        this.state.dateRange = parseInt(ev.target.value, 10);
        await this.load();
    }

    async refresh() {
        if (this.state.syncing) {
            return;
        }
        this.state.syncing = true;
        try {
            const ids = this.state.selected.length
                ? this.state.selected
                : this.state.data.tenants.map((t) => t.id);
            await this.orm.call("m365.tenant", "sync_from_dashboard", [], { tenant_ids: ids });
        } finally {
            this.state.syncing = false;
        }
        await this.load();
    }

    open(xmlid) {
        this.action.doAction(`m365_license_management.${xmlid}`);
    }

    // ---- charts ----
    donut(pct) {
        const p = Math.max(0, Math.min(100, pct || 0));
        return `${(CIRC * p) / 100} ${CIRC}`;
    }
    get circ() {
        return CIRC;
    }
    barColor(u) {
        return u >= 90 ? "#e5484d" : u >= 75 ? "#f28c28" : "#1a6fe0";
    }
    statusClass(s) {
        return { Enabled: "ok", Warning: "warn" }[s] || "bad";
    }
    delta(v, invert = false) {
        if (v === null || v === undefined) {
            return { text: "–", cls: "flat" };
        }
        const up = v > 0;
        const good = invert ? !up : up;
        const sign = v > 0 ? "+" : "";
        return { text: `${sign}${v}`, cls: v === 0 ? "flat" : good ? "up" : "down", up };
    }
    get distribution() {
        const k = this.state.data.kpis;
        const total = k.total || 1;
        const parts = [
            { label: "Assigned", value: k.assigned, color: "#1a6fe0" },
            { label: "Available", value: k.available, color: "#1fa34a" },
            { label: "Unassigned", value: k.suspended, color: "#f28c28" },
        ];
        let offset = 0;
        return parts.map((p) => {
            const len = (CIRC * p.value) / total;
            const seg = { ...p, pct: Math.round((p.value * 100) / total), dash: `${len} ${CIRC}`, offset: -offset };
            offset += len;
            return seg;
        });
    }
    segWidth(t, key) {
        return t.total ? Math.round((t[key] * 100) / t.total) : 0;
    }

    // ---- user license management ----
    async onTenantChange(ev) {
        this.state.form.tenantId = ev.target.value;
        this.state.form.user = null;
        this.state.form.userQuery = "";
        this.state.form.skuId = "";
        this.state.userResults = [];
        await this.loadSkus();
    }
    async loadSkus() {
        const tid = parseInt(this.state.form.tenantId, 10);
        if (!tid) {
            this.state.skus = [];
            return;
        }
        this.state.skus = await this.orm.searchRead(
            "m365.sku", [["tenant_id", "=", tid]],
            ["name", "available_units", "enabled_units"]
        );
    }
    setTab(tab) {
        this.state.tab = tab;
        this.state.form.skuId = "";
    }
    onUserInput(ev) {
        const q = ev.target.value;
        this.state.form.userQuery = q;
        this.state.form.user = null;
        clearTimeout(this._timer);
        this._timer = setTimeout(() => this.searchUsers(q), 250);
    }
    async searchUsers(q) {
        const tid = parseInt(this.state.form.tenantId, 10);
        if (!tid || q.length < 2) {
            this.state.userResults = [];
            return;
        }
        this.state.userResults = await this.orm.searchRead(
            "m365.license.user",
            [["tenant_id", "=", tid], "|", ["name", "ilike", q], ["user_principal_name", "ilike", q]],
            ["name", "user_principal_name", "sku_ids"],
            { limit: 8 }
        );
    }
    pickUser(u) {
        this.state.form.user = u;
        this.state.form.userQuery = `${u.name} (${u.user_principal_name || ""})`;
        this.state.userResults = [];
        this.state.form.skuId = "";
    }
    get skuOptions() {
        const u = this.state.form.user;
        const held = u ? u.sku_ids : [];
        if (this.state.tab === "assign") {
            return this.state.skus.filter((s) => s.available_units > 0 && !held.includes(s.id));
        }
        return this.state.skus.filter((s) => held.includes(s.id));
    }
    async submitRequest() {
        const f = this.state.form;
        if (!f.user || !f.skuId) {
            this.notification.add("Select a user and a license first.", { type: "warning" });
            return;
        }
        this.state.submitting = true;
        try {
            const res = await this.orm.call("m365.license.request", "create_from_dashboard", [], {
                tenant_id: parseInt(f.tenantId, 10),
                user_id: f.user.id,
                sku_id: parseInt(f.skuId, 10),
                request_type: this.state.tab,
                reason: f.reason || false,
            });
            const msg = {
                done: [`${res.name}: license ${this.state.tab === "assign" ? "assigned" : "removed"}.`, "success"],
                submitted: [`${res.name} submitted for approval.`, "info"],
                failed: [`${res.name} failed: ${res.error}`, "danger"],
            }[res.state] || [`${res.name} created.`, "info"];
            this.notification.add(msg[0], { type: msg[1] });
            f.user = null;
            f.userQuery = "";
            f.skuId = "";
            f.reason = "";
            await this.load();
            await this.loadSkus();
        } catch (e) {
            // server errors are displayed by the framework dialog
            throw e;
        } finally {
            this.state.submitting = false;
        }
    }
}

registry.category("actions").add("m365_license_dashboard", M365Dashboard);
