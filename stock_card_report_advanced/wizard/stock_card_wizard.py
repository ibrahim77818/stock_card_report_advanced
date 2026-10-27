# -*- coding: utf-8 -*-
import json
from urllib.parse import urlencode

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockCardWizard(models.TransientModel):
    _name = "stock.card.wizard"
    _description = "Stock Card Report Wizard"

    def _default_date_from(self):
        return fields.Date.context_today(self).replace(day=1)

    report_mode = fields.Selection(
        [("detail", "Detailed Moves"), ("summary", "Totals per Period")],
        string="Report Type", required=True, default="detail",
        help="Detailed Moves: one line per stock move.\n"
             "Totals per Period: one line per day, week or month with the in / out "
             "quantities, the running balance and its value - no individual moves.",
    )
    interval = fields.Selection(
        [("day", "Daily"), ("week", "Weekly"), ("month", "Monthly"), ("period", "Full Period")],
        string="Totals Interval", required=True, default="day",
        help="Full Period: one line per product for the whole Date From - Date To range.\n"
             "Weeks start on the first day of the week of your language settings.",
    )
    date_from = fields.Date(string="Date From", required=True, default=_default_date_from)
    date_to = fields.Date(string="Date To", required=True, default=fields.Date.context_today)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company,
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Warehouse",
        domain="[('company_id', '=', company_id)]",
    )
    allowed_location_ids = fields.Many2many(
        "stock.location", compute="_compute_allowed_location_ids",
    )
    location_ids = fields.Many2many(
        "stock.location", "stock_card_wizard_location_rel", "wizard_id", "location_id",
        string="Locations",
        domain="[('id', 'in', allowed_location_ids)]",
        help="Leave empty to report on every internal location of the warehouse "
             "(or of the company when no warehouse is selected).",
    )
    include_child_locations = fields.Boolean(
        string="Include Sub-locations", default=True,
    )
    product_ids = fields.Many2many(
        "product.product", "stock_card_wizard_product_rel", "wizard_id", "product_id",
        string="Products",
        domain="[('type', 'in', ('product', 'consu')),"
               " '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    categ_ids = fields.Many2many(
        "product.category", "stock_card_wizard_categ_rel", "wizard_id", "categ_id",
        string="Product Categories",
        help="Sub-categories are included automatically.",
    )
    group_by_location = fields.Boolean(
        string="Split by Location",
        help="One stock card per product AND location. Internal transfers between "
             "reported locations then appear as an out on one card and an in on the other.",
    )
    storable_only = fields.Boolean(string="Storable Products Only", default=True)
    show_valuation = fields.Boolean(
        string="Show Valuation", default=True,
        help="Adds the unit cost and the value of every balance (balance x product cost "
             "in the company currency).",
    )

    # ------------------------------------------------------------------
    @api.depends("company_id", "warehouse_id")
    def _compute_allowed_location_ids(self):
        Location = self.env["stock.location"]
        for wizard in self:
            domain = [
                ("usage", "=", "internal"),
                ("company_id", "in", [wizard.company_id.id, False]),
            ]
            if wizard.warehouse_id:
                domain.append(("id", "child_of", wizard.warehouse_id.view_location_id.id))
            wizard.allowed_location_ids = Location.search(domain)

    @api.onchange("company_id")
    def _onchange_company_id(self):
        if self.warehouse_id and self.warehouse_id.company_id != self.company_id:
            self.warehouse_id = False
        self.location_ids = self.location_ids.filtered(
            lambda l: not l.company_id or l.company_id == self.company_id
        )

    @api.onchange("warehouse_id")
    def _onchange_warehouse_id(self):
        if self.warehouse_id:
            self.location_ids = self.location_ids & self.allowed_location_ids

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from and wizard.date_to and wizard.date_from > wizard.date_to:
                raise ValidationError(_("'Date From' must be earlier than or equal to 'Date To'."))

    # ------------------------------------------------------------------
    def _get_options(self):
        self.ensure_one()
        return {
            "date_from": fields.Date.to_string(self.date_from),
            "date_to": fields.Date.to_string(self.date_to),
            "company_id": self.company_id.id,
            "warehouse_id": self.warehouse_id.id or False,
            "location_ids": self.location_ids.ids,
            "include_child_locations": self.include_child_locations,
            "product_ids": self.product_ids.ids,
            "categ_ids": self.categ_ids.ids,
            "group_by_location": self.group_by_location,
            "storable_only": self.storable_only,
            "show_valuation": self.show_valuation,
            "report_mode": self.report_mode,
            "interval": self.interval,
            "tz": self.env.context.get("tz") or self.env.user.tz or "UTC",
        }

    def action_view_html(self):
        self.ensure_one()
        options = self._get_options()
        return {
            "type": "ir.actions.client",
            "tag": "stock_card_report_advanced.stock_card_report",
            "name": _("Stock Card"),
            "target": "main",
            "params": {"options": options},
            "context": {"stock_card_options": options},
        }

    def action_print_pdf(self):
        self.ensure_one()
        return self.env.ref("stock_card_report_advanced.action_report_stock_card").report_action(
            self, data={"options": self._get_options()}
        )

    def action_export_xlsx(self):
        self.ensure_one()
        query = urlencode({"options": json.dumps(self._get_options())})
        return {
            "type": "ir.actions.act_url",
            "url": "/stock_card_report_advanced/xlsx?%s" % query,
            "target": "self",
        }

    # ------------------------------------------------------------------
    def init(self):
        # Partial index serving the engine: done move lines by product and date.
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS stock_card_sml_done_product_date_idx
                ON stock_move_line (product_id, date)
             WHERE state = 'done'
        """)
