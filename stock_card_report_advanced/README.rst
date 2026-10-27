==========================
Advanced Stock Card Report
==========================

Odoo 16 (Community & Enterprise) stock card / product ledger with three outputs
built from the same SQL engine:

* **HTML** - interactive OWL client action: one collapsible card per product
  (per product *and* location when split), lazy drill-down that loads a card's
  lines only when opened, quick search, click-through to the transfer, move,
  product or location.
* **PDF** - QWeb, A4 landscape, own fixed-height header and footer (company
  logo and details, report title, period, page x / y), filter tiles with
  card and movement counts, one card per product with an Opening / In / Out /
  Closing strip; the card header and column titles repeat on every page.
* **XLSX** - ``xlsxwriter`` in ``constant_memory`` mode, streamed from a temp
  file; ledger sheet with live ``SUM``/closing formulas plus a summary sheet.

Columns: Date, Reference/Origin, Move Type, Source Location, Destination
Location, In Quantity, Out Quantity, Running Balance, Balance Value (the last
one with *Show Valuation*, on by default).

Report Type
-----------
Chosen in the wizard; the same three outputs (web page, PDF, XLSX) follow it.

* **Detailed Moves** - one line per stock move (columns above).
* **Totals per Period** - no individual moves: one line per day, week, month
  or for the **Full Period** (*Totals Interval*) with Date (or Period), In
  Quantity, Out Quantity, Running Balance and Balance Value, plus the opening
  and closing rows. *Full Period* gives exactly one line per product for the
  whole Date From - Date To range, including products with stock but no
  movement in it (zero in / out, balance = opening). Computed in SQL
  from the same legs as the detailed report (``date_trunc`` / ISO weekday in
  the user's timezone, running balance as a window over the buckets), so both
  reports always reconcile. Weeks start on the first day of the week of the
  user's language (*Settings > Languages*); partial first/last periods are
  labelled with the actual dates covered.

Bill / invoice numbers
----------------------
When the origin is a purchase or sale order, the posted vendor bills / customer
invoices of the move's own order line are shown beside it (``P00012 ·
BILL/2026/10/0003``), in the direction of the move: receipts show bills,
purchase returns vendor credit notes, deliveries invoices, sales returns
customer credit notes. Drafts and cancelled documents are ignored; several
documents are listed comma-separated. In the web view the number opens the
document. Looked up in the same SQL statement (``LATERAL`` on the indexed order
line links) and only when Purchase / Sales are installed.

Move Type
---------
Computed in the same SQL statement, first matching rule wins: Scrap >
Inventory Adjustment (**Increase** when the line adds stock to the card,
**Decrease** when it removes stock) > Point of Sale > Unbuild > Manufacturing (finished
product / component consumption) > Purchase Order / Purchase Return > Sale
Order / Sales Return > other returns > production locations > Inter-Warehouse
Transfer (transit) > operation type (Receipt / Delivery / Internal Transfer) >
location usages > Other. Purchase, Sales, Manufacturing and PoS rules are only
emitted when those apps' columns exist, so the module still depends on
``stock`` alone.

Partner and Notes (detailed report, XLSX only)
----------------------------------------------
Two extra columns after Balance Value in the Excel export of *Detailed Moves*
(not in the web page, the PDF or the totals reports):

* **Partner**, by move type: purchase / purchase return -> PO vendor; sale /
  sales return -> SO customer; inventory adjustment -> user who applied it;
  scrap -> user who created the scrap; any other move of a transfer -> the
  transfer's *Responsible*. Without Purchase / Sales the transfer's partner is
  used for order moves.
* **Notes** - the transfer's *Notes*, converted from HTML to plain text.

The sources are joined in the same SQL statement only when the XLSX asks for
them (``get_report_data(..., xlsx_extras=True)``), so the web page and PDF do
not pay for them; the joins are on keys and never change the ledger rows.

Virtual locations
-----------------
Source / Destination are left blank for virtual locations, using Odoo's own
definition (``stock.location.should_bypass_reservation``): vendor, customer,
inventory adjustment and production locations, scrap locations and
inter-company transit. Real warehouse locations and company transit stay.

Valuation
---------
Unit cost = the product's *Cost* (``standard_price``) in the report company:
for AVCO/FIFO products Odoo keeps it at the current average/FIFO cost, for
Standard Price products it is the standard. Balance Value = running balance x
unit cost, rounded to the company currency. Shown in the ledger (after Running
Balance), in each product's strip (unit cost, opening and closing value) and
in the header tiles (total stock value, opening value). Read in one batch, so
the number of queries stays constant.

Usage
=====
*Inventory > Reporting > Stock Card*, pick the filters, then **View Report**,
**Print PDF** or **Export XLSX**. The HTML view has the same PDF/XLSX buttons
and a *Filters* button that reopens the wizard pre-filled.

Filters
-------
Date From / Date To (interpreted in the user's timezone), Company, Warehouse,
Locations (multi, sub-locations optional), Products (multi), Product
Categories (multi, sub-categories included), *Split by Location*,
*Storable Products Only*.

How the numbers are computed
============================
``stock.card.report.engine`` runs **one SQL statement per call**:

1. ``scope`` - internal locations selected by warehouse / locations / children.
2. ``base`` - done ``stock.move.line`` rows touching the scope, quantity
   converted to the product UoM in SQL (``qty / line_factor * product_factor``,
   rounded HALF-UP to the product UoM rounding, as Odoo does).
3. ``legs`` - each line becomes an IN leg (destination in scope) and/or an OUT
   leg (source in scope). Transfers between two scoped locations net to zero
   unless the report is split by location.
4. Opening = ``SUM(in - out) FILTER (WHERE date < start)``; running balance =
   ``opening + SUM(in - out) OVER (PARTITION BY product[, location]
   ORDER BY date, line id ROWS UNBOUNDED PRECEDING)``.

Period boundaries are the user's local midnights converted to UTC; displayed
dates are converted back with ``AT TIME ZONE``. The ORM is flushed before every
query, record access and company membership are checked, and every user value
is passed as a psycopg2 parameter. A partial index
``stock_card_sml_done_product_date_idx`` on ``stock_move_line (product_id, date)
WHERE state = 'done'`` is created on install.

PDF layout
==========
The report does not use ``web.external_layout``: those layouts print a company
address block whose height depends on the data, and on continuation pages it
overflowed onto the repeated table header. wkhtmltopdf draws the header from
``margin_top - header_spacing`` downwards, so ``header_spacing`` is the room
reserved for it. The header is a fixed 19 mm box and the template pins the
geometry (margin 30 mm, spacing 26 mm): header 4 -> 23 mm, body from 30 mm.
Bootstrap 5's ``border: 0 solid`` on table cells, which QtWebKit draws as
hairlines, is reset for this report. Brand colour = the company's
*Document Layout* primary colour.

Arabic / right-to-left
======================
``i18n/ar.po`` (base Arabic, used for ``ar_001``, ``ar_SA``, ``ar_EG`` ...)
translates every term: wizard, menu, PDF, XLSX, web view and messages. For RTL
languages the PDF body is ``dir="rtl"`` and mirrored by Odoo's rtlcss step,
dates use the language's month names (babel), and XLSX sheets are
right-to-left. QtWebKit-specific choices: no letter-spacing/uppercase on RTL
labels (it breaks Arabic letter joining), no shrink-to-fit boxes around Arabic
text and no ``opacity`` on text (rasterised in PDFs).

The terms were extracted with Odoo 16's own extractors, so ``ar.po`` matches
what ``Settings > Translations > Export`` produces; after changing a label,
re-export the ``.pot`` and update ``ar.po``. Updating the module (``-u``)
reloads the translations.

Tests
=====
::

    odoo-bin -d <db> -i stock_card_report_advanced --test-tags stock_card --stop-after-init

* ``stock_card`` - calculation (Opening + In - Out = Ending, cross-checked
  against ``qty_available`` with ``to_date``), internal transfers, warehouse
  scope, tie ordering, timezones (Asia/Riyadh, America/New_York, invalid tz),
  multi-UoM (dozens -> units, grams -> kg), category / product / consumable /
  company filters, access rights, wizard, HTML RPC, PDF and XLSX outputs,
  XLSX formula-injection safety, PDF header geometry, move types against
  real moves, hidden virtual locations, valuation (values, company-dependent
  cost, Show Valuation off), totals per period (daily / weekly / monthly /
  full period,
  reconciliation with the detailed report, language week start), bill and
  invoice numbers on real PO / SO flows (skipped without Purchase / Sales),
  draft bills ignored, adjustment increase / decrease, XLSX Partner and Notes
  on real receipt / applied count / scrap flows; Arabic: translated
  python/record/PDF terms, RTL PDF and workbook, Arabic month names.
* ``stock_card_perf`` - 6,000 bulk-cloned move lines: constant query count vs
  30 lines, exact running balance on every row, read-only SQL, time budget
  (``STOCK_CARD_TIME_BUDGET``, default 10 s).
* ``stock_card_stress`` (opt-in, never in the standard run) - N threads with
  their own cursors read committed data concurrently and must all get identical
  results (``STOCK_CARD_STRESS_WORKERS``, ``STOCK_CARD_STRESS_ROUNDS``)::

    odoo-bin -d <db> --test-tags stock_card_stress --stop-after-init
