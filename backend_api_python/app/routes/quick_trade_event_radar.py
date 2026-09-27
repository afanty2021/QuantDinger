"""Reference-only Event Radar endpoints for Quick Trade."""

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.event_radar import EventRadarError, get_event_radar_service
from app.utils.auth import login_required
from app.utils.logger import get_logger

logger = get_logger(__name__)

quick_trade_event_radar_blp = Blueprint("quick_trade_event_radar", __name__)


@quick_trade_event_radar_blp.route("/event-radar", methods=["GET"])
@login_required
def get_event_radar():
    """Return Event Radar availability and the latest reference analysis."""
    symbol = str(request.args.get("symbol") or "").strip()
    market_type = str(request.args.get("market_type") or "").strip()
    data = get_event_radar_service().get_status(int(g.user_id), symbol, market_type)
    return jsonify({"code": 1, "msg": "common.success", "data": data})


@quick_trade_event_radar_blp.route("/event-radar/analyze", methods=["POST"])
@login_required
def analyze_event_radar():
    """Run a paid, reference-only event analysis for the current instrument."""
    payload = request.get_json(silent=True) or {}
    try:
        data = get_event_radar_service().analyze(
            int(g.user_id),
            str(payload.get("symbol") or ""),
            str(payload.get("market_type") or ""),
        )
        return jsonify({"code": 1, "msg": "common.success", "data": data})
    except EventRadarError as exc:
        return jsonify({"code": 0, "msg": exc.code, "data": exc.details}), exc.status
    except Exception as exc:
        logger.exception("Event Radar analysis failed")
        return jsonify({"code": 0, "msg": "event_radar_failed", "data": {"error": str(exc)}}), 500
