# -*- coding: utf-8 -*-
"""XLSX export of the Stock Card.

``xlsxwriter`` runs in ``constant_memory`` mode: each row is flushed to a temp
file as soon as the next row starts, so memory stays flat whatever the number
of ledger lines. The workbook itself is written to an anonymous temp file that
the controller streams back to the browser.
"""
import tempfile
from datetime import datetime

from odoo import _, api, models
from odoo.tools.translate import _lt

try:
    import xlsxwriter
except ImportError:  # pragma: no cover - declared in external_dependencies
    xlsxwriter = None

COLUMNS = [
    # (key, lazy-translated title, width)
    ("date", _lt("Date"), 17),
    ("ref", _lt("Reference / Origin"), 28),
    ("type", _lt("Move Type"), 20),
    ("src", _lt("Source Location"), 26),
    ("dst", _lt("Destination Location"), 26),
    ("in", _lt("In Quantity"), 13),
    ("out", _lt("Out Quantity"), 13),
    ("bal", _lt("Running Balance"), 15),
    ("value", _lt("Balance Value"), 17),
    # detailed report, XLSX only
    ("partner", _lt("Partner"), 30),
    ("notes", _lt("Notes"), 45),
]
SUMMARY_KEYS = ("date", "in", "out", "bal", "value")


def col_letter(index):
    return chr(ord("A") + index)


class StockCardReportXlsx(models.AbstractModel):
    _name = "stock.card.report.xlsx"
    _description = "Stock Card XLSX Export"

    @api.model
    def _formats(self, workbook, precision, money_digits=2, price_digits=2):
        num = "#,##0" + ("." + "0" * precision if precision else "")
        money = "#,##0" + ("." + "0" * money_digits if money_digits else "")
        price = "#,##0" + ("." + "0" * price_digits if price_digits else "")
        base = {"font_name": "Arial", "font_size": 9, "valign": "top"}

        def add(**kw):
            fmt = dict(base)
            fmt.update(kw)
            return workbook.add_format(fmt)

        return {
            "title": add(bold=True, font_size=15),
            "meta_label": add(bold=True, font_color="#4B5563"),
            "meta": add(),
            "header": add(bold=True, bg_color="#F3F4F6", bottom=2, bottom_color="#9CA3AF", text_wrap=True),
            "header_num": add(bold=True, bg_color="#F3F4F6", bottom=2, bottom_color="#9CA3AF", align="right"),
            "group": add(bold=True, font_size=10, font_color="#FFFFFF", bg_color="#1F2933"),
            "opening": add(italic=True, bold=True, bg_color="#FAFAFA"),
            "opening_num": add(italic=True, bold=True, bg_color="#FAFAFA", num_format=num),
            "datetime": add(num_format="yyyy-mm-dd hh:mm", align="left"),
            "date": add(num_format="yyyy-mm-dd", align="left"),
            "text": add(text_wrap=True),
            "num": add(num_format=num),
            "num_in": add(num_format=num, font_color="#166534"),
            "num_out": add(num_format=num, font_color="#991B1B"),
            "num_bal": add(num_format=num, bold=True),
            "total_label": add(bold=True, align="right", top=2, bottom=2, bg_color="#F9FAFB"),
            "total_num": add(bold=True, num_format=num, top=2, bottom=2, bg_color="#F9FAFB"),
            "summary_text": add(),
            "money": add(num_format=money),
            "money_bold": add(num_format=money, bold=True),
            "price": add(num_format=price),
            "opening_money": add(italic=True, bold=True, bg_color="#FAFAFA", num_format=money),
            "total_money": add(bold=True, num_format=money, top=2, bottom=2, bg_color="#F9FAFB"),
        }

    @api.model
    def generate_xlsx(self, options):
        """Build the workbook. Return ``(filename, fileobj, size)``; ``fileobj``
        is an open temp file positioned at 0 that the caller must close."""
        if xlsxwriter is None:
            raise ImportError("xlsxwriter is required for the Stock Card XLSX export")
        report = self.env["stock.card.report.engine"].get_report_data(options, xlsx_extras=True)
        meta = report["meta"]
        fileobj = tempfile.TemporaryFile()
        workbook = xlsxwriter.Workbook(fileobj, {
            "constant_memory": True,
            # Never interpret user data (references, origins) as formulas/URLs.
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "strings_to_numbers": False,
        })
        try:
            fmt = self._formats(workbook, meta["precision"], meta["currency_digits"], meta["price_precision"])
            self._write_ledger_sheet(workbook, fmt, report)
            self._write_summary_sheet(workbook, fmt, report)
        finally:
            workbook.close()
        size = fileobj.tell()
        fileobj.seek(0)
        kind = "Stock_Card" if meta.get("report_mode") != "summary" else "Stock_Card_Totals"
        filename = "%s_%s_%s.xlsx" % (kind, meta["date_from"], meta["date_to"])
        return filename, fileobj, size

    # ------------------------------------------------------------------
    @api.model
    def _write_meta(self, sheet, fmt, meta):
        sheet.write_string(0, 0, _("Stock Card"), fmt["title"])
        rows = [
            (_("Company"), meta["company_name"]),
            (_("Period"), "%s - %s (%s)" % (meta["date_from_display"], meta["date_to_display"], meta["tz"])),
            (_("Locations"), meta["locations_label"]),
            (_("Products"), meta["products_label"]),
            (_("Categories"), meta["categories_label"]),
            (_("Report Type"), meta.get("mode_label", "")),
            (_("Generated"), meta["generated_at"]),
        ]
        if meta.get("show_valuation"):
            rows.append((_("Stock Value"), "%s %s" % (
                "{:,.{d}f}".format(meta["total_closing_value"], d=meta["currency_digits"]), meta["currency_name"])))
        for idx, (label, value) in enumerate(rows, start=1):
            sheet.write_string(idx, 0, label, fmt["meta_label"])
            sheet.write_string(idx, 1, value or "", fmt["meta"])
        return len(rows) + 2  # first free row after a blank line

    @api.model
    def _columns(self, meta):
        columns = [c for c in COLUMNS if c[0] != "value" or meta.get("show_valuation")]
        if meta.get("report_mode") == "summary":
            columns = [c for c in columns if c[0] in SUMMARY_KEYS]
            if meta.get("interval") != "day":
                columns[0] = ("date", _lt("Period"), 30)
            else:
                columns[0] = ("date", columns[0][1], 16)
        return columns

    @api.model
    def _write_ledger_sheet(self, workbook, fmt, report):
        meta = report["meta"]
        columns = self._columns(meta)
        idx = {key: i for i, (key, _t, _w) in enumerate(columns)}
        last_col = len(columns) - 1
        sheet = workbook.add_worksheet(_("Stock Card")[:31])
        if meta.get("direction") == "rtl":
            sheet.right_to_left()
        for col, (_key, _title, width) in enumerate(columns):
            sheet.set_column(col, col, width)
        sheet.set_landscape()
        sheet.set_paper(9)  # A4
        sheet.fit_to_pages(1, 0)

        row = self._write_meta(sheet, fmt, meta)
        header_row = row
        for col, (key, title, _width) in enumerate(columns):
            title = str(title)
            if key == "value":
                title = "%s (%s)" % (title, meta["currency_name"])
            numeric = key in ("in", "out", "bal", "value")
            sheet.write_string(row, col, title, fmt["header_num"] if numeric else fmt["header"])
        sheet.freeze_panes(header_row + 1, 0)
        sheet.repeat_rows(header_row)
        row += 1

        if not report["groups"]:
            sheet.write_string(row, 0, _("No stock movement matches the selected filters."), fmt["meta"])
            return

        IN, OUT, BAL = col_letter(idx["in"]), col_letter(idx["out"]), col_letter(idx["bal"])
        for group in report["groups"]:
            title = group["product_name"]
            if group["location_name"]:
                title += "  |  " + group["location_name"]
            title += "  |  %s: %s" % (_("UoM"), group["uom_name"])
            if "value" in idx:
                title += "  |  %s: %s %s" % (_("Unit Cost"), "{:,.{d}f}".format(
                    group["unit_cost"], d=meta["price_precision"]), meta["currency_name"])
            sheet.merge_range(row, 0, row, last_col, title, fmt["group"])
            row += 1

            label_col = 1 if "ref" in idx else 0
            if label_col:
                sheet.write_string(row, 0, "", fmt["opening"])
            sheet.write_string(row, label_col, _("Opening Balance"), fmt["opening"])
            for col in range(label_col + 1, idx["bal"]):
                sheet.write_blank(row, col, None, fmt["opening"])
            sheet.write_number(row, idx["bal"], group["opening"], fmt["opening_num"])
            if "value" in idx:
                sheet.write_number(row, idx["value"], group["opening_value"], fmt["opening_money"])
            opening_row = row
            row += 1

            first_line_row = row
            for line in group["lines"]:
                if "bucket" in line:  # totals per period
                    if meta.get("interval") == "day":
                        sheet.write_datetime(row, idx["date"], line["bucket"], fmt["date"])
                    else:
                        sheet.write_string(row, idx["date"], line["date_display"], fmt["text"])
                elif isinstance(line["date_local"], datetime):
                    sheet.write_datetime(row, idx["date"], line["date_local"], fmt["datetime"])
                else:
                    sheet.write_string(row, idx["date"], line["date_display"], fmt["text"])
                if "ref" in idx:
                    # origin (PO / SO) with its bill / invoice beside it
                    origin = " · ".join(filter(None, [line["origin"], line.get("invoice_refs")]))
                    ref = line["reference"]
                    if origin:
                        ref = "%s (%s)" % (ref, origin) if ref else origin
                    sheet.write_string(row, idx["ref"], ref, fmt["text"])
                    sheet.write_string(row, idx["type"], line["move_type_label"], fmt["text"])
                    # virtual locations (vendors, customers, adjustments, scrap...) are left blank
                    sheet.write_string(row, idx["src"], "" if line["src_virtual"] else line["src_name"], fmt["text"])
                    sheet.write_string(row, idx["dst"], "" if line["dst_virtual"] else line["dst_name"], fmt["text"])
                if "partner" in idx:
                    sheet.write_string(row, idx["partner"], line.get("partner_name") or "", fmt["text"])
                    sheet.write_string(row, idx["notes"], line.get("picking_note") or "", fmt["text"])
                sheet.write_number(row, idx["in"], line["qty_in"], fmt["num_in"])
                sheet.write_number(row, idx["out"], line["qty_out"], fmt["num_out"])
                sheet.write_number(row, idx["bal"], line["running_balance"], fmt["num_bal"])
                if "value" in idx:
                    sheet.write_number(row, idx["value"], line["balance_value"], fmt["money"])
                row += 1
            last_line_row = row - 1

            if idx["in"] > 1:
                sheet.merge_range(row, 0, row, idx["in"] - 1, _("Totals / Closing Balance"), fmt["total_label"])
            else:  # a single cell cannot be merged
                sheet.write_string(row, 0, _("Totals / Closing Balance"), fmt["total_label"])
            if group["lines"]:
                # Formulas let auditors re-check the SQL totals; cached values keep
                # the file correct in viewers that do not recalculate.
                sheet.write_formula(row, idx["in"], "=SUM(%s%d:%s%d)" % (IN, first_line_row + 1, IN, last_line_row + 1),
                                    fmt["total_num"], group["qty_in"])
                sheet.write_formula(row, idx["out"], "=SUM(%s%d:%s%d)" % (OUT, first_line_row + 1, OUT, last_line_row + 1),
                                    fmt["total_num"], group["qty_out"])
            else:
                sheet.write_number(row, idx["in"], group["qty_in"], fmt["total_num"])
                sheet.write_number(row, idx["out"], group["qty_out"], fmt["total_num"])
            sheet.write_formula(
                row, idx["bal"], "=%s%d+%s%d-%s%d" % (BAL, opening_row + 1, IN, row + 1, OUT, row + 1),
                fmt["total_num"], group["closing"],
            )
            if "value" in idx:
                sheet.write_number(row, idx["value"], group["closing_value"], fmt["total_money"])
            row += 2  # blank separator row

    @api.model
    def _write_summary_sheet(self, workbook, fmt, report):
        meta = report["meta"]
        valuation = meta.get("show_valuation")
        sheet = workbook.add_worksheet(_("Summary")[:31])
        if meta.get("direction") == "rtl":
            sheet.right_to_left()
        headers = [
            (_("Internal Reference"), 18), (_("Product"), 40), (_("Location"), 30), (_("UoM"), 10),
            (_("Opening Balance"), 15), (_("In Quantity"), 13), (_("Out Quantity"), 13),
            (_("Closing Balance"), 15), (_("Moves"), 9),
        ]
        if valuation:
            cur = meta["currency_name"]
            headers += [("%s (%s)" % (_("Unit Cost"), cur), 15), ("%s (%s)" % (_("Opening Value"), cur), 17),
                        ("%s (%s)" % (_("Closing Value"), cur), 17)]
        for col, (_title, width) in enumerate(headers):
            sheet.set_column(col, col, width)
        row = self._write_meta(sheet, fmt, meta)
        header_row = row
        for col, (title, _width) in enumerate(headers):
            sheet.write_string(row, col, title, fmt["header_num"] if col >= 4 else fmt["header"])
        sheet.freeze_panes(header_row + 1, 0)
        row += 1
        first = row
        for group in report["groups"]:
            sheet.write_string(row, 0, group["product_code"], fmt["summary_text"])
            sheet.write_string(row, 1, group["product_name"], fmt["summary_text"])
            sheet.write_string(row, 2, group["location_name"] or _("All selected locations"), fmt["summary_text"])
            sheet.write_string(row, 3, group["uom_name"], fmt["summary_text"])
            sheet.write_number(row, 4, group["opening"], fmt["num"])
            sheet.write_number(row, 5, group["qty_in"], fmt["num_in"])
            sheet.write_number(row, 6, group["qty_out"], fmt["num_out"])
            sheet.write_number(row, 7, group["closing"], fmt["num_bal"])
            sheet.write_number(row, 8, group["move_count"], fmt["summary_text"])
            if valuation:
                sheet.write_number(row, 9, group["unit_cost"], fmt["price"])
                sheet.write_number(row, 10, group["opening_value"], fmt["money"])
                sheet.write_number(row, 11, group["closing_value"], fmt["money_bold"])
            row += 1
        if report["groups"]:
            sheet.autofilter(header_row, 0, row - 1, len(headers) - 1)
            if valuation:
                sheet.merge_range(row, 0, row, 9, _("Total Stock Value"), fmt["total_label"])
                for col, key in ((10, "total_opening_value"), (11, "total_closing_value")):
                    letter = col_letter(col)
                    sheet.write_formula(row, col, "=SUM(%s%d:%s%d)" % (letter, first + 1, letter, row),
                                        fmt["total_money"], meta[key])
