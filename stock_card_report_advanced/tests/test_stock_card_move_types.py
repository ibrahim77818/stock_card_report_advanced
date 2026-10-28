# -*- coding: utf-8 -*-
from odoo.tests import tagged

from .common import StockCardCommon


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardMoveTypeAndLocations(StockCardCommon):
    """Move Type column and hiding of virtual (non-warehouse) locations."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        product_c = cls.product.with_company(cls.company)
        cls.inventory_loc = product_c.property_stock_inventory
        cls.production_loc = product_c.property_stock_production

    def _types(self, options=None):
        return [(l["reference"], l["move_type"]) for l in self.engine.get_lines(options or self._options())]

    def test_01_classification(self):
        receipt = self._move(self.product, self.supplier, self.stock, 50, "2026-01-02 10:00:00")
        self._move(self.product, self.stock, self.customer, 5, "2026-01-03 10:00:00")
        self._move(self.product, self.inventory_loc, self.stock, 3, "2026-01-04 10:00:00")
        self._scrap(self.product, 1, "2026-01-05 10:00:00")
        self._move(self.product, self.stock, self.inventory_loc, 2, "2026-01-05 12:00:00")
        self._move(self.product, self.stock, self.production_loc, 2, "2026-01-06 10:00:00")
        ret = self._move(self.product, self.stock, self.supplier, 4, "2026-01-07 10:00:00")
        ret.origin_returned_move_id = receipt
        types = [t for _ref, t in self._types()]
        self.assertEqual(types, ["receipt", "delivery", "inventory_in", "scrap", "inventory_out",
                                 "mrp_consume", "purchase_return"])
        labels = {l["move_type"]: l["move_type_label"] for l in self.engine.get_lines(self._options())}
        self.assertEqual(labels["inventory_in"], "Inventory Adjustment Increase")
        self.assertEqual(labels["inventory_out"], "Inventory Adjustment Decrease")
        self.assertEqual(labels["scrap"], "Scrap")

    def test_02_internal_transfer_split_by_location(self):
        self._move(self.product, self.supplier, self.stock, 10, "2026-01-02 10:00:00")
        self._move(self.product, self.stock, self.shelf, 4, "2026-01-03 10:00:00")
        lines = self.engine.get_lines(self._options(group_by_location=True))
        internal = [l for l in lines if l["move_type"] == "internal"]
        self.assertEqual(len(internal), 2, "one OUT leg on WH/Stock, one IN leg on the shelf")
        for line in internal:  # both locations are real warehouse locations: shown
            self.assertFalse(line["src_virtual"] or line["dst_virtual"])
            self.assertTrue(line["src_name"] and line["dst_name"])

    def test_03_virtual_locations_hidden(self):
        self._move(self.product, self.supplier, self.stock, 10, "2026-01-02 10:00:00")
        self._move(self.product, self.stock, self.customer, 3, "2026-01-03 10:00:00")
        scrap_loc = self._scrap(self.product, 1, "2026-01-04 10:00:00").scrap_location_id
        lines = self.engine.get_lines(self._options())
        receipt, delivery, scrap = lines
        self.assertTrue(receipt["src_virtual"])
        self.assertEqual(receipt["src_name"], "")
        self.assertEqual(receipt["dst_name"], self.stock.complete_name)
        self.assertTrue(delivery["dst_virtual"])
        self.assertEqual(delivery["dst_name"], "")
        self.assertTrue(scrap["dst_virtual"])
        self.assertEqual(scrap["dst_name"], "")
        # PDF: vendor / customer / scrap location names are not printed
        html, _fmt = self.env["ir.actions.report"]._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", [], data={"options": self._options()})
        html = html.decode() if isinstance(html, bytes) else html
        for name in (self.supplier.complete_name, self.customer.complete_name, scrap_loc.complete_name):
            self.assertNotIn(name, html)
        self.assertIn(self.stock.complete_name, html)


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardValuation(StockCardCommon):
    """Balance value = running balance x unit cost (product cost, company)."""

    def setUp(self):
        super().setUp()
        self.product.with_company(self.company).standard_price = 12.5
        self._move(self.product, self.supplier, self.stock, 20, "2025-12-15 10:00:00")
        self._move(self.product, self.supplier, self.stock, 8, "2026-01-05 10:00:00")
        self._move(self.product, self.stock, self.customer, 3, "2026-01-09 10:00:00")

    def test_01_values(self):
        data = self.engine.get_report_data(self._options())
        group = data["groups"][0]
        self.assertEqual(group["unit_cost"], 12.5)
        self.assertEqual(group["opening_value"], 250.0)
        self.assertEqual(group["closing_value"], 312.5)
        self.assertEqual([l["balance_value"] for l in group["lines"]], [350.0, 312.5])
        meta = data["meta"]
        self.assertEqual(meta["total_closing_value"], 312.5)
        self.assertEqual(meta["total_opening_value"], 250.0)
        self.assertEqual(meta["currency_id"], self.company.currency_id.id)
        # every line value is exactly its running balance x unit cost
        for line in group["lines"]:
            self.assertAlmostEqual(line["balance_value"], line["running_balance"] * group["unit_cost"], places=2)

    def test_02_cost_is_company_dependent(self):
        company_2 = self.env["res.company"].create({"name": "SC Valuation Co 2"})
        self.env.user.company_ids |= company_2
        self.product.with_company(company_2).standard_price = 99.0
        group = self.engine.get_report_data(self._options())["groups"][0]
        self.assertEqual(group["unit_cost"], 12.5, "the report company's cost is used")

    def test_03_pdf_with_and_without_valuation(self):
        report = self.env["ir.actions.report"]
        html, _fmt = report._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", [], data={"options": self._options()})
        html = html.decode() if isinstance(html, bytes) else html
        for text in ("Move Type", "Balance Value", "Unit Cost", "Stock Value", "312.50"):
            self.assertIn(text, html)
        html, _fmt = report._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", [],
            data={"options": self._options(show_valuation=False)})
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn("Move Type", html)
        for text in ("Balance Value", "Unit Cost", "Stock Value"):
            self.assertNotIn(text, html)

    def test_04_rpc_payload_has_values(self):
        summary = self.engine.rpc_get_summary(self._options())
        group = summary["groups"][0]
        self.assertTrue(summary["meta"]["show_valuation"])
        lines = self.engine.rpc_get_lines(self._options(), group["product_id"], group["location_id"] or 0)
        self.assertEqual([l["balance_value"] for l in lines[group["key"]]], [350.0, 312.5])
        self.assertEqual(lines[group["key"]][0]["move_type"], "receipt")
