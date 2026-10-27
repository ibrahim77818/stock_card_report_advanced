# -*- coding: utf-8 -*-
{
    "name": "Advanced Stock Card Report",
    "version": "16.0.1.6.0",
    "category": "Inventory/Inventory",
    "summary": "Stock card (product ledger) with opening balance, in/out and running "
               "balance - interactive HTML, QWeb PDF and XLSX, computed in pure SQL.",
    "description": """
Advanced Stock Card Report
==========================
* Columns: Date, Reference/Origin, Move Type, Source Location, Destination
  Location, In Quantity, Out Quantity, Running Balance, Balance Value.
* Move Type: Purchase Order, Sale Order, returns, Internal Transfer, Scrap,
  Inventory Adjustment, Manufacturing, Point of Sale... (uses Purchase, Sales,
  Manufacturing and PoS links when those apps are installed).
* Virtual locations (vendors, customers, adjustments, scrap...) are hidden.
* Bill / invoice / credit note numbers beside the PO / SO origin.
* Inventory adjustments split into Increase / Decrease.
* Detailed XLSX: Partner (PO vendor, SO customer, transfer responsible,
  adjustment user, scrap creator) and Notes (transfer note) columns.
* Report Type: Detailed Moves, or Totals per Period (daily / weekly /
  monthly / full period: Date, In, Out, Running Balance, Balance Value) in
  HTML, PDF, XLSX.
* Valuation: unit cost, value of every balance, opening/closing stock value.
* Outputs: interactive drill-down HTML (client action), QWeb PDF, XLSX.
* Filters: period, company, warehouse, locations, products, product categories.
* Engine: a single PostgreSQL statement (CTEs + SUM() OVER window) per call,
  constant query count regardless of data volume.
* Multi-UoM aware (quantities converted to the product's UoM in SQL),
  timezone-correct period boundaries, multi-company safe.
* Arabic translation included; PDF, XLSX and the web view are right-to-left
  for RTL languages.
""",
    "author": "Ibrahim Yahia",
    "website": "https://www.linkedin.com/in/ibrahim-yahia",
    "license": "LGPL-3",
    "depends": ["stock"],
    "external_dependencies": {"python": ["xlsxwriter"]},
    "data": [
        "security/ir.model.access.csv",
        "report/stock_card_report.xml",
        "report/stock_card_templates.xml",
        "wizard/stock_card_wizard_views.xml",
        "views/stock_card_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "stock_card_report_advanced/static/src/scss/stock_card_report.scss",
            "stock_card_report_advanced/static/src/js/stock_card_report.js",
            "stock_card_report_advanced/static/src/xml/stock_card_report.xml",
        ],
        "web.report_assets_common": [
            "stock_card_report_advanced/static/src/scss/stock_card_pdf.scss",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
