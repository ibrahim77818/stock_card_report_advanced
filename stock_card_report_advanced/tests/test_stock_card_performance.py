# -*- coding: utf-8 -*-
"""Performance and concurrency tests.

* ``TestStockCardPerformance`` (standard suite): bulk-clones thousands of done
  move lines with one INSERT ... SELECT generate_series, then proves that
  - the number of SQL queries issued by the engine does not grow with volume,
  - the window-function running balance is exact on every single row,
  - the engine is strictly read-only (only SELECT / WITH statements),
  - the whole report is produced well within a time budget.
* ``TestStockCardConcurrency`` (opt-in, ``--test-tags stock_card_stress``):
  hammers the engine from many threads, each with its own database cursor,
  on committed data, and checks that every reader gets identical results.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.sql_db import Cursor
from odoo.tests import tagged

from .common import StockCardCommon

# Generous by design: CI machines vary. Override with STOCK_CARD_TIME_BUDGET.
TIME_BUDGET = float(os.environ.get("STOCK_CARD_TIME_BUDGET", "10"))


@contextmanager
def capture_queries():
    queries = []
    original = Cursor.execute

    def spy(cr, query, params=None, log_exceptions=True):
        # Odoo 17 passes odoo.tools.SQL objects as well as plain strings
        queries.append(str(getattr(query, "code", query)))
        return original(cr, query, params, log_exceptions)

    with patch.object(Cursor, "execute", spy):
        yield queries


@tagged("post_install", "-at_install", "stock_card", "stock_card_perf")
class TestStockCardPerformance(StockCardCommon):

    def _bulk_clone(self, template_line, count, start):
        """Clone ``template_line`` ``count`` times, one minute apart starting at
        ``start``. Every third clone is reversed (an OUT), the others are INs.
        All columns are copied generically so the test survives schema tweaks."""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'stock_move_line'
               AND column_name NOT IN ('id', 'date', 'location_id', 'location_dest_id',
                                       'quantity', 'quantity_product_uom')
        """)
        columns = ['"%s"' % row[0] for row in cr.fetchall()]
        cr.execute("""
            INSERT INTO stock_move_line ({cols}, date, location_id, location_dest_id,
                                         quantity, quantity_product_uom)
            SELECT {tcols},
                   %(start)s + (g * INTERVAL '1 minute'),
                   CASE WHEN g %% 3 = 0 THEN t.location_dest_id ELSE t.location_id END,
                   CASE WHEN g %% 3 = 0 THEN t.location_id ELSE t.location_dest_id END,
                   1, 1
              FROM stock_move_line t, generate_series(1, %(count)s) AS g
             WHERE t.id = %(tid)s
        """.format(cols=", ".join(columns), tcols=", ".join("t." + c for c in columns)),
            {"start": start, "count": count, "tid": template_line.id})
        self.env.invalidate_all()

    def _expected_balances(self, opening, count):
        balances, balance = [], opening
        for g in range(1, count + 1):
            balance += -1 if g % 3 == 0 else 1
            balances.append(balance)
        return balances

    def test_01_constant_query_count_and_exact_balances(self):
        small, large = 30, 6000
        product_small = self._create_product("SC Perf Small", "SC-PS", self.categ_root)
        product_large = self._create_product("SC Perf Large", "SC-PL", self.categ_root)
        start = datetime(2026, 1, 1, 0, 0)
        for product, count in ((product_small, small), (product_large, large)):
            move = self._move(product, self.supplier, self.stock, 10, "2025-12-01 10:00:00")
            self._bulk_clone(move.move_line_ids, count, start)

        def run(product):
            options = self._options(product_ids=[product.id])
            self.env.flush_all()
            self.env.invalidate_all()  # same cold-cache conditions for every run
            with capture_queries() as queries:
                summary = self.engine.get_summary(options)
                lines = self.engine.get_lines(options)
            return summary, lines, queries

        # Warm caches (decimal precision, lang, access rights) before counting.
        run(product_small)
        _s, _l, queries_small = run(product_small)

        started = time.perf_counter()
        summary, lines, queries_large = run(product_large)
        elapsed = time.perf_counter() - started

        self.assertEqual(
            len(queries_large), len(queries_small),
            "query count must not depend on data volume (%s vs %s)" % (len(queries_large), len(queries_small)),
        )
        self.assertLess(elapsed, TIME_BUDGET, "stock card took %.2fs for %s lines" % (elapsed, large))

        outs = large // 3
        card = summary[0]
        self.assertEqual(card["opening"], 10)
        self.assertEqual(card["qty_in"], large - outs)
        self.assertEqual(card["qty_out"], outs)
        self.assertEqual(card["closing"], 10 + large - 2 * outs)
        self.assertEqual(len(lines), large)
        self.assertEqual([l["running_balance"] for l in lines], self._expected_balances(10, large))

    def test_02_engine_is_read_only(self):
        self._move(self.product, self.supplier, self.stock, 5, "2026-01-05 10:00:00")
        self.env.flush_all()
        with capture_queries() as queries:
            self.engine.get_report_data(self._options())
            self.engine.rpc_get_summary(self._options(group_by_location=True))
        self.assertTrue(queries)
        for query in queries:
            head = query.lstrip().split(None, 1)[0].upper()
            self.assertIn(head, ("SELECT", "WITH"), "engine issued a non-read statement: %s" % query[:120])

    def test_03_interleaved_writes_keep_results_consistent(self):
        """Readers interleaved with writers: every read reflects exactly the
        committed-so-far state, totals always reconcile."""
        expected_closing = 0
        for idx in range(1, 6):
            self._move(self.product, self.supplier, self.stock, idx, "2026-01-%02d 10:00:00" % idx)
            expected_closing += idx
            card = self._single_group(self._options())
            self.assertEqual(card["closing"], expected_closing)
            self.assertLedgerConsistent(self._options())


@tagged("-standard", "post_install", "-at_install", "stock_card_stress")
class TestStockCardConcurrency(StockCardCommon):
    """Opt-in: run with ``--test-tags stock_card_stress``.

    Each worker opens its own database cursor (own transaction / snapshot) and
    reads the *committed* data of the database, so the test is meaningful on a
    copy of a production database too. It never writes.
    """

    WORKERS = int(os.environ.get("STOCK_CARD_STRESS_WORKERS", "8"))
    ROUNDS = int(os.environ.get("STOCK_CARD_STRESS_ROUNDS", "32"))

    def test_parallel_readers_get_identical_results(self):
        registry = self.registry
        options = {
            "date_from": "2000-01-01",
            "date_to": "2100-12-31",
            "company_id": self.env.ref("base.main_company").id,
            "warehouse_id": False,
            "location_ids": [],
            "include_child_locations": True,
            "product_ids": [],
            "categ_ids": [],
            "group_by_location": True,
            "storable_only": True,
            "tz": "UTC",
        }

        def worker(_round):
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                engine = env["stock.card.report.engine"]
                summary = engine.get_summary(options)
                lines = engine.get_lines(options)
                cr.rollback()
            return (
                [(g["key"], g["opening"], g["qty_in"], g["qty_out"], g["closing"]) for g in summary],
                [(l["uid"], l["running_balance"]) for l in lines],
            )

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            results = list(pool.map(worker, range(self.ROUNDS)))
        elapsed = time.perf_counter() - started

        reference = results[0]
        for result in results[1:]:
            self.assertEqual(result, reference)
        self.assertLess(elapsed, TIME_BUDGET * 6)
