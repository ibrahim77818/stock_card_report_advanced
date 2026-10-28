/** @odoo-module **/

import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { download } from "@web/core/network/download";
import { browser } from "@web/core/browser/browser";
import { formatFloat } from "@web/views/fields/formatters";
import { _t } from "@web/core/l10n/translation";

const STORAGE_KEY = "stock_card_report_advanced.options";
const REPORT_NAME = "stock_card_report_advanced.report_stock_card";

export class StockCardReport extends Component {
    static template = "stock_card_report_advanced.StockCardReport";
    // standard client action props (action, actionId, updateActionState, className)
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.ui = useService("ui");
        this.notification = useService("notification");

        this.options = this._resolveOptions();
        this.state = useState({
            loading: true,
            error: false,
            meta: {},
            groups: [],
            expanded: {},
            lines: {},
            loadingKeys: {},
            search: "",
            allLoaded: false,
        });
        onWillStart(() => this.loadSummary());
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    _resolveOptions() {
        const action = this.props.action || {};
        let options = (action.params && action.params.options) || (action.context || {}).stock_card_options;
        if (options) {
            try {
                browser.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(options));
            } catch (_e) {
                // storage unavailable (private mode): reload will reopen the filters
            }
        } else {
            try {
                options = JSON.parse(browser.sessionStorage.getItem(STORAGE_KEY) || "null");
            } catch (_e) {
                options = null;
            }
        }
        return options;
    }

    async loadSummary() {
        if (!this.options) {
            this.state.loading = false;
            this.state.error = _t("No report filters found. Open the Stock Card filters to run the report.");
            return;
        }
        this.state.loading = true;
        this.state.error = false;
        try {
            const result = await this.orm.call("stock.card.report.engine", "rpc_get_summary", [this.options]);
            this.options = result.options;
            this.state.meta = result.meta;
            this.state.groups = result.groups;
            this.state.expanded = {};
            this.state.lines = {};
            this.state.allLoaded = false;
            if (result.groups.length === 1) {
                await this.toggleGroup(result.groups[0]);
            }
        } catch (error) {
            this.state.error = (error && error.data && error.data.message) || String(error);
        } finally {
            this.state.loading = false;
        }
    }

    async _loadLines(group) {
        this.state.loadingKeys[group.key] = true;
        try {
            const result = await this.orm.call("stock.card.report.engine", "rpc_get_lines", [
                this.options,
                group.product_id,
                group.location_id || 0,
            ]);
            this.state.lines[group.key] = result[group.key] || [];
        } finally {
            delete this.state.loadingKeys[group.key];
        }
    }

    // ------------------------------------------------------------------
    // Getters / formatting
    // ------------------------------------------------------------------
    get filteredGroups() {
        const term = (this.state.search || "").trim().toLowerCase();
        if (!term) {
            return this.state.groups;
        }
        return this.state.groups.filter(
            (g) =>
                g.product_name.toLowerCase().includes(term) ||
                (g.location_name || "").toLowerCase().includes(term)
        );
    }

    get isAnyExpanded() {
        return Object.values(this.state.expanded).some(Boolean);
    }

    formatQty(value, hideZero = false) {
        if (hideZero && !value) {
            return "";
        }
        return formatFloat(value || 0, { digits: [false, this.state.meta.precision || 2] });
    }

    get isSummary() {
        return this.state.meta.report_mode === "summary";
    }

    get nCols() {
        const base = this.isSummary ? 4 : 8;
        return this.state.meta.show_valuation ? base + 1 : base;
    }

    openInvoices(line) {
        const ids = line.invoice_ids || [];
        if (ids.length === 1) {
            return this._openRecord("account.move", ids[0]);
        }
        return this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "account.move",
            name: line.invoice_label || line.invoice_refs,
            domain: [["id", "in", ids]],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    formatMoney(value, price = false) {
        const meta = this.state.meta;
        const digits = price ? meta.price_precision : meta.currency_digits;
        const number = formatFloat(value || 0, { digits: [false, digits === undefined ? 2 : digits] });
        return meta.currency_position === "before"
            ? `${meta.currency_symbol}\u00a0${number}`
            : `${number}\u00a0${meta.currency_symbol}`;
    }

    balanceClass(value) {
        return value < 0 ? "text-danger" : "";
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------
    async toggleGroup(group) {
        if (this.state.expanded[group.key]) {
            this.state.expanded[group.key] = false;
            return;
        }
        this.state.expanded[group.key] = true;
        if (!this.state.lines[group.key]) {
            await this._loadLines(group);
        }
    }

    async expandAll() {
        if (!this.state.allLoaded) {
            this.ui.block();
            try {
                const result = await this.orm.call("stock.card.report.engine", "rpc_get_lines", [this.options]);
                for (const group of this.state.groups) {
                    this.state.lines[group.key] = result[group.key] || [];
                }
                this.state.allLoaded = true;
            } finally {
                this.ui.unblock();
            }
        }
        for (const group of this.filteredGroups) {
            this.state.expanded[group.key] = true;
        }
    }

    collapseAll() {
        this.state.expanded = {};
    }

    onSearchInput(ev) {
        this.state.search = ev.target.value;
    }

    openLine(line) {
        if (line.picking_id) {
            return this._openRecord("stock.picking", line.picking_id);
        }
        if (line.move_id) {
            return this._openRecord("stock.move", line.move_id);
        }
        return this._openRecord("stock.move.line", line.line_id);
    }

    openProduct(group) {
        return this._openRecord("product.product", group.product_id);
    }

    openLocation(locationId) {
        return this._openRecord("stock.location", locationId);
    }

    _openRecord(model, resId) {
        return this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: resId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    onEditFilters() {
        const o = this.options || {};
        return this.actionService.doAction("stock_card_report_advanced.action_stock_card_wizard", {
            additionalContext: {
                default_date_from: o.date_from,
                default_date_to: o.date_to,
                default_company_id: o.company_id,
                default_warehouse_id: o.warehouse_id || false,
                default_location_ids: o.location_ids || [],
                default_include_child_locations: o.include_child_locations,
                default_product_ids: o.product_ids || [],
                default_categ_ids: o.categ_ids || [],
                default_group_by_location: o.group_by_location,
                default_storable_only: o.storable_only,
                default_show_valuation: o.show_valuation,
                default_report_mode: o.report_mode,
                default_interval: o.interval,
            },
        });
    }

    async onPrintPdf() {
        return this.actionService.doAction({
            type: "ir.actions.report",
            report_type: "qweb-pdf",
            report_name: REPORT_NAME,
            report_file: REPORT_NAME,
            data: { options: this.options },
            context: {},
        });
    }

    async onExportXlsx() {
        this.ui.block();
        try {
            await download({
                url: "/stock_card_report_advanced/xlsx",
                data: { options: JSON.stringify(this.options) },
            });
        } catch (error) {
            this.notification.add(_t("The XLSX export failed."), { type: "danger" });
            throw error;
        } finally {
            this.ui.unblock();
        }
    }
}

registry.category("actions").add("stock_card_report_advanced.stock_card_report", StockCardReport);
