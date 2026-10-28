==========================
Advanced Stock Card Report
==========================

Odoo 19 (Community & Enterprise) stock card / product ledger with three outputs
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
*Storable Products Only* (products with *Track Inventory*).

How the numbers are computed
============================
``stock.card.report.engine`` runs **one SQL statement per call**:

1. ``scope`` - internal locations selected by warehouse / locations / children.
2. ``base`` - done ``stock.move.line`` rows touching the scope, with Odoo
   17+ stored ``quantity_product_uom`` (the done ``quantity`` converted to the
   product UoM, rounded HALF-UP to its rounding by Odoo itself).
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
dates follow the language's date format through babel (Odoo 19 ships Arabic
as ``%d/%m/%Y``; a format with ``%B`` / ``%b`` prints Arabic date format), and XLSX sheets are
right-to-left. QtWebKit-specific choices: no letter-spacing/uppercase on RTL
labels (it breaks Arabic letter joining), no shrink-to-fit boxes around Arabic
text and no ``opacity`` on text (rasterised in PDFs).

The terms were extracted with Odoo 19's own exporter (``odoo-bin i18n export``), so ``ar.po`` matches
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
  python/record/PDF terms, RTL PDF and workbook, Arabic date format.
* ``stock_card_perf`` - 6,000 bulk-cloned move lines: constant query count vs
  30 lines, exact running balance on every row, read-only SQL, time budget
  (``STOCK_CARD_TIME_BUDGET``, default 10 s).
* ``stock_card_stress`` (opt-in, never in the standard run) - N threads with
  their own cursors read committed data concurrently and must all get identical
  results (``STOCK_CARD_STRESS_WORKERS``, ``STOCK_CARD_STRESS_ROUNDS``)::

    odoo-bin -d <db> --test-tags stock_card_stress --stop-after-init

Odoo 19 port (19.0.1.6.0)
=========================
Same features as 18.0.1.6.0. What changed for Odoo 19:

* Scraps: Odoo 19 removed ``stock.location.scrap_location`` and
  ``stock.move.scrapped``; scrap locations are *Inventory Loss* locations
  (usage ``inventory``). Move Type *Scrap* = the move of a scrap order
  (``stock.move.scrap_id``); the location is hidden by the usage rule.
* ``stock.move.line.reference`` is no longer stored (related to the move): the
  engine reads the move's stored ``reference``. Moves without transfer, scrap
  or count have no reference in Odoo 19 (as in its own Moves History).
* Decimal precision ``Product Unit of Measure`` is renamed ``Product Unit``.
* QWeb: ``t-esc`` is deprecated (a warning per render) - the PDF template and
  the OWL template use ``t-out``; the JS file drops the ``@odoo-module``
  header (native modules).
* XLSX column titles moved to a dict: Odoo 19's term extractor misread
  ``("ref", _lt("..."), 28)`` tuples and exported ``ref``, ``dst``... as terms.
* Tests: ``group_ids`` (was ``groups_id``), no ``stock.move.name`` /
  ``product.template.uom_po_id``, scraps through real scrap orders, Arabic
  dates checked against the language format, stress-test
  ``Transaction`` from ``odoo.orm.environments``.
* ``i18n``: ``.pot`` exported by Odoo 19; ``ar.po`` merged from it.

Odoo 18 port (18.0.1.6.0)
=========================
Same features as 17.0.1.6.0. What changed for Odoo 18:

* Storable products: Odoo 18 has no ``type = 'product'`` any more; goods are
  ``type = 'consu'`` and stock is tracked with ``is_storable`` (*Track
  Inventory*). *Storable Products Only* filters on ``is_storable``; the
  wizard's product domain is ``type = 'consu'``.
* Access check: ``check_access("read")`` (Odoo 18 API) instead of
  ``check_access_rights``.
* Virtual locations: Odoo 18's ``should_bypass_reservation`` no longer lists
  inter-company transit (it only governs reservations). The report keeps
  hiding transit locations without company, as in 16.0 / 17.0, because they are
  not warehouse locations.
* Tests: products created with ``type='consu', is_storable=True``.
* ``i18n``: ``.pot`` exported by Odoo 18; ``ar.po`` merged from it.

Odoo 17 port (17.0.1.6.0)
=========================
Same features as 16.0.1.6.0. What changed for Odoo 17:

* SQL engine: the done quantity of a move line is ``stock_move_line.quantity``
  (``qty_done`` was removed); the engine now reads the stored
  ``quantity_product_uom`` (already converted to the product UoM by Odoo), so
  the UoM joins are gone.
* Scrap creator: ``stock.scrap`` no longer has ``move_id``; the scrap is
  reached through ``stock.move.scrap_id``.
* Partner names: ``res.partner.display_name`` is no longer stored in 17;
  order partners use ``complete_name``. Users (transfer responsible,
  adjustment user, scrap creator) are shown by their own name, without the
  parent company ("Marc Demo", not "YourCompany, Marc Demo").
* Views: ``attrs`` replaced by the Odoo 17 ``invisible="report_mode !=
  'summary'"`` expression.
* Web client: OWL component with ``static template`` / ``static props``,
  ``_t`` instead of the deprecated ``_lt``, no ``owl="1"`` on the template.
* PDF from the wizard: ``report_action(..., config=False)``; the report has its
  own header and footer, so Odoo 17 must not open the document-layout
  configurator on databases where no layout was chosen yet.
* An origin equal to the reference (scraps) is no longer repeated under it.
* Tests: ``move.quantity`` + ``picked`` instead of ``quantity_done``,
  ``registry.clear_cache()``, Odoo 17's ``SQL`` query objects in the
  read-only check.
* ``i18n``: ``.pot`` exported by Odoo 17 itself; ``ar.po`` merged from it
  (adds the translatable PDF file name, drops "Last Modified on").

No data migration is needed from 16.0, 17.0 or 18.0: the module stores no business data
(one transient wizard and a database index).

RTL PDFs need ``rtlcss`` on the server (``npm install -g rtlcss``), as for any
Odoo report; without it Odoo prints Arabic PDFs left to right and logs
"You need https://rtlcss.com/".
