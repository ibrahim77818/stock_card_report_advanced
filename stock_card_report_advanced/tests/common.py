# -*- coding: utf-8 -*-
from datetime import datetime

from odoo.tests.common import TransactionCase


class StockCardCommon(TransactionCase):
    """Shared fixtures: an isolated product family moving in and out of the
    main warehouse of the current company, dated in a fixed past period so the
    assertions never depend on demo data or on today's date."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.engine = cls.env["stock.card.report.engine"]
        cls.company = cls.env.company
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.stock = cls.warehouse.lot_stock_id
        cls.shelf = cls.env["stock.location"].create({
            "name": "SC Shelf A",
            "location_id": cls.stock.id,
            "usage": "internal",
        })
        cls.supplier = cls.env.ref("stock.stock_location_suppliers")
        cls.customer = cls.env.ref("stock.stock_location_customers")

        cls.uom_unit = cls.env.ref("uom.product_uom_unit")
        cls.uom_dozen = cls.env.ref("uom.product_uom_dozen")
        cls.uom_kg = cls.env.ref("uom.product_uom_kgm")
        cls.uom_gram = cls.env.ref("uom.product_uom_gram")

        cls.categ_root = cls.env["product.category"].create({"name": "SC Root"})
        cls.categ_child = cls.env["product.category"].create({
            "name": "SC Child", "parent_id": cls.categ_root.id,
        })
        cls.categ_other = cls.env["product.category"].create({"name": "SC Other"})

        cls.product = cls._create_product("SC Widget", "SC-W", cls.categ_child)
        cls.product_b = cls._create_product("SC Bolt", "SC-B", cls.categ_other)
        cls.product_kg = cls._create_product("SC Flour", "SC-F", cls.categ_root, uom=cls.uom_kg)

    @classmethod
    def _create_product(cls, name, code, categ, uom=None):
        uom = uom or cls.uom_unit
        return cls.env["product.product"].create({
            "name": name,
            "default_code": code,
            "detailed_type": "product",
            "categ_id": categ.id,
            "uom_id": uom.id,
            "uom_po_id": uom.id,
        })

    def _move(self, product, src, dst, qty, date, uom=None, company=None):
        """Create, validate and back-date a stock move (and its move line)."""
        move = self.env["stock.move"].create({
            "name": "SC test move",
            "product_id": product.id,
            "product_uom_qty": qty,
            "product_uom": (uom or product.uom_id).id,
            "location_id": src.id,
            "location_dest_id": dst.id,
            "company_id": (company or self.company).id,
        })
        move._action_confirm()
        # Odoo 17: the done quantity is `quantity` and must be flagged `picked`
        move.write({"quantity": qty, "picked": True})
        move._action_done()
        self.assertEqual(move.state, "done")
        if isinstance(date, str):
            date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
        move.write({"date": date})
        move.move_line_ids.write({"date": date})
        return move

    def _options(self, **overrides):
        options = {
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "company_id": self.company.id,
            "warehouse_id": False,
            "location_ids": [self.stock.id],
            "include_child_locations": True,
            "product_ids": [self.product.id],
            "categ_ids": [],
            "group_by_location": False,
            "storable_only": True,
            "tz": "UTC",
        }
        options.update(overrides)
        return options

    def _single_group(self, options):
        groups = self.engine.get_summary(options)
        self.assertEqual(len(groups), 1, "expected exactly one stock card")
        return groups[0]

    def assertLedgerConsistent(self, options):
        """Opening + In - Out = Closing for every card, and the last running
        balance of each card equals its closing balance."""
        data = self.engine.get_report_data(options)
        for group in data["groups"]:
            self.assertAlmostEqual(group["opening"] + group["qty_in"] - group["qty_out"], group["closing"], places=6)
            self.assertAlmostEqual(sum(l["qty_in"] for l in group["lines"]), group["qty_in"], places=6)
            self.assertAlmostEqual(sum(l["qty_out"] for l in group["lines"]), group["qty_out"], places=6)
            balance = group["opening"]
            for line in group["lines"]:
                balance += line["qty_in"] - line["qty_out"]
                self.assertAlmostEqual(line["running_balance"], balance, places=6)
            if group["lines"]:
                self.assertAlmostEqual(group["lines"][-1]["running_balance"], group["closing"], places=6)
        return data
