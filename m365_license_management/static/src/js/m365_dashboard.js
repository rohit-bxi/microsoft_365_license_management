/** @odoo-module **/
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { browser } from "@web/core/browser/browser";
import { formatDateTime, deserializeDateTime } from "@web/core/l10n/dates";

const CIRC = 2 * Math.PI * 36;
const AUTO_REFRESH_MS = 60 * 1000;
const THEME_KEY = "m365d.theme";
const COLORS = { assigned: "#2f7bf5", available: "#22b573", suspended: "#f5a524" };
const CHART = { w: 640, h: 230, l: 44, r: 14, t: 14, b: 28 };

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
            syncing: false,
            theme: browser.localStorage.getItem(THEME_KEY) || "light",
            navOpen: false,
            autoRefresh: true,
            // inventory table
            search: "",
            statusFilter: "all",
            sortKey: "total",
            sortDir: -1,
            page: 0,
            pageSize: 7,
            // trend chart
            hover: null,
            // operations form
            tab: "assign",
            form: { tenantId: "", userQuery: "", user: null, skuId: "", reason: "" },
            userResults: [],
            skus: [],
            submitting: false,
        });
        this._timer = null;
        this._auto = null;
        onWillStart(() => this.load());
        this._auto = browser.setInterval(() => {
            if (this.state.autoRefresh && !this.state.loading && !document.hidden) {
                this.load(true);
            }
        }, AUTO_REFRESH_MS);
        onWillUnmount(() => {
            browser.clearInterval(this._auto);
            browser.clearTimeout(this._timer);
        });
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    async load(silent = false) {
        if (!silent) {
            this.state.loading = true;
        }
        try {
            this.state.data = await this.orm.call("m365.dashboard", "get_dashboard_data", [], {
                tenant_ids: this.state.selected,
                date_range: this.state.dateRange,
            });
        } finally {
            this.state.loading = false;
        }
        if (!this.state.form.tenantId && this.state.data.tenants.length) {
            this.state.form.tenantId = String(this.state.data.tenants[0].id);
            await this.loadSkus();
        }
    }

    get d() {
        return this.state.data;
    }
    get k() {
        return this.state.data.kpis;
    }
    get lastUpdated() {
        const v = this.state.data && this.state.data.last_updated;
        return v ? formatDateTime(deserializeDateTime(v)) : "-";
    }
    formatWhen(v) {
        return v ? formatDateTime(deserializeDateTime(v)) : "";
    }

    // ------------------------------------------------------------------
    // Shell: theme, nav, filters
    // ------------------------------------------------------------------
    toggleTheme() {
        this.state.theme = this.state.theme === "dark" ? "light" : "dark";
        browser.localStorage.setItem(THEME_KEY, this.state.theme);
    }
    toggleNav() {
        this.state.navOpen = !this.state.navOpen;
    }
    toggleAuto() {
        this.state.autoRefresh = !this.state.autoRefresh;
    }
    open(xmlid) {
        this.state.navOpen = false;
        this.action.doAction(`m365_license_management.${xmlid}`);
    }
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
    async clearTenants() {
        this.state.selected.splice(0);
        this.state.page = 0;
        await this.load();
    }
    isSelected(t) {
        return this.state.selected.includes(t.id);
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
            const res = await this.orm.call("m365.tenant", "sync_from_dashboard", [], {
                tenant_ids: ids,
            });
            this.notification.add(res.messages.join("\n") || "Done", {
                type: res.ok ? "success" : "danger",
                sticky: !res.ok,
            });
        } finally {
            this.state.syncing = false;
        }
        await this.load();
    }
    exportCsv() {
        const sel = this.state.selected;
        const q = sel.length === 1 ? `?tenant_id=${sel[0]}` : "";
        browser.open(`/m365/export/licenses${q}`, "_blank");
    }
    onAlertClick(a) {
        const map = {
            tenant_error: "action_m365_tenant",
            tenant_draft: "action_m365_tenant",
            sku_status: "action_m365_sku",
            sku_empty: "action_m365_sku",
            sku_low: "action_m365_sku",
            sku_expiring: "action_m365_sku",
            requests_failed: "action_m365_license_request",
            requests_pending: "action_m365_license_request",
        };
        if (map[a.kind]) {
            this.open(map[a.kind]);
        }
    }

    // ------------------------------------------------------------------
    // KPI helpers
    // ------------------------------------------------------------------
    delta(v, invert = false, pct = true) {
        if (v === null || v === undefined) {
            return { text: "–", cls: "flat", icon: "fa-minus" };
        }
        const up = v > 0;
        const good = invert ? !up : up;
        const sign = v > 0 ? "+" : "";
        const unit = pct ? "%" : "";
        return {
            text: `${sign}${v}${unit}`,
            cls: v === 0 ? "flat" : good ? "up" : "down",
            icon: v === 0 ? "fa-minus" : up ? "fa-arrow-up" : "fa-arrow-down",
        };
    }
    donut(pct) {
        const p = Math.max(0, Math.min(100, pct || 0));
        return `${(CIRC * p) / 100} ${CIRC}`;
    }
    barColor(u) {
        return u >= 90 ? "#ef4565" : u >= 75 ? "#f5a524" : "#2f7bf5";
    }
    statusClass(s) {
        return { Active: "ok", Enabled: "ok", Warning: "warn" }[s] || "bad";
    }
    sparkline(key) {
        const pts = this.d.trend;
        if (!pts || pts.length < 2) {
            return "";
        }
        const vals = pts.map((p) => p[key]);
        const min = Math.min(...vals);
        const max = Math.max(...vals);
        const span = max - min || 1;
        return vals
            .map((v, i) => `${(i * 100) / (vals.length - 1)},${28 - ((v - min) * 24) / span - 2}`)
            .join(" ");
    }

    // ------------------------------------------------------------------
    // Charts
    // ------------------------------------------------------------------
    get distribution() {
        const k = this.k;
        const total = k.total || 1;
        const parts = [
            { label: "Assigned", value: k.assigned, color: COLORS.assigned },
            { label: "Available", value: k.available, color: COLORS.available },
            { label: "Suspended", value: k.suspended, color: COLORS.suspended },
        ];
        let offset = 0;
        return parts.map((p) => {
            const len = (CIRC * p.value) / total;
            const seg = {
                ...p,
                pct: Math.round((p.value * 100) / total),
                dash: `${len} ${CIRC}`,
                offset: -offset,
            };
            offset += len;
            return seg;
        });
    }
    segWidth(t, key) {
        return t.total ? Math.round((t[key] * 100) / t.total) : 0;
    }
    get chart() {
        const pts = this.d.trend || [];
        const { w, h, l, r, t, b } = CHART;
        if (pts.length < 2) {
            return null;
        }
        const max = Math.max(...pts.map((p) => p.total), 1);
        const niceMax = Math.ceil(max / 10) * 10 || 10;
        const x = (i) => l + (i * (w - l - r)) / (pts.length - 1);
        const y = (v) => t + (h - t - b) * (1 - v / niceMax);
        const line = (key) => pts.map((p, i) => `${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ");
        const assigned = line("assigned");
        const area = `${l},${h - b} ${assigned} ${x(pts.length - 1).toFixed(1)},${h - b}`;
        const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({
            y: y(niceMax * f),
            label: Math.round(niceMax * f),
        }));
        const step = Math.max(1, Math.floor(pts.length / 5));
        const xlabels = pts
            .map((p, i) => ({ x: x(i), label: p.date.slice(5), i }))
            .filter((p) => p.i % step === 0);
        return {
            w, h, l, r, t, b,
            totalLine: line("total"),
            assignedLine: assigned,
            area,
            ticks,
            xlabels,
            xs: pts.map((_, i) => x(i)),
            ys: pts.map((p) => y(p.assigned)),
        };
    }
    onChartMove(ev) {
        const c = this.chart;
        if (!c) {
            return;
        }
        const box = ev.currentTarget.getBoundingClientRect();
        const px = ((ev.clientX - box.left) / box.width) * c.w;
        let best = 0;
        let dist = Infinity;
        c.xs.forEach((xv, i) => {
            const dd = Math.abs(xv - px);
            if (dd < dist) {
                dist = dd;
                best = i;
            }
        });
        this.state.hover = best;
    }
    onChartLeave() {
        this.state.hover = null;
    }
    get hoverPoint() {
        const i = this.state.hover;
        const c = this.chart;
        if (i === null || !c) {
            return null;
        }
        const p = this.d.trend[i];
        return { ...p, x: c.xs[i], y: c.ys[i], left: (c.xs[i] / c.w) * 100 };
    }

    // ------------------------------------------------------------------
    // Inventory table
    // ------------------------------------------------------------------
    get products() {
        return this.state.data ? this.state.data.products : [];
    }
    get filteredProducts() {
        const q = this.state.search.trim().toLowerCase();
        const f = this.state.statusFilter;
        const key = this.state.sortKey;
        const dir = this.state.sortDir;
        const rows = this.products.filter((p) => {
            if (f === "critical" && p.utilization < 90) {
                return false;
            }
            if (f === "available" && p.available <= 0) {
                return false;
            }
            if (f === "inactive" && p.status === "Active") {
                return false;
            }
            return !q || p.name.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q);
        });
        rows.sort((a, b) => {
            const av = a[key];
            const bv = b[key];
            if (av === bv) {
                return 0;
            }
            if (av === null || av === undefined) {
                return 1;
            }
            if (bv === null || bv === undefined) {
                return -1;
            }
            return (av > bv ? 1 : -1) * dir;
        });
        return rows;
    }
    get pagedProducts() {
        const s = this.state.page * this.state.pageSize;
        return this.filteredProducts.slice(s, s + this.state.pageSize);
    }
    get pageCount() {
        return Math.max(1, Math.ceil(this.filteredProducts.length / this.state.pageSize));
    }
    get pageInfo() {
        const n = this.filteredProducts.length;
        if (!n) {
            return "No products";
        }
        const s = this.state.page * this.state.pageSize;
        return `Showing ${s + 1}–${Math.min(n, s + this.state.pageSize)} of ${n}`;
    }
    get pages() {
        return Array.from({ length: this.pageCount }, (_, i) => i).slice(0, 7);
    }
    sortBy(key) {
        if (this.state.sortKey === key) {
            this.state.sortDir *= -1;
        } else {
            this.state.sortKey = key;
            this.state.sortDir = key === "name" || key === "sku" ? 1 : -1;
        }
        this.state.page = 0;
    }
    sortIcon(key) {
        if (this.state.sortKey !== key) {
            return "fa-sort";
        }
        return this.state.sortDir > 0 ? "fa-sort-asc" : "fa-sort-desc";
    }
    setPage(i) {
        this.state.page = i;
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
    onSearch(ev) {
        this.state.search = ev.target.value;
        this.state.page = 0;
    }
    onStatusFilter(ev) {
        this.state.statusFilter = ev.target.value;
        this.state.page = 0;
    }
    forecastText(p) {
        if (p.days_left === null || p.days_left === undefined) {
            return "–";
        }
        return p.days_left > 365 ? ">1y" : `${p.days_left}d`;
    }
    forecastClass(p) {
        if (p.days_left === null || p.days_left === undefined) {
            return "flat";
        }
        return p.days_left <= 30 ? "down" : p.days_left <= 90 ? "warnc" : "up";
    }

    // ------------------------------------------------------------------
    // User license management
    // ------------------------------------------------------------------
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
            "m365.sku",
            [["tenant_id", "=", tid]],
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
        browser.clearTimeout(this._timer);
        this._timer = browser.setTimeout(() => this.searchUsers(q), 250);
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
            await this.load(true);
            await this.loadSkus();
        } finally {
            this.state.submitting = false;
        }
    }
    openBulk() {
        // The tenants, users and licenses come from the uploaded file
        this.action.doAction("m365_license_management.action_m365_license_bulk", {
            additionalContext: { default_request_type: this.state.tab },
            onClose: async () => {
                await this.load(true);
                await this.loadSkus();
            },
        });
    }
}

registry.category("actions").add("m365_license_dashboard", M365Dashboard);
