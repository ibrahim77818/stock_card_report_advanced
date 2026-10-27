# -*- coding: utf-8 -*-
import json

from werkzeug.exceptions import BadRequest, Forbidden
from werkzeug.wrappers import Response
from werkzeug.wsgi import wrap_file

from odoo import http
from odoo.http import content_disposition, request

XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class StockCardReportController(http.Controller):

    @http.route("/stock_card_report_advanced/xlsx", type="http", auth="user", methods=["GET", "POST"])
    def stock_card_xlsx(self, options=None, **kwargs):
        try:
            raw_options = json.loads(options or "{}")
        except ValueError as exc:
            raise BadRequest("Invalid options") from exc
        if not request.env.user.has_group("stock.group_stock_user"):
            raise Forbidden()

        filename, fileobj, size = request.env["stock.card.report.xlsx"].generate_xlsx(raw_options)
        headers = [
            ("Content-Type", XLSX_MIMETYPE),
            ("Content-Disposition", content_disposition(filename)),
            ("Content-Length", str(size)),
            ("Cache-Control", "no-store"),
        ]
        # Stream the temp file instead of loading the whole workbook in memory;
        # the WSGI file wrapper closes (and thereby deletes) it when done.
        return Response(
            wrap_file(request.httprequest.environ, fileobj),
            headers=headers,
            direct_passthrough=True,
        )
