# -*- coding: utf-8 -*-
import io
import zipfile
from datetime import datetime

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import StockCardCommon


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardCalculation(StockCardCommon):

    def test_01_opening_in_out_ending(self):
        """Opening + In - Out = Ending, per line and per card."""
        self._move(self.product, self.supplier, self.stock, 100, "2025-12-15 10:00:00")   # opening
        self._move(self.product, self.supplier, self.stock, 50, "2026-01-05 09:00:00")    # in
        self._move(self.product, self.stock, self.customer, 30, "2026-01-10 14:00:00")    # out
        self._move(self.product, self.stock, self.customer, 25, "2026-01-20 16:00:00")    # out
        self._move(self.product, self.supplier, self.stock, 7, "2026-02-02 08:00:00")     # after period

        card = self._single_group(self._options())
        self.assertEqual(card["opening"], 100)
        self.assertEqual(card["qty_in"], 50)
        self.assertEqual(card["qty_out"], 55)
        self.assertEqual(card["closing"], 95)
        self.assertEqual(card["move_count"], 3)

        lines = self.engine.get_lines(self._options())
        self.assertEqual([l["running_balance"] for l in lines], [150, 120, 95])
        self.assertEqual([(l["qty_in"], l["qty_out"]) for l in lines], [(50, 0), (0, 30), (0, 25)])
        self.assertEqual(lines[0]["src_id"], self.supplier.id)
        self.assertEqual(lines[0]["dst_id"], self.stock.id)
        self.assertTrue(lines[0]["reference"])

        self.assertLedgerConsistent(self._options())

        # Independent cross-check against the ORM's historical quantity.
        orm_qty = self.product.with_context(
            location=self.stock.id, to_date=datetime(2026, 2, 1)
        ).qty_available
        self.assertAlmostEqual(orm_qty, card["closing"])

    def test_02_opening_only_product_is_listed(self):
        """A product without movement in the period but with stock is reported."""
        self._move(self.product, self.supplier, self.stock, 12, "2025-11-01 10:00:00")
        card = self._single_group(self._options())
        self.assertEqual((card["opening"], card["qty_in"], card["qty_out"], card["closing"]), (12, 0, 0, 12))
        self.assertEqual(card["move_count"], 0)
        self.assertEqual(self.engine.get_lines(self._options()), [])

    def test_03_no_data_returns_empty(self):
        self.assertEqual(self.engine.get_summary(self._options()), [])
        data = self.engine.get_report_data(self._options())
        self.assertEqual(data["groups"], [])

    def test_04_internal_transfers(self):
        """Transfers inside the scope cancel out; split-by-location shows both legs."""
        self._move(self.product, self.supplier, self.stock, 40, "2026-01-02 10:00:00")
        self._move(self.product, self.stock, self.shelf, 10, "2026-01-15 10:00:00")

        # Aggregated over WH/Stock and its children: the transfer is invisible.
        card = self._single_group(self._options())
        self.assertEqual((card["qty_in"], card["qty_out"], card["closing"]), (40, 0, 40))

        # Split by location: WH/Stock out 10, Shelf in 10.
        groups = self.engine.get_summary(self._options(group_by_location=True))
        by_loc = {g["location_id"]: g for g in groups}
        self.assertEqual(set(by_loc), {self.stock.id, self.shelf.id})
        self.assertEqual((by_loc[self.stock.id]["qty_in"], by_loc[self.stock.id]["qty_out"]), (40, 10))
        self.assertEqual(by_loc[self.stock.id]["closing"], 30)
        self.assertEqual((by_loc[self.shelf.id]["qty_in"], by_loc[self.shelf.id]["closing"]), (10, 10))
        self.assertLedgerConsistent(self._options(group_by_location=True))

        # WH/Stock alone (children excluded): the transfer is an out.
        card = self._single_group(self._options(include_child_locations=False))
        self.assertEqual((card["qty_in"], card["qty_out"], card["closing"]), (40, 10, 30))

        # Shelf alone: only the transfer in.
        card = self._single_group(self._options(location_ids=[self.shelf.id]))
        self.assertEqual((card["opening"], card["qty_in"], card["closing"]), (0, 10, 10))

    def test_05_warehouse_scope(self):
        """No location selected + warehouse = every internal location of it."""
        self._move(self.product, self.supplier, self.shelf, 9, "2026-01-03 10:00:00")
        card = self._single_group(self._options(location_ids=[], warehouse_id=self.warehouse.id))
        self.assertEqual(card["closing"], 9)

    def test_06_same_timestamp_ordering_is_deterministic(self):
        """Ties on the date are ordered by move line id: balances are stable."""
        same = "2026-01-10 10:00:00"
        self._move(self.product, self.supplier, self.stock, 5, same)
        self._move(self.product, self.stock, self.customer, 2, same)
        self._move(self.product, self.supplier, self.stock, 1, same)
        first = [l["running_balance"] for l in self.engine.get_lines(self._options())]
        second = [l["running_balance"] for l in self.engine.get_lines(self._options())]
        self.assertEqual(first, [5, 3, 4])
        self.assertEqual(first, second)


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardTimezone(StockCardCommon):

    def test_01_period_boundaries_follow_user_timezone(self):
        # 2025-12-31 21:30 UTC == 2026-01-01 00:30 in Riyadh (UTC+3)
        self._move(self.product, self.supplier, self.stock, 10, "2025-12-31 21:30:00")
        # 2026-01-31 22:30 UTC == 2026-02-01 01:30 in Riyadh
        self._move(self.product, self.supplier, self.stock, 4, "2026-01-31 22:30:00")

        riyadh = self._single_group(self._options(tz="Asia/Riyadh"))
        self.assertEqual((riyadh["opening"], riyadh["qty_in"]), (0, 10),
                         "Riyadh: first move is in January, last one in February")

        utc = self._single_group(self._options(tz="UTC"))
        self.assertEqual((utc["opening"], utc["qty_in"]), (10, 4),
                         "UTC: first move is in December, last one in January")

    def test_02_displayed_dates_are_local(self):
        self._move(self.product, self.supplier, self.stock, 3, "2025-12-31 21:30:00")
        line = self.engine.get_lines(self._options(tz="Asia/Riyadh"))[0]
        self.assertEqual(line["date_local"], datetime(2026, 1, 1, 0, 30))

    def test_03_negative_offset_timezone(self):
        # 2026-01-01 03:00 UTC == 2025-12-31 22:00 in New York (UTC-5)
        self._move(self.product, self.supplier, self.stock, 6, "2026-01-01 03:00:00")
        card = self._single_group(self._options(tz="America/New_York"))
        self.assertEqual((card["opening"], card["qty_in"]), (6, 0))

    def test_04_invalid_timezone_falls_back_to_utc(self):
        self._move(self.product, self.supplier, self.stock, 1, "2026-01-01 00:30:00")
        card = self._single_group(self._options(tz="Mars/Olympus_Mons"))
        self.assertEqual(card["qty_in"], 1)


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardMultiUom(StockCardCommon):

    def test_01_dozens_converted_to_units(self):
        self._move(self.product, self.supplier, self.stock, 2, "2026-01-05 10:00:00", uom=self.uom_dozen)
        self._move(self.product, self.stock, self.customer, 5, "2026-01-06 10:00:00")
        card = self._single_group(self._options())
        self.assertEqual((card["qty_in"], card["qty_out"], card["closing"]), (24, 5, 19))
        self.assertEqual(card["uom_name"], self.uom_unit.name)
        self.assertEqual([l["running_balance"] for l in self.engine.get_lines(self._options())], [24, 19])

    def test_02_grams_converted_to_kilograms(self):
        self._move(self.product_kg, self.supplier, self.stock, 1500, "2026-01-05 10:00:00", uom=self.uom_gram)
        self._move(self.product_kg, self.stock, self.customer, 0.25, "2026-01-07 10:00:00")
        card = self._single_group(self._options(product_ids=[self.product_kg.id]))
        self.assertAlmostEqual(card["qty_in"], 1.5)
        self.assertAlmostEqual(card["qty_out"], 0.25)
        self.assertAlmostEqual(card["closing"], 1.25)
        orm_qty = self.product_kg.with_context(location=self.stock.id).qty_available
        self.assertAlmostEqual(orm_qty, card["closing"])

    def test_03_mixed_uom_ledger_consistent(self):
        self._move(self.product, self.supplier, self.stock, 1, "2025-12-20 10:00:00", uom=self.uom_dozen)
        self._move(self.product, self.supplier, self.stock, 3, "2026-01-04 10:00:00")
        self._move(self.product, self.stock, self.customer, 1, "2026-01-08 10:00:00", uom=self.uom_dozen)
        card = self._single_group(self._options())
        self.assertEqual((card["opening"], card["qty_in"], card["qty_out"], card["closing"]), (12, 3, 12, 3))
        self.assertLedgerConsistent(self._options())


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardFilters(StockCardCommon):

    def test_01_category_filter_includes_children(self):
        self._move(self.product, self.supplier, self.stock, 1, "2026-01-05 10:00:00")    # SC Child
        self._move(self.product_b, self.supplier, self.stock, 1, "2026-01-05 10:00:00")  # SC Other
        groups = self.engine.get_summary(self._options(product_ids=[], categ_ids=[self.categ_root.id]))
        self.assertEqual([g["product_id"] for g in groups], [self.product.id])
        groups = self.engine.get_summary(self._options(product_ids=[], categ_ids=[self.categ_other.id]))
        self.assertEqual([g["product_id"] for g in groups], [self.product_b.id])

    def test_02_multi_product_filter(self):
        self._move(self.product, self.supplier, self.stock, 1, "2026-01-05 10:00:00")
        self._move(self.product_b, self.supplier, self.stock, 2, "2026-01-05 10:00:00")
        groups = self.engine.get_summary(self._options(product_ids=[self.product.id, self.product_b.id]))
        self.assertEqual({g["product_id"]: g["closing"] for g in groups},
                         {self.product.id: 1, self.product_b.id: 2})
        # Drill-down restriction never escapes the report's product filter.
        lines = self.engine.get_lines(self._options(product_ids=[self.product.id]), product_ids=[self.product_b.id])
        self.assertEqual(lines, [])

    def test_03_consumables_only_when_requested(self):
        consu = self.env["product.product"].create({"name": "SC Tape", "detailed_type": "consu"})
        self._move(consu, self.supplier, self.stock, 3, "2026-01-05 10:00:00")
        self.assertEqual(self.engine.get_summary(self._options(product_ids=[consu.id])), [])
        card = self._single_group(self._options(product_ids=[consu.id], storable_only=False))
        self.assertEqual(card["qty_in"], 3)

    def test_04_company_isolation_and_access(self):
        company_2 = self.env["res.company"].create({"name": "SC Company 2"})
        self.env.user.company_ids |= company_2
        warehouse_2 = self.env["stock.warehouse"].search([("company_id", "=", company_2.id)], limit=1)
        self._move(self.product, self.supplier, self.stock, 5, "2026-01-05 10:00:00")
        self._move(self.product, self.supplier, warehouse_2.lot_stock_id, 8, "2026-01-05 10:00:00",
                   company=company_2)

        card_1 = self._single_group(self._options())
        card_2 = self._single_group(self._options(company_id=company_2.id, location_ids=[]))
        self.assertEqual(card_1["closing"], 5)
        self.assertEqual(card_2["closing"], 8)

        # A stock user restricted to company 1 cannot read company 2.
        user = self.env["res.users"].create({
            "name": "SC Stock User",
            "login": "sc_stock_user",
            "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "groups_id": [(6, 0, [self.env.ref("stock.group_stock_user").id])],
        })
        engine = self.engine.with_user(user)
        self.assertEqual(engine.get_summary(self._options())[0]["closing"], 5)
        with self.assertRaises(AccessError):
            engine.get_summary(self._options(company_id=company_2.id, location_ids=[]))

    def test_05_invalid_options(self):
        with self.assertRaises(UserError):
            self.engine.get_summary(self._options(date_from="2026-02-01", date_to="2026-01-01"))
        with self.assertRaises(UserError):
            self.engine.get_summary(self._options(date_from=False))
        with self.assertRaises(UserError):
            self.engine.get_summary(self._options(product_ids="1; DROP TABLE stock_move_line"))
        with self.assertRaises(UserError):
            self.engine.get_summary("not a dict")


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardOutputs(StockCardCommon):

    def setUp(self):
        super().setUp()
        self._move(self.product, self.supplier, self.stock, 20, "2025-12-15 10:00:00")
        self._move(self.product, self.supplier, self.stock, 8, "2026-01-05 10:00:00")
        self._move(self.product, self.stock, self.customer, 3, "2026-01-09 10:00:00")
        self.wizard = self.env["stock.card.wizard"].create({
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "company_id": self.company.id,
            "location_ids": [(6, 0, [self.stock.id])],
            "product_ids": [(6, 0, [self.product.id])],
        })

    def test_01_wizard_constraint(self):
        with self.assertRaises(ValidationError):
            self.wizard.write({"date_from": "2026-02-10"})

    def test_02_wizard_options_roundtrip(self):
        options = self.wizard._get_options()
        card = self._single_group(options)
        self.assertEqual((card["opening"], card["qty_in"], card["qty_out"], card["closing"]), (20, 8, 3, 25))

    def test_03_html_client_action(self):
        action = self.wizard.action_view_html()
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "stock_card_report_advanced.stock_card_report")
        summary = self.engine.rpc_get_summary(action["params"]["options"])
        self.assertEqual(len(summary["groups"]), 1)
        group = summary["groups"][0]
        lines = self.engine.rpc_get_lines(action["params"]["options"], group["product_id"], group["location_id"] or 0)
        self.assertEqual([l["running_balance"] for l in lines[group["key"]]], [28, 25])
        self.assertIsInstance(lines[group["key"]][0]["date_local"], str, "RPC payload must be JSON-friendly")
        all_lines = self.engine.rpc_get_lines(action["params"]["options"])
        self.assertEqual(list(all_lines), [group["key"]])

    def test_04_pdf_report(self):
        action = self.wizard.action_print_pdf()
        self.assertEqual(action["report_name"], "stock_card_report_advanced.report_stock_card")
        html, _fmt = self.env["ir.actions.report"]._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", self.wizard.ids, data=action["data"]
        )
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn("SC Widget", html)
        self.assertIn("Opening Balance", html)
        self.assertIn("Running Balance", html)
        self.assertIn("25.00", html)
        # Own fixed-height header/footer (no variable-height company address
        # block) and margins pinned by the template: header 19 mm + spacing
        # 8 mm fits inside the 30 mm top margin, so it cannot hit the table.
        self.assertIn("o_sc_header", html)
        self.assertIn('class="page"', html)
        self.assertIn('data-report-margin-top="30"', html)
        # header is drawn from (margin - spacing) = 4 mm down; its 19 mm box ends
        # at 23 mm, before the body (30 mm): no overlap on any page
        self.assertIn('data-report-header-spacing="26"', html)
        self.assertIn("Move Type", html)
        self.assertIn('dir="ltr"', html)

    def test_05_xlsx_export(self):
        filename, fileobj, size = self.env["stock.card.report.xlsx"].generate_xlsx(self.wizard._get_options())
        try:
            payload = fileobj.read()
        finally:
            fileobj.close()
        self.assertTrue(filename.endswith(".xlsx"))
        self.assertEqual(len(payload), size)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn("xl/worksheets/sheet1.xml", names)
            self.assertIn("xl/worksheets/sheet2.xml", names)
            strings = archive.read("xl/sharedStrings.xml").decode() if "xl/sharedStrings.xml" in names else ""
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        # constant_memory mode writes strings inline; accept both storages.
        self.assertTrue("SC Widget" in strings or "SC Widget" in sheet)
        # closing balance (a formula cell whose cached value is written as 25 or 25.0)
        self.assertRegex(sheet, r"<v>25(\.0)?</v>")

    def test_06_xlsx_formula_injection_is_neutralised(self):
        move = self._move(self.product, self.supplier, self.stock, 1, "2026-01-10 10:00:00")
        move.write({"origin": "=HYPERLINK(\"http://evil\")"})
        _name, fileobj, _size = self.env["stock.card.report.xlsx"].generate_xlsx(self.wizard._get_options())
        with fileobj, zipfile.ZipFile(fileobj) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("<f>HYPERLINK", sheet)
