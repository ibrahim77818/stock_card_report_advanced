# -*- coding: utf-8 -*-
import re

from odoo import _, api, models
from odoo.tools.misc import formatLang

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
DEFAULT_ACCENT = "#0F4C81"


class ReportStockCard(models.AbstractModel):
    _name = "report.stock_card_report_advanced.report_stock_card"
    _description = "Stock Card PDF Report"

    @api.model
    def _brand_color(self, company):
        """Company 'Document Layout' primary color, validated because it is
        injected in a style attribute."""
        color = (company.primary_color or "").strip()
        return color if HEX_COLOR.match(color) else DEFAULT_ACCENT

    @api.model
    def _company_lines(self, company):
        partner = company.partner_id
        address = ", ".join(filter(None, [
            partner.street, partner.street2, partner.city, partner.state_id.name,
            partner.zip, partner.country_id.name,
        ]))
        vat = _("Tax ID: %s", company.vat) if company.vat else ""
        # One string: separately-directed spans for contact and tax id overlap
        # in QtWebKit when the report is right-to-left.
        contact = " · ".join(filter(None, [company.phone, company.email, company.website, vat]))
        return address, contact

    @api.model
    def _get_report_values(self, docids, data=None):
        options = (data or {}).get("options")
        wizards = self.env["stock.card.wizard"].browse(docids or [])
        if not options and wizards:
            options = wizards[0]._get_options()
        report = self.env["stock.card.report.engine"].get_report_data(options or {})
        meta = report["meta"]
        precision = meta["precision"]
        company = self.env["res.company"].browse(meta["company_id"])
        address, contact = self._company_lines(company)

        currency = self.env["res.currency"].browse(meta["currency_id"])

        def fmt(value):
            """Quantity, product UoM precision."""
            return formatLang(self.env, value or 0.0, digits=precision)

        def amount(value):
            """Value without symbol (column headed by the currency code)."""
            return formatLang(self.env, value or 0.0, digits=currency.decimal_places)

        def money(value, price=False):
            """Value with currency symbol. formatLang(currency_obj=...) forces the
            currency's decimals, so the number is formatted first: a unit cost
            keeps the 'Product Price' precision."""
            digits = meta["price_precision"] if price else currency.decimal_places
            number = formatLang(self.env, value or 0.0, digits=digits)
            if currency.position == "before":
                return "%s\N{NO-BREAK SPACE}%s" % (currency.symbol, number)
            return "%s\N{NO-BREAK SPACE}%s" % (number, currency.symbol)

        return {
            "doc_ids": docids,
            "doc_model": "stock.card.wizard",
            "docs": wizards,
            "report": report,
            "meta": meta,
            "company": company,
            "company_address": address,
            "company_contact": contact,
            "accent": self._brand_color(company),
            "is_rtl": meta["direction"] == "rtl",
            "lang": meta["lang"],
            "fmt": fmt,
            "amount": amount,
            "money": money,
        }
