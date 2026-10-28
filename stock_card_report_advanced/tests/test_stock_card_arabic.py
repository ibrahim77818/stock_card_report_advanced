# -*- coding: utf-8 -*-
import zipfile

from odoo.tests import tagged

from .common import StockCardCommon


@tagged("post_install", "-at_install", "stock_card")
class TestStockCardArabic(StockCardCommon):
    """Arabic: module translations, RTL PDF, localised dates, RTL workbook."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.lang"]._activate_lang("ar_001")
        cls.env["ir.module.module"]._load_module_terms(
            ["stock_card_report_advanced"], ["ar_001"], overwrite=True)
        cls.env.registry.clear_cache()  # field labels are ormcached per language
        cls.engine_ar = cls.engine.with_context(lang="ar_001")

    def setUp(self):
        super().setUp()
        self._move(self.product, self.supplier, self.stock, 20, "2025-12-15 10:00:00")
        self._move(self.product, self.supplier, self.stock, 8, "2026-01-05 10:00:00")
        self._move(self.product, self.stock, self.customer, 3, "2026-01-09 10:00:00")

    def test_01_python_terms_direction_and_dates(self):
        data = self.engine_ar.get_report_data(self._options(location_ids=[]))
        meta = data["meta"]
        self.assertEqual(meta["direction"], "rtl")
        self.assertEqual(meta["locations_label"], "جميع المواقع الداخلية")
        self.assertEqual(meta["locations_lines"], ["جميع المواقع الداخلية"])
        # warehouse scope: Arabic phrase and (Latin) warehouse name on separate
        # single-direction lines, so QtWebKit never mixes them in one RTL line
        meta_wh = self.engine_ar.get_report_data(self._options(
            location_ids=[], warehouse_id=self.warehouse.id))["meta"]
        self.assertEqual(meta_wh["locations_lines"],
                         ["جميع المواقع الداخلية في", self.warehouse.display_name])
        # babel month names in Arabic: never an English month inside RTL text
        line = data["groups"][0]["lines"][0]
        self.assertIn("يناير", line["date_display"])
        self.assertNotIn("Jan", line["date_display"])
        self.assertEqual(data["groups"][0]["closing"], 25)

    def test_02_pdf_is_translated_and_rtl(self):
        html, _fmt = self.env["ir.actions.report"].with_context(lang="ar_001")._render_qweb_html(
            "stock_card_report_advanced.report_stock_card", [], data={"options": self._options()}
        )
        html = html.decode() if isinstance(html, bytes) else html
        for term in ("بطاقة الصنف", "الرصيد الافتتاحي", "الرصيد المتراكم", "الكمية الواردة",
                     "الإجماليات / الرصيد الختامي", "الصفحة", "نوع الحركة", "قيمة الرصيد", "الاستلام"):
            self.assertIn(term, html)
        self.assertIn('dir="rtl"', html)
        self.assertNotIn("Opening Balance", html)

    def test_03_wizard_and_menu_translated(self):
        menu = self.env.ref("stock_card_report_advanced.menu_stock_card_report")
        self.assertEqual(menu.with_context(lang="ar_001").name, "بطاقة الصنف")
        fields_ar = self.env["stock.card.wizard"].with_context(lang="ar_001").fields_get(
            ["date_from", "location_ids"])
        self.assertEqual(fields_ar["date_from"]["string"], "من تاريخ")
        self.assertEqual(fields_ar["location_ids"]["string"], "المواقع")

    def test_04_xlsx_is_rtl_and_translated(self):
        _name, fileobj, _size = self.env["stock.card.report.xlsx"].with_context(
            lang="ar_001").generate_xlsx(self._options())
        with fileobj, zipfile.ZipFile(fileobj) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode()
            workbook = archive.read("xl/workbook.xml").decode()
        self.assertIn('rightToLeft="1"', sheet)
        self.assertIn("الرصيد المتراكم", sheet)
        self.assertIn("بطاقة الصنف", workbook)
