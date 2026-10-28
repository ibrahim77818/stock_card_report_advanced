# -*- coding: utf-8 -*-
"""SQL engine of the Stock Card report.

Every figure of the report (opening balance, in / out quantities, running
balance, closing balance) is produced by PostgreSQL in ONE statement per call:

* ``scope``  CTE   - the internal locations covered by the filters
                     (warehouse, selected locations, optionally their children);
* ``base``   CTE   - done move lines touching the scope, with the quantity
                     converted into the product's UoM (multi-UoM safe);
* ``legs``   CTE   - every move line exploded into an IN leg and/or an OUT leg
                     relative to the scope (internal transfers inside the scope
                     cancel out unless the report is split by location);
* summary / lines  - aggregated with ``FILTER`` clauses and ``SUM() OVER``.

The Python side only normalises options, adds display names in batch and
shapes the result, so the number of SQL queries is constant whatever the
number of stock moves.
"""
import logging
import re
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_round, html2plaintext
from odoo.tools.misc import format_date

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL templates. Only constant fragments built by this module are formatted
# into them; every user value goes through psycopg2 parameters.
# ---------------------------------------------------------------------------
_LEGS_CTE = """
WITH scope AS MATERIALIZED (
    SELECT l.id
      FROM stock_location l
     WHERE l.usage = 'internal'
       AND (l.company_id IS NULL OR l.company_id = %(company_id)s)
       {scope_where}
),
base AS NOT MATERIALIZED (
    SELECT sml.id                     AS line_id,
           sml.move_id                AS move_id,
           sml.picking_id             AS picking_id,
           sml.reference              AS line_reference,
           sml.date                   AS date_utc,
           sml.product_id             AS product_id,
           sml.location_id            AS src_id,
           sml.location_dest_id       AS dst_id,
           (s_src.id IS NOT NULL)     AS src_in,
           (s_dst.id IS NOT NULL)     AS dst_in,
           -- Odoo 17 stores the done quantity converted to the product UoM
           -- (stock.move.line.quantity_product_uom, HALF-UP to the UoM rounding)
           sml.quantity_product_uom::numeric AS qty
      FROM stock_move_line sml
      JOIN product_product  pp ON pp.id = sml.product_id
      JOIN product_template pt ON pt.id = pp.product_tmpl_id
      LEFT JOIN scope    s_src ON s_src.id = sml.location_id
      LEFT JOIN scope    s_dst ON s_dst.id = sml.location_dest_id
      {categ_join}
     WHERE sml.state = 'done'
       AND sml.company_id = %(company_id)s
       AND sml.date < %(date_end)s
       AND (s_src.id IS NOT NULL OR s_dst.id IS NOT NULL)
       {product_where}
),
-- Each move line becomes an IN leg (destination in scope) and/or an OUT leg
-- (source in scope) in a single pass. When the report is not split by
-- location, a transfer between two scoped locations produces no leg at all.
legs AS NOT MATERIALIZED (
    SELECT b.line_id, b.move_id, b.picking_id, b.line_reference, b.date_utc, b.product_id,
           b.src_id, b.dst_id,
           x.group_location_id, x.qty_in, x.qty_out, x.leg_seq
      FROM base b
      CROSS JOIN LATERAL (VALUES
          (1, CASE WHEN %(by_location)s THEN b.dst_id ELSE 0 END, b.qty, 0::numeric,
              b.dst_in AND (%(by_location)s OR NOT b.src_in)),
          (0, CASE WHEN %(by_location)s THEN b.src_id ELSE 0 END, 0::numeric, b.qty,
              b.src_in AND (%(by_location)s OR NOT b.dst_in))
      ) AS x(leg_seq, group_location_id, qty_in, qty_out, keep)
     WHERE x.keep
)
"""

_SUMMARY_SQL = """
SELECT s.product_id,
       s.group_location_id,
       s.opening,
       s.qty_in,
       s.qty_out,
       s.opening + s.qty_in - s.qty_out AS closing,
       s.move_count
  FROM (
        SELECT product_id,
               group_location_id,
               COALESCE(SUM(qty_in - qty_out) FILTER (WHERE date_utc <  %(date_start)s), 0) AS opening,
               COALESCE(SUM(qty_in)           FILTER (WHERE date_utc >= %(date_start)s), 0) AS qty_in,
               COALESCE(SUM(qty_out)          FILTER (WHERE date_utc >= %(date_start)s), 0) AS qty_out,
               COUNT(*)                       FILTER (WHERE date_utc >= %(date_start)s)     AS move_count
          FROM legs
         GROUP BY product_id, group_location_id
       ) s
 WHERE s.move_count > 0 OR s.opening <> 0
"""

_LINES_SQL = """,
opening AS (
    SELECT product_id, group_location_id, SUM(qty_in - qty_out) AS opening
      FROM legs
     WHERE date_utc < %(date_start)s
     GROUP BY product_id, group_location_id
),
period AS (
    SELECT *
      FROM legs
     WHERE date_utc >= %(date_start)s
       {group_where}
)
SELECT p.line_id,
       p.leg_seq,
       p.move_id,
       p.picking_id,
       p.product_id,
       p.group_location_id,
       p.date_utc,
       (p.date_utc AT TIME ZONE 'UTC') AT TIME ZONE %(tz)s     AS date_local,
       COALESCE(sm.reference, p.line_reference, '')            AS reference,
       COALESCE(sm.origin, '')                                 AS origin,
       {move_type}                                             AS move_type,
       {invoice_cols},
       p.src_id,
       {src_virtual}                                           AS src_virtual,
       CASE WHEN {src_virtual} THEN NULL ELSE src.complete_name END AS src_name,
       p.dst_id,
       {dst_virtual}                                           AS dst_virtual,
       CASE WHEN {dst_virtual} THEN NULL ELSE dst.complete_name END AS dst_name,
       p.qty_in,
       p.qty_out,
       COALESCE(o.opening, 0) + SUM(p.qty_in - p.qty_out) OVER w AS running_balance
       {extra_cols}
  FROM period p
  LEFT JOIN opening           o   ON o.product_id = p.product_id
                                 AND o.group_location_id = p.group_location_id
  LEFT JOIN stock_move        sm  ON sm.id = p.move_id
  LEFT JOIN stock_picking_type spt ON spt.id = sm.picking_type_id
  {extra_joins}
  JOIN stock_location         src ON src.id = p.src_id
  JOIN stock_location         dst ON dst.id = p.dst_id
  {invoice_join}
  {extra_joins_xlsx}
WINDOW w AS (
    PARTITION BY p.product_id, p.group_location_id
    ORDER BY p.date_utc, p.line_id, p.leg_seq
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
)
 ORDER BY p.product_id, p.group_location_id, p.date_utc, p.line_id, p.leg_seq
"""

# Totals per period: the same legs, summed per local day / week / month.
_BUCKETS_SQL = """,
opening AS (
    SELECT product_id, group_location_id, SUM(qty_in - qty_out) AS opening
      FROM legs
     WHERE date_utc < %(date_start)s
     GROUP BY product_id, group_location_id
),
period AS (
    SELECT product_id, group_location_id, qty_in, qty_out,
           ((date_utc AT TIME ZONE 'UTC') AT TIME ZONE %(tz)s)::date AS local_day
      FROM legs
     WHERE date_utc >= %(date_start)s
       {group_where}
),
buckets AS (
    SELECT product_id, group_location_id,
           CASE %(interval)s
               WHEN 'period' THEN %(date_from)s::date
               WHEN 'month' THEN date_trunc('month', local_day)::date
               WHEN 'week'  THEN local_day - ((EXTRACT(ISODOW FROM local_day)::int - %(week_start)s + 7) %% 7)
               ELSE local_day
           END                AS bucket,
           SUM(qty_in)        AS qty_in,
           SUM(qty_out)       AS qty_out,
           COUNT(*)           AS move_count
      FROM period
     GROUP BY 1, 2, 3
    UNION ALL
    -- Full Period: products with an opening balance but no movement still get
    -- their (single) line, as they get their card in the summary
    SELECT product_id, group_location_id, %(date_from)s::date, 0, 0, 0
      FROM opening
     WHERE %(interval)s = 'period'
       AND opening <> 0
       {group_where}
       AND NOT EXISTS (SELECT 1 FROM period pe
                        WHERE pe.product_id = opening.product_id
                          AND pe.group_location_id = opening.group_location_id)
)
SELECT b.product_id, b.group_location_id, b.bucket, b.qty_in, b.qty_out, b.move_count,
       COALESCE(o.opening, 0) + SUM(b.qty_in - b.qty_out) OVER (
           PARTITION BY b.product_id, b.group_location_id
           ORDER BY b.bucket
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS running_balance
  FROM buckets b
  LEFT JOIN opening o ON o.product_id = b.product_id
                     AND o.group_location_id = b.group_location_id
 ORDER BY b.product_id, b.group_location_id, b.bucket
"""

# Posted vendor bills / customer invoices of the move's OWN order line, in the
# direction of the move (receipt -> bill, purchase return -> vendor credit
# note, delivery -> invoice, sales return -> credit note).
_INVOICE_JOIN = """LEFT JOIN LATERAL (
      SELECT string_agg(DISTINCT x.name, ', ' ORDER BY x.name) AS invoice_refs,
             array_agg(DISTINCT x.id)                          AS invoice_ids
        FROM ({sources}) x
  ) inv ON TRUE"""
_PURCHASE_INVOICES = """
          SELECT am.id, am.name
            FROM account_move_line aml
            JOIN account_move am ON am.id = aml.move_id
           WHERE aml.purchase_line_id = sm.purchase_line_id
             AND am.state = 'posted'
             AND am.move_type = CASE WHEN dst.usage = 'supplier' OR sm.origin_returned_move_id IS NOT NULL
                                     THEN 'in_refund' ELSE 'in_invoice' END"""
_SALE_INVOICES = """
          SELECT am.id, am.name
            FROM {rel} rel
            JOIN account_move_line aml ON aml.id = rel.{invoice_col}
            JOIN account_move am ON am.id = aml.move_id
           WHERE rel.{order_col} = sm.sale_line_id
             AND am.state = 'posted'
             AND am.move_type = CASE WHEN src.usage = 'customer' OR sm.origin_returned_move_id IS NOT NULL
                                     THEN 'out_refund' ELSE 'out_invoice' END"""
_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
REPORT_MODES = ("detail", "summary")
INTERVALS = ("day", "week", "month", "period")

# A location is "virtual" (vendors, customers, inventory adjustment,
# production, scrap) as Odoo defines it in stock.location.should_bypass_reservation();
# its name is hidden in the report. Odoo 18 removed the inter-company transit
# clause from that method (a reservation concern), but such a location (transit
# without company) is still no warehouse location, so the report keeps hiding it
# as the 16.0 / 17.0 versions do.
_VIRTUAL_LOCATION = (
    "({a}.usage IN ('supplier', 'customer', 'inventory', 'production')"
    " OR COALESCE({a}.scrap_location, FALSE)"
    " OR ({a}.usage = 'transit' AND {a}.company_id IS NULL))"
)

# Optional stock.move / stock.picking columns added by other apps. A rule is
# only emitted when its column exists, so the module keeps depending on
# 'stock' alone and gains precision when purchase/sale/mrp/pos are installed.
_OPTIONAL_LINK_COLUMNS = {
    "stock.move": ("purchase_line_id", "sale_line_id", "production_id",
                   "raw_material_production_id", "unbuild_id", "consume_unbuild_id"),
    "stock.picking": ("pos_order_id", "pos_session_id"),
}

class StockCardReportEngine(models.AbstractModel):
    _name = "stock.card.report.engine"
    _description = "Stock Card Report - SQL Engine"

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    @api.model
    def _normalize_options(self, raw):
        """Validate and coerce untrusted options (they may come from the web
        client) into a canonical dict with real ``date`` objects."""
        if not isinstance(raw, dict):
            raise UserError(_("Invalid Stock Card options."))

        def to_id(value):
            if isinstance(value, (list, tuple)):  # many2one as read() returns it
                value = value[0] if value else False
            return int(value) if value else False

        def to_ids(value):
            if not value:
                return []
            if not isinstance(value, (list, tuple)):
                raise ValueError("expected a list of ids")
            ids = set()
            for item in value:
                if isinstance(item, (list, tuple)):  # ORM command (6, 0, ids)
                    if len(item) == 3 and item[0] == 6:
                        ids.update(int(i) for i in item[2] or [])
                    continue
                if item:
                    ids.add(int(item))
            return sorted(ids)

        try:
            date_from = fields.Date.to_date(raw.get("date_from"))
            date_to = fields.Date.to_date(raw.get("date_to"))
            company_id = to_id(raw.get("company_id")) or self.env.company.id
            warehouse_id = to_id(raw.get("warehouse_id"))
            location_ids = to_ids(raw.get("location_ids"))
            product_ids = to_ids(raw.get("product_ids"))
            categ_ids = to_ids(raw.get("categ_ids"))
        except (TypeError, ValueError) as exc:
            raise UserError(_("Invalid Stock Card options: %s", exc)) from exc

        if not date_from or not date_to:
            raise UserError(_("Both 'Date From' and 'Date To' are required."))
        if date_from > date_to:
            raise UserError(_("'Date From' must be earlier than or equal to 'Date To'."))
        if not self.env.su and company_id not in self.env.user.company_ids.ids:
            raise AccessError(_("You are not allowed to access the stock data of this company."))
        if warehouse_id:
            warehouse = self.env["stock.warehouse"].browse(warehouse_id).exists()
            if not warehouse or warehouse.company_id.id != company_id:
                raise UserError(_("The selected warehouse does not belong to the selected company."))

        report_mode = raw.get("report_mode") or "detail"
        if report_mode not in REPORT_MODES:
            raise UserError(_("Invalid report type: %s", report_mode))
        interval = raw.get("interval") or "day"
        if interval not in INTERVALS:
            raise UserError(_("Invalid totals interval: %s", interval))
        lang = self._lang()
        week_start = int(lang.week_start) if lang and lang.week_start else 7

        tz = raw.get("tz") or self.env.context.get("tz") or self.env.user.tz or "UTC"
        if tz not in pytz.all_timezones_set:
            tz = "UTC"

        return {
            "date_from": date_from,
            "date_to": date_to,
            "company_id": company_id,
            "warehouse_id": warehouse_id,
            "location_ids": location_ids,
            "include_child_locations": bool(raw.get("include_child_locations", True)),
            "product_ids": product_ids,
            "categ_ids": categ_ids,
            "group_by_location": bool(raw.get("group_by_location", False)),
            "storable_only": bool(raw.get("storable_only", True)),
            "show_valuation": bool(raw.get("show_valuation", True)),
            "report_mode": report_mode,
            "interval": interval,
            # first day of week of the user's language (ISO 1=Mon .. 7=Sun)
            "week_start": week_start,
            "tz": tz,
        }

    @api.model
    def _options_to_json(self, opts):
        res = dict(opts)
        res["date_from"] = fields.Date.to_string(opts["date_from"])
        res["date_to"] = fields.Date.to_string(opts["date_to"])
        return res

    @api.model
    def _prepare(self, options):
        opts = self._normalize_options(options)
        self.env["stock.move.line"].check_access("read")  # Odoo 18 access API
        # Raw SQL below must see every pending ORM write.
        self.env.flush_all()
        return opts

    @api.model
    def _utc_bounds(self, opts):
        """Local-day boundaries of the period, expressed as naive UTC datetimes
        (the storage format of ``stock_move_line.date``)."""
        tz = pytz.timezone(opts["tz"])
        start = tz.localize(datetime.combine(opts["date_from"], time.min))
        end = tz.localize(datetime.combine(opts["date_to"] + timedelta(days=1), time.min))
        return (
            start.astimezone(pytz.utc).replace(tzinfo=None),
            end.astimezone(pytz.utc).replace(tzinfo=None),
        )

    @api.model
    def _qty_precision(self):
        return self.env["decimal.precision"].precision_get("Product Unit of Measure")

    # ------------------------------------------------------------------
    # SQL construction
    # ------------------------------------------------------------------
    @api.model
    def _build_legs_cte(self, opts, restrict_product_ids=None):
        """Return ``(sql, params)`` of the shared CTE, or ``(None, None)`` when
        the requested product restriction cannot match anything."""
        date_start, date_end = self._utc_bounds(opts)
        params = {
            "company_id": opts["company_id"],
            "date_start": date_start,
            "date_end": date_end,
            "by_location": opts["group_by_location"],
            "tz": opts["tz"],
        }

        scope_where = []
        if opts["location_ids"]:
            params["root_location_ids"] = opts["location_ids"]
            if opts["include_child_locations"]:
                scope_where.append(
                    "AND EXISTS (SELECT 1 FROM stock_location r"
                    " WHERE r.id = ANY(%(root_location_ids)s)"
                    " AND l.parent_path LIKE r.parent_path || '%%')"
                )
            else:
                scope_where.append("AND l.id = ANY(%(root_location_ids)s)")
        if opts["warehouse_id"]:
            params["warehouse_id"] = opts["warehouse_id"]
            scope_where.append(
                "AND l.parent_path LIKE ("
                " SELECT vl.parent_path FROM stock_warehouse w"
                " JOIN stock_location vl ON vl.id = w.view_location_id"
                " WHERE w.id = %(warehouse_id)s) || '%%'"
            )

        product_where = []
        if opts["storable_only"]:
            product_where.append("AND pt.is_storable")  # Odoo 18: goods with "Track Inventory"
        product_ids = opts["product_ids"]
        if restrict_product_ids is not None:
            wanted = set(restrict_product_ids)
            product_ids = sorted(wanted & set(product_ids)) if product_ids else sorted(wanted)
            if not product_ids:
                return None, None
        if product_ids:
            params["product_ids"] = product_ids
            product_where.append("AND sml.product_id = ANY(%(product_ids)s)")

        categ_join = ""
        if opts["categ_ids"]:
            params["categ_ids"] = opts["categ_ids"]
            categ_join = "JOIN product_category pc ON pc.id = pt.categ_id"
            product_where.append(
                "AND EXISTS (SELECT 1 FROM product_category c"
                " WHERE c.id = ANY(%(categ_ids)s)"
                " AND pc.parent_path LIKE c.parent_path || '%%')"
            )

        sql = _LEGS_CTE.format(
            scope_where="\n       ".join(scope_where),
            categ_join=categ_join,
            product_where="\n       ".join(product_where),
        )
        return sql, params

    # ------------------------------------------------------------------
    # Raw data access
    # ------------------------------------------------------------------
    @api.model
    def _fetch_summary(self, opts, restrict_product_ids=None):
        cte, params = self._build_legs_cte(opts, restrict_product_ids)
        if cte is None:
            return []
        self.env.cr.execute(cte + _SUMMARY_SQL, params)
        return self.env.cr.dictfetchall()

    @api.model
    def _fetch_lines(self, opts, restrict_product_ids=None, group_location_id=None, xlsx_extras=False):
        cte, params = self._build_legs_cte(opts, restrict_product_ids)
        if cte is None:
            return []
        group_where = ""
        if group_location_id is not None:
            params["group_location_id"] = int(group_location_id)
            group_where = "AND group_location_id = %(group_location_id)s"
        parts = self._move_type_sql()
        parts.update(self._extras_sql(parts["available"]) if xlsx_extras else {"extra_cols": "", "extra_joins_xlsx": ""})
        sql = _LINES_SQL.format(group_where=group_where, **parts)
        self.env.cr.execute(cte + sql, params)
        return self.env.cr.dictfetchall()

    @api.model
    def _fetch_buckets(self, opts, restrict_product_ids=None, group_location_id=None):
        cte, params = self._build_legs_cte(opts, restrict_product_ids)
        if cte is None:
            return []
        params.update(interval=opts["interval"], week_start=opts["week_start"], date_from=opts["date_from"])
        group_where = ""
        if group_location_id is not None:
            params["group_location_id"] = int(group_location_id)
            group_where = "AND group_location_id = %(group_location_id)s"
        self.env.cr.execute(cte + _BUCKETS_SQL.format(group_where=group_where), params)
        return self.env.cr.dictfetchall()

    @api.model
    def _report_lines(self, opts, restrict_product_ids=None, group_location_id=None, xlsx_extras=False):
        """Rows of the selected report type: ledger moves or period totals.
        ``xlsx_extras`` adds the Partner / Notes sources (detailed XLSX only)."""
        if opts["report_mode"] == "summary":
            return self._shape_buckets(opts, self._fetch_buckets(opts, restrict_product_ids, group_location_id))
        rows = self._fetch_lines(opts, restrict_product_ids, group_location_id, xlsx_extras=xlsx_extras)
        lines = self._shape_lines(opts, rows)
        if xlsx_extras:
            self._add_partner_and_notes(lines, rows)
        return lines

    # ------------------------------------------------------------------
    # Move type
    # ------------------------------------------------------------------
    @api.model
    def _available_link_columns(self):
        """Stored link columns present in this database (installed apps)."""
        available = set()
        for model, names in _OPTIONAL_LINK_COLUMNS.items():
            model_fields = self.env[model]._fields
            for name in names:
                field = model_fields.get(name)
                if field is not None and field.store:
                    available.add(name)
        return available

    @api.model
    def _invoice_links(self):
        """How bills / invoices are linked to order lines in this database:
        {'purchase': bool, 'sale': (relation, order_col, invoice_col) | None}."""
        aml_fields = self.env["account.move.line"]._fields if "account.move.line" in self.env else {}
        purchase = bool(aml_fields.get("purchase_line_id") and aml_fields["purchase_line_id"].store)
        sale = None
        if "sale.order.line" in self.env:
            field = self.env["sale.order.line"]._fields.get("invoice_lines")
            if field is not None and field.type == "many2many":
                names = (field.relation, field.column1, field.column2)
                if all(n and _SQL_IDENTIFIER.match(n) for n in names):
                    sale = names
        return {"purchase": purchase, "sale": sale}

    @api.model
    def _move_type_sql(self, available=None, invoice_links=None):
        """SQL fragments classifying each line into a move type code.

        Precedence: scrap > inventory adjustment > point of sale > unbuild >
        manufacturing > purchase / sale (with their returns) > generic return >
        production locations > transit > operation type > location usages.
        Labels are translated in Python (see _move_type_labels)."""
        cols = self._available_link_columns() if available is None else set(available)
        rules = [
            "WHEN COALESCE(dst.scrap_location, FALSE) OR COALESCE(sm.scrapped, FALSE) THEN 'scrap'",
            # inventory adjustment, split by the direction of this leg on the card:
            # IN leg = the adjustment increased the stock, OUT leg = decreased it
            "WHEN COALESCE(sm.is_inventory, FALSE) OR src.usage = 'inventory' OR dst.usage = 'inventory'"
            " THEN CASE WHEN p.leg_seq = 1 THEN 'inventory_in' ELSE 'inventory_out' END",
        ]
        extra_joins = ""
        pos_checks = ["sp.%s IS NOT NULL" % c for c in ("pos_order_id", "pos_session_id") if c in cols]
        if pos_checks:
            extra_joins = "LEFT JOIN stock_picking sp ON sp.id = p.picking_id"
            rules.append("WHEN %s THEN 'pos'" % " OR ".join(pos_checks))
        unbuild = ["sm.%s IS NOT NULL" % c for c in ("unbuild_id", "consume_unbuild_id") if c in cols]
        if unbuild:
            rules.append("WHEN %s THEN 'unbuild'" % " OR ".join(unbuild))
        if "production_id" in cols:
            rules.append("WHEN sm.production_id IS NOT NULL THEN 'mrp_produce'")
        if "raw_material_production_id" in cols:
            rules.append("WHEN sm.raw_material_production_id IS NOT NULL THEN 'mrp_consume'")
        if "purchase_line_id" in cols:
            rules.append(
                "WHEN sm.purchase_line_id IS NOT NULL THEN CASE WHEN dst.usage = 'supplier'"
                " OR sm.origin_returned_move_id IS NOT NULL THEN 'purchase_return' ELSE 'purchase' END")
        if "sale_line_id" in cols:
            rules.append(
                "WHEN sm.sale_line_id IS NOT NULL THEN CASE WHEN src.usage = 'customer'"
                " OR sm.origin_returned_move_id IS NOT NULL THEN 'sale_return' ELSE 'sale' END")
        rules += [
            "WHEN sm.origin_returned_move_id IS NOT NULL THEN CASE"
            " WHEN src.usage = 'customer' THEN 'sale_return'"
            " WHEN dst.usage = 'supplier' THEN 'purchase_return' ELSE 'return' END",
            "WHEN src.usage = 'production' THEN 'mrp_produce'",
            "WHEN dst.usage = 'production' THEN 'mrp_consume'",
            "WHEN src.usage = 'transit' OR dst.usage = 'transit' THEN 'transit'",
            "WHEN spt.code = 'incoming' THEN 'receipt'",
            "WHEN spt.code = 'outgoing' THEN 'delivery'",
            "WHEN spt.code = 'internal' THEN 'internal'",
            "WHEN src.usage = 'supplier' THEN 'receipt'",
            "WHEN dst.usage = 'customer' THEN 'delivery'",
            "WHEN src.usage = 'internal' AND dst.usage = 'internal' THEN 'internal'",
        ]
        links = self._invoice_links() if invoice_links is None else invoice_links
        sources = []
        if links.get("purchase") and "purchase_line_id" in cols:
            sources.append(_PURCHASE_INVOICES)
        if links.get("sale") and "sale_line_id" in cols:
            rel, order_col, invoice_col = links["sale"]
            sources.append(_SALE_INVOICES.format(rel=rel, order_col=order_col, invoice_col=invoice_col))
        if sources:
            invoice_join = _INVOICE_JOIN.format(sources="\n          UNION ALL".join(sources))
            invoice_cols = "inv.invoice_refs AS invoice_refs, inv.invoice_ids AS invoice_ids"
        else:
            invoice_join = ""
            invoice_cols = "NULL::varchar AS invoice_refs, NULL::int[] AS invoice_ids"
        return {
            "available": cols,
            "move_type": "CASE %s ELSE 'other' END" % " ".join(rules),
            "invoice_join": invoice_join,
            "invoice_cols": invoice_cols,
            "extra_joins": extra_joins,
            "src_virtual": _VIRTUAL_LOCATION.format(a="src"),
            "dst_virtual": _VIRTUAL_LOCATION.format(a="dst"),
        }

    @api.model
    def _extras_sql(self, available):
        """Partner / Notes sources, joined only for the detailed XLSX.

        Every candidate is fetched; _partner_for() picks the one matching the
        move type. All joins are on primary keys (the scrap via stock.move.scrap_id),
        so they cost one hash/merge join each, not one query per line."""
        # users (responsible, adjustment user, scrap creator) by their own name:
        # a user's contact is usually a child of the company ("YourCompany, Marc
        # Demo"); order partners keep their full name ("Azure Interior, Brandon")
        cols = [
            "spx.note                                    AS picking_note",
            "spx.id                                      AS picking_ref_id",
            "pup.name                                    AS picking_user",
            "COALESCE(spp.complete_name, spp.name)       AS picking_partner",
            "mup.name                                    AS move_user",
            "scp.name                                    AS scrap_user",
        ]
        joins = [
            "LEFT JOIN stock_picking spx ON spx.id = p.picking_id",
            "LEFT JOIN res_users     pu  ON pu.id = spx.user_id",
            "LEFT JOIN res_partner   pup ON pup.id = pu.partner_id",
            "LEFT JOIN res_partner   spp ON spp.id = spx.partner_id",
            "LEFT JOIN res_users     mu  ON mu.id = sm.create_uid",
            "LEFT JOIN res_partner   mup ON mup.id = mu.partner_id",
            "LEFT JOIN stock_scrap   scr ON scr.id = sm.scrap_id",
            "LEFT JOIN res_users     scu ON scu.id = scr.create_uid",
            "LEFT JOIN res_partner   scp ON scp.id = scu.partner_id",
        ]
        if "purchase_line_id" in available and "purchase.order.line" in self.env:
            cols.append("COALESCE(pop.complete_name, pop.name)       AS po_partner")
            joins += ["LEFT JOIN purchase_order_line pol ON pol.id = sm.purchase_line_id",
                      "LEFT JOIN purchase_order      po  ON po.id = pol.order_id",
                      "LEFT JOIN res_partner         pop ON pop.id = po.partner_id"]
        else:
            cols.append("NULL::varchar AS po_partner")
        if "sale_line_id" in available and "sale.order.line" in self.env:
            cols.append("COALESCE(sop.complete_name, sop.name)       AS so_partner")
            joins += ["LEFT JOIN sale_order_line sol ON sol.id = sm.sale_line_id",
                      "LEFT JOIN sale_order      so  ON so.id = sol.order_id",
                      "LEFT JOIN res_partner     sop ON sop.id = so.partner_id"]
        else:
            cols.append("NULL::varchar AS so_partner")
        return {
            "extra_cols": "".join(",\n       " + c for c in cols),
            "extra_joins_xlsx": "\n  ".join(joins),
        }

    @api.model
    def _partner_for(self, move_type, row):
        """Partner shown for a line, by move type:
        purchase -> PO vendor, sale -> SO customer, inventory adjustment ->
        user who applied it, scrap -> user who created the scrap, any other
        move of a transfer -> the transfer's responsible."""
        if move_type in ("purchase", "purchase_return"):
            return row.get("po_partner") or row.get("picking_partner")
        if move_type in ("sale", "sale_return"):
            return row.get("so_partner") or row.get("picking_partner")
        if move_type in ("inventory_in", "inventory_out"):
            return row.get("move_user")
        if move_type == "scrap":
            return row.get("scrap_user") or row.get("move_user")
        if row.get("picking_ref_id"):
            return row.get("picking_user")
        return ""

    @api.model
    def _add_partner_and_notes(self, lines, rows):
        notes = {}  # picking id -> plain text (one conversion per transfer)
        for line, row in zip(lines, rows):
            line["partner_name"] = self._partner_for(line["move_type"], row) or ""
            picking_id = row.get("picking_ref_id")
            if picking_id and picking_id not in notes:
                notes[picking_id] = html2plaintext(row["picking_note"]).strip() if row.get("picking_note") else ""
            line["picking_note"] = notes.get(picking_id, "") if picking_id else ""

    @api.model
    def _move_type_labels(self):
        return {
            "purchase": _("Purchase Order"),
            "purchase_return": _("Purchase Return"),
            "sale": _("Sale Order"),
            "sale_return": _("Sales Return"),
            "pos": _("Point of Sale"),
            "mrp_produce": _("Manufacturing"),
            "mrp_consume": _("Manufacturing Consumption"),
            "unbuild": _("Unbuild"),
            "scrap": _("Scrap"),
            "inventory_in": _("Inventory Adjustment Increase"),
            "inventory_out": _("Inventory Adjustment Decrease"),
            "return": _("Return"),
            "receipt": _("Receipt"),
            "delivery": _("Delivery"),
            "internal": _("Internal Transfer"),
            "transit": _("Inter-Warehouse Transfer"),
            "other": _("Other"),
        }

    # ------------------------------------------------------------------
    # Shaping
    # ------------------------------------------------------------------
    @api.model
    def _group_key(self, product_id, group_location_id):
        return "%s_%s" % (product_id, group_location_id or 0)

    @api.model
    def _lang(self):
        return self.env["res.lang"]._lang_get(self.env.lang or self.env.user.lang or "en_US")

    @api.model
    def _format_local_datetime(self, value, cache=None):
        """Format an already-localised naive datetime with the language's date
        format (babel: month names in the user's language, e.g. Arabic), so an
        RTL report never mixes an English month into Arabic text. The date part
        is memoised per day: formatting 500k lines costs a few babel calls."""
        if not value:
            return ""
        day = value.date()
        if cache is None or day not in cache:
            formatted = format_date(self.env, day)
            if cache is None:
                return "%s %s" % (formatted, value.strftime("%H:%M"))
            cache[day] = formatted
        return "%s %s" % (cache[day], value.strftime("%H:%M"))

    @api.model
    def _currency(self, opts):
        return self.env["res.company"].browse(opts["company_id"]).currency_id

    @api.model
    def _unit_costs(self, opts, product_ids):
        """Unit cost of each product = its Cost (``standard_price``) in the
        report's company: one batched read, whatever the number of lines.
        For AVCO/FIFO products Odoo keeps this field equal to the current
        average / FIFO unit cost; for Standard Price products it is the standard."""
        products = self.env["product.product"].with_company(opts["company_id"]).with_context(
            active_test=False).browse(sorted(product_ids))
        return {p.id: p.standard_price for p in products}

    @api.model
    def _shape_groups(self, opts, rows):
        precision = self._qty_precision()
        currency = self._currency(opts)
        costs = self._unit_costs(opts, {r["product_id"] for r in rows})
        products = self.env["product.product"].with_context(active_test=False).browse(
            sorted({r["product_id"] for r in rows})
        )
        locations = self.env["stock.location"].with_context(active_test=False).browse(
            sorted({r["group_location_id"] for r in rows if r["group_location_id"]})
        )
        # display_name / complete_name are fetched in batch (prefetch).
        product_map = {p.id: p for p in products}
        location_map = {l.id: l for l in locations}

        groups = []
        for row in rows:
            product = product_map[row["product_id"]]
            location = location_map.get(row["group_location_id"])
            opening = float_round(row["opening"] or 0.0, precision_digits=precision)
            qty_in = float_round(row["qty_in"] or 0.0, precision_digits=precision)
            qty_out = float_round(row["qty_out"] or 0.0, precision_digits=precision)
            closing = float_round(row["closing"] or 0.0, precision_digits=precision)
            unit_cost = costs.get(product.id, 0.0)
            groups.append({
                "key": self._group_key(row["product_id"], row["group_location_id"]),
                "product_id": product.id,
                "product_name": product.display_name,
                "product_code": product.default_code or "",
                "uom_name": product.uom_id.name,
                "location_id": location.id if location else False,
                "location_name": location.complete_name if location else "",
                "opening": opening,
                "qty_in": qty_in,
                "qty_out": qty_out,
                "closing": closing,
                "move_count": row["move_count"],
                "unit_cost": unit_cost,
                "opening_value": currency.round(opening * unit_cost),
                "closing_value": currency.round(closing * unit_cost),
            })
        groups.sort(key=lambda g: (g["product_name"].lower(), g["location_name"].lower(), g["key"]))
        return groups

    @api.model
    def _shape_lines(self, opts, rows):
        precision = self._qty_precision()
        currency = self._currency(opts)
        costs = self._unit_costs(opts, {r["product_id"] for r in rows})
        labels = self._move_type_labels()
        invoice_labels = {
            "purchase": _("Vendor Bill"),
            "purchase_return": _("Vendor Credit Note"),
            "sale": _("Customer Invoice"),
            "sale_return": _("Customer Credit Note"),
        }
        date_cache = {}
        lines = []
        for row in rows:
            date_local = row["date_local"]
            running_balance = float_round(row["running_balance"] or 0.0, precision_digits=precision)
            move_type = row["move_type"] or "other"
            lines.append({
                "uid": "%s_%s" % (row["line_id"], row["leg_seq"]),
                "key": self._group_key(row["product_id"], row["group_location_id"]),
                "line_id": row["line_id"],
                "move_id": row["move_id"] or False,
                "picking_id": row["picking_id"] or False,
                "product_id": row["product_id"],
                "date_local": date_local,
                "date_display": self._format_local_datetime(date_local, date_cache),
                "reference": row["reference"] or "",
                # scraps (and some other moves) carry their own name as origin
                "origin": "" if row["origin"] == row["reference"] else (row["origin"] or ""),
                "move_type": move_type,
                "move_type_label": labels.get(move_type, labels["other"]),
                "invoice_refs": row.get("invoice_refs") or "",
                "invoice_label": invoice_labels.get(move_type, ""),
                "invoice_ids": list(row.get("invoice_ids") or []),
                "src_id": row["src_id"],
                "src_virtual": bool(row["src_virtual"]),
                "src_name": row["src_name"] or "",
                "dst_id": row["dst_id"],
                "dst_virtual": bool(row["dst_virtual"]),
                "dst_name": row["dst_name"] or "",
                "qty_in": float_round(row["qty_in"] or 0.0, precision_digits=precision),
                "qty_out": float_round(row["qty_out"] or 0.0, precision_digits=precision),
                "running_balance": running_balance,
                "balance_value": currency.round(running_balance * costs.get(row["product_id"], 0.0)),
            })
        return lines

    @api.model
    def _bucket_label(self, opts, start, end):
        """Display label of a period bucket, clipped to the report period."""
        start = max(start, opts["date_from"])
        end = min(end, opts["date_to"])
        if opts["interval"] == "day" or start == end:
            return format_date(self.env, start)
        if opts["interval"] == "month" and start.day == 1 and (end + timedelta(days=1)).day == 1:
            return format_date(self.env, start, date_format="MMMM y")  # full month: "October 2026"
        return "%s – %s" % (format_date(self.env, start), format_date(self.env, end))

    @api.model
    def _shape_buckets(self, opts, rows):
        precision = self._qty_precision()
        currency = self._currency(opts)
        costs = self._unit_costs(opts, {r["product_id"] for r in rows})
        lines = []
        for row in rows:
            start = row["bucket"]
            if opts["interval"] == "period":
                end = opts["date_to"]
            elif opts["interval"] == "week":
                end = start + timedelta(days=6)
            elif opts["interval"] == "month":
                end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            else:
                end = start
            key = self._group_key(row["product_id"], row["group_location_id"])
            running_balance = float_round(row["running_balance"] or 0.0, precision_digits=precision)
            lines.append({
                "uid": "%s_%s" % (key, start.isoformat()),
                "key": key,
                "product_id": row["product_id"],
                "bucket": start,
                "bucket_end": end,
                "date_local": start,
                "date_display": self._bucket_label(opts, start, end),
                "qty_in": float_round(row["qty_in"] or 0.0, precision_digits=precision),
                "qty_out": float_round(row["qty_out"] or 0.0, precision_digits=precision),
                "move_count": row["move_count"],
                "running_balance": running_balance,
                "balance_value": currency.round(running_balance * costs.get(row["product_id"], 0.0)),
            })
        return lines

    @api.model
    def _mode_label(self, opts):
        if opts["report_mode"] == "detail":
            return _("Detailed Moves")
        return {
            "day": _("Daily Totals"),
            "week": _("Weekly Totals"),
            "month": _("Monthly Totals"),
            "period": _("Full Period Totals"),
        }[opts["interval"]]

    @api.model
    def _build_meta(self, opts, groups):
        company = self.env["res.company"].browse(opts["company_id"])
        currency = company.currency_id
        # locations_lines: the same scope as short single-direction lines for the
        # PDF (QtWebKit mis-measures a line mixing Arabic and Latin text)
        if opts["location_ids"]:
            locations = self.env["stock.location"].browse(opts["location_ids"])
            names = ", ".join(locations.mapped("complete_name"))
            locations_label = names
            locations_lines = [names]
            if opts["include_child_locations"]:
                locations_label += " " + _("(incl. sub-locations)")
                locations_lines.append(_("Including sub-locations"))
        elif opts["warehouse_id"]:
            warehouse_name = self.env["stock.warehouse"].browse(opts["warehouse_id"]).display_name
            locations_label = _("All internal locations of %s", warehouse_name)
            locations_lines = [_("All internal locations of"), warehouse_name]
        else:
            locations_label = _("All internal locations")
            locations_lines = [locations_label]
        products_label = (
            ", ".join(self.env["product.product"].browse(opts["product_ids"]).mapped("display_name"))
            if opts["product_ids"] else _("All products")
        )
        categories_label = (
            ", ".join(self.env["product.category"].browse(opts["categ_ids"]).mapped("complete_name"))
            if opts["categ_ids"] else _("All categories")
        )
        now_local = fields.Datetime.now().replace(tzinfo=pytz.utc).astimezone(
            pytz.timezone(opts["tz"])).replace(tzinfo=None)
        lang = self._lang()
        date_from_display = format_date(self.env, opts["date_from"])
        date_to_display = format_date(self.env, opts["date_to"])
        return {
            "company_id": company.id,
            "company_name": company.name,
            "date_from": fields.Date.to_string(opts["date_from"]),
            "date_to": fields.Date.to_string(opts["date_to"]),
            "date_from_display": date_from_display,
            "date_to_display": date_to_display,
            "period_label": _("%(from)s → %(to)s", **{"from": date_from_display, "to": date_to_display}),
            "locations_label": locations_label,
            "locations_lines": locations_lines,
            "products_label": products_label,
            "categories_label": categories_label,
            "group_by_location": opts["group_by_location"],
            "tz": opts["tz"],
            "precision": self._qty_precision(),
            "group_count": len(groups),
            "move_count": sum(g["move_count"] for g in groups),
            "generated_at": self._format_local_datetime(now_local),
            "lang": lang.code if lang else "en_US",
            "direction": (lang.direction if lang else False) or "ltr",
            "show_valuation": opts["show_valuation"],
            "report_mode": opts["report_mode"],
            "interval": opts["interval"],
            "mode_label": self._mode_label(opts),
            "currency_id": currency.id,
            "currency_name": currency.name,
            "currency_symbol": currency.symbol,
            "currency_position": currency.position,
            "currency_digits": currency.decimal_places,
            "price_precision": self.env["decimal.precision"].precision_get("Product Price"),
            "total_opening_value": currency.round(sum(g["opening_value"] for g in groups)),
            "total_closing_value": currency.round(sum(g["closing_value"] for g in groups)),
        }

    # ------------------------------------------------------------------
    # Public API (Python)
    # ------------------------------------------------------------------
    @api.model
    def get_summary(self, options, product_ids=None):
        """One row per product (and per location when split by location)."""
        opts = self._prepare(options)
        return self._shape_groups(opts, self._fetch_summary(opts, product_ids))

    @api.model
    def get_lines(self, options, product_ids=None, group_location_id=None):
        """Ledger lines of the period with their running balance."""
        opts = self._prepare(options)
        return self._report_lines(opts, product_ids, group_location_id)

    @api.model
    def get_report_data(self, options, xlsx_extras=False):
        """Complete report (summary + lines grouped) used by PDF and XLSX.
        ``xlsx_extras``: add Partner / Notes to detailed lines (XLSX only)."""
        opts = self._prepare(options)
        groups = self._shape_groups(opts, self._fetch_summary(opts))
        lines = self._report_lines(opts, xlsx_extras=xlsx_extras)
        by_key = {g["key"]: g for g in groups}
        for group in groups:
            group["lines"] = []
        for line in lines:
            group = by_key.get(line["key"])
            if group is not None:
                group["lines"].append(line)
        precision = self._qty_precision()
        for group in groups:
            if group["lines"] and float_compare(
                group["lines"][-1]["running_balance"], group["closing"], precision_digits=precision
            ):
                _logger.warning("Stock card: running balance / closing mismatch for %s", group["key"])
        return {
            "options": self._options_to_json(opts),
            "meta": self._build_meta(opts, groups),
            "groups": groups,
        }

    # ------------------------------------------------------------------
    # Public API (JSON-RPC, used by the OWL client action)
    # ------------------------------------------------------------------
    @api.model
    def rpc_get_summary(self, options):
        opts = self._prepare(options)
        groups = self._shape_groups(opts, self._fetch_summary(opts))
        return {
            "options": self._options_to_json(opts),
            "meta": self._build_meta(opts, groups),
            "groups": groups,
        }

    @api.model
    def rpc_get_lines(self, options, product_id=False, location_id=False):
        """Lines grouped by group key. ``product_id``/``location_id`` limit the
        query to one group (drill-down); without them every group is returned."""
        opts = self._prepare(options)
        product_ids = [int(product_id)] if product_id else None
        group_location_id = int(location_id or 0) if product_id else None
        lines = self._report_lines(opts, product_ids, group_location_id)
        result = {}
        for line in lines:
            if line.get("bucket"):
                line["bucket"] = fields.Date.to_string(line["bucket"])
                line["bucket_end"] = fields.Date.to_string(line["bucket_end"])
                line["date_local"] = line["bucket"]
            else:
                line["date_local"] = fields.Datetime.to_string(line["date_local"]) if line["date_local"] else False
            result.setdefault(line["key"], []).append(line)
        return result
