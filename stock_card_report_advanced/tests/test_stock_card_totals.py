# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import StockCardCommon


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardTotalsPerPeriod(StockCardCommon):
    """Report Type = Totals per Period: Date, In, Out, Running Balance, Value."""

    def setUp(self):
        super().setUp()
        self.product.with_company(self.company).standard_price = 2.0
        self._move(self.product, self.supplier, self.stock, 100, "2025-12-20 10:00:00")   # opening
        self._move(self.product, self.supplier, self.stock, 10, "2026-01-05 08:00:00")
        self._move(self.product, self.stock, self.customer, 4, "2026-01-05 15:00:00")     # same day
        self._move(self.product, self.stock, self.customer, 6, "2026-01-12 09:00:00")
        self._move(self.product, self.supplier, self.stock, 7, "2026-02-03 09:00:00")     # next month

    def test_01_daily_totals(self):
        options = self._options(report_mode="summary", interval="day")
        rows = self.engine.get_lines(options)
        self.assertEqual([(r["bucket"].day, r["qty_in"], r["qty_out"], r["running_balance"]) for r in rows],
                         [(5, 10, 4, 106), (12, 0, 6, 100)])
        self.assertEqual([r["balance_value"] for r in rows], [212.0, 200.0])
        self.assertEqual([r["move_count"] for r in rows], [2, 1])

    def test_02_totals_reconcile_with_detailed_report(self):
        detail = self.engine.get_report_data(self._options())["groups"][0]
        for interval in ("day", "week", "month", "period"):
            totals = self.engine.get_report_data(self._options(report_mode="summary", interval=interval))
            group = totals["groups"][0]
            self.assertEqual((group["opening"], group["qty_in"], group["qty_out"], group["closing"]),
                             (detail["opening"], detail["qty_in"], detail["qty_out"], detail["closing"]))
            self.assertEqual(sum(r["qty_in"] for r in group["lines"]), group["qty_in"])
            self.assertEqual(sum(r["qty_out"] for r in group["lines"]), group["qty_out"])
            self.assertEqual(group["lines"][-1]["running_balance"], group["closing"])
            self.assertEqual(totals["meta"]["report_mode"], "summary")

    def test_03_monthly_buckets_and_labels(self):
        options = self._options(report_mode="summary", interval="month",
                                date_from="2025-12-01", date_to="2026-02-28")
        rows = self.engine.get_lines(options)
        self.assertEqual([r["bucket"].month for r in rows], [12, 1, 2])
        self.assertEqual([r["running_balance"] for r in rows], [100, 100, 107])
        self.assertIn("2026", rows[1]["date_display"])

    def test_04_weeks_follow_language_week_start(self):
        lang = self.env["res.lang"]._lang_get(self.env.user.lang or "en_US")
        week_start = int(lang.week_start)
        rows = self.engine.get_lines(self._options(report_mode="summary", interval="week"))
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["bucket"].isoweekday(), week_start)

    def test_05_rpc_pdf_and_xlsx(self):
        options = self._options(report_mode="summary")
        summary = self.engine.rpc_get_summary(options)
        group = summary["groups"][0]
        lines = self.engine.rpc_get_lines(options, group["product_id"], group["location_id"] or 0)
        self.assertEqual(lines[group["key"]][0]["bucket"], "2026-01-05")
        html, _fmt = self.env["ir.actions.report"]._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", [], data={"options": options})
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn("Daily Totals", html)
        self.assertIn("Running Balance", html)
        self.assertNotIn("Move Type", html)
        self.assertNotIn("Source Location", html)
        filename, fileobj, _size = self.env["stock.card.report.xlsx"].generate_xlsx(options)
        fileobj.close()
        self.assertTrue(filename.startswith("Stock_Card_Totals_"))

    def test_07_full_period(self):
        # a second product with stock but no movement in January
        self._move(self.product_b, self.supplier, self.stock, 9, "2025-12-01 10:00:00")
        options = self._options(report_mode="summary", interval="period",
                                product_ids=[self.product.id, self.product_b.id])
        data = self.engine.get_report_data(options)
        self.assertEqual(data["meta"]["mode_label"], "Full Period Totals")
        self.assertEqual(len(data["groups"]), 2)
        for group in data["groups"]:
            self.assertEqual(len(group["lines"]), 1, "exactly one line per product")
            line = group["lines"][0]
            self.assertEqual((line["qty_in"], line["qty_out"], line["running_balance"], line["move_count"]),
                             (group["qty_in"], group["qty_out"], group["closing"], group["move_count"]))
            self.assertEqual(str(line["bucket"]), "2026-01-01")
            self.assertEqual(str(line["bucket_end"]), "2026-01-31")
            self.assertIn("–", line["date_display"], "labelled with the full date range")
        widget = next(g for g in data["groups"] if g["product_id"] == self.product.id)
        self.assertEqual((widget["lines"][0]["qty_in"], widget["lines"][0]["qty_out"]), (10, 10))
        idle = next(g for g in data["groups"] if g["product_id"] == self.product_b.id)
        self.assertEqual((idle["lines"][0]["qty_in"], idle["lines"][0]["running_balance"]), (0, 9))
        html, _fmt = self.env["ir.actions.report"]._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", [], data={"options": options})
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn("Full Period Totals", html)
        self.assertIn("Period", html)
        _name, fileobj, _size = self.env["stock.card.report.xlsx"].generate_xlsx(options)
        fileobj.close()

    def test_06_wizard_and_validation(self):
        wizard = self.env["stock.card.wizard"].create({
            "date_from": "2026-01-01", "date_to": "2026-01-31", "report_mode": "summary", "interval": "period",
        })
        options = wizard._get_options()
        self.assertEqual((options["report_mode"], options["interval"]), ("summary", "period"))
        with self.assertRaises(UserError):
            self.engine.get_lines(self._options(report_mode="ledger"))
        with self.assertRaises(UserError):
            self.engine.get_lines(self._options(report_mode="summary", interval="hour"))


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardInvoiceNumbers(StockCardCommon):
    """Bill / invoice numbers beside the PO / SO origin (only with those apps)."""

    def _today_options(self):
        today = fields.Date.today()
        return self._options(date_from=fields.Date.to_string(today - timedelta(days=1)),
                             date_to=fields.Date.to_string(today + timedelta(days=1)))

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "SC Partner"})

    def test_01_purchase_order_bill(self):
        if "purchase.order" not in self.env or "purchase_line_id" not in self.env["stock.move"]._fields:
            self.skipTest("purchase_stock is not installed")
        po = self.env["purchase.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_qty": 5, "price_unit": 10})],
        })
        po.button_confirm()
        picking = po.picking_ids
        picking.move_ids.quantity_done = 5
        picking.button_validate()
        po.action_create_invoice()
        bill = po.invoice_ids
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        line = self.engine.get_lines(self._today_options())[0]
        self.assertEqual(line["move_type"], "purchase")
        self.assertEqual(line["origin"], po.name)
        self.assertEqual(line["invoice_refs"], bill.name)
        self.assertEqual(line["invoice_ids"], bill.ids)

    def test_02_sale_order_invoice(self):
        if "sale.order" not in self.env or "sale_line_id" not in self.env["stock.move"]._fields:
            self.skipTest("sale_stock is not installed")
        self._move(self.product, self.supplier, self.stock, 10, fields.Datetime.now() - timedelta(hours=1))
        so = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2, "price_unit": 20})],
        })
        so.action_confirm()
        picking = so.picking_ids
        picking.move_ids.quantity_done = 2
        picking.button_validate()
        invoice = so._create_invoices()
        invoice.action_post()
        line = [l for l in self.engine.get_lines(self._today_options()) if l["move_type"] == "sale"][0]
        self.assertEqual(line["origin"], so.name)
        self.assertEqual(line["invoice_refs"], invoice.name)

    def test_03_draft_bill_not_shown(self):
        if "purchase.order" not in self.env or "purchase_line_id" not in self.env["stock.move"]._fields:
            self.skipTest("purchase_stock is not installed")
        po = self.env["purchase.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_qty": 3, "price_unit": 10})],
        })
        po.button_confirm()
        po.picking_ids.move_ids.quantity_done = 3
        po.picking_ids.button_validate()
        po.action_create_invoice()  # left in draft
        line = self.engine.get_lines(self._today_options())[0]
        self.assertEqual(line["invoice_refs"], "")
