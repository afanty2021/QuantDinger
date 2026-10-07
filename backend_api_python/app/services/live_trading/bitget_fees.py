"""Bitget REST reports debits as negative amounts; keep native fee currencies."""

import json
from decimal import Decimal


def fee_breakdown(raw, *, received_currency=""):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, (dict, list)):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("newFees"), dict):
        details = raw["newFees"]
        result = {}
        if details.get("d") not in (None, ""):
            bgb_fee = -float(details["d"])
            if bgb_fee != 0:
                result["BGB"] = bgb_fee
        if details.get("r") not in (None, ""):
            result[received_currency or "UNKNOWN"] = -float(details["r"])
        return result
    entries = (
        raw
        if isinstance(raw, list)
        else [raw]
        if "totalFee" in raw or "fee" in raw
        else [
            dict(value, feeCoin=value.get("feeCoinCode") or key)
            for key, value in raw.items()
            if isinstance(value, dict)
        ]
    )
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        currency = str(
            entry.get("feeCoin") or entry.get("feeCcy") or entry.get("feeCoinCode") or received_currency or "UNKNOWN"
        ).upper()
        value = entry.get("totalFee")
        if value is None:
            value = entry.get("fee")
        if value is None:
            continue
        fee = -Decimal(str(value or 0))
        if not fee.is_finite():
            raise ValueError("Non-finite execution fee")
        result[currency] = result.get(currency, 0.0) + float(fee)
    return result


def fee_storage(raw, *, received_currency=""):
    fees = fee_breakdown(raw, received_currency=received_currency)
    if len(fees) == 1:
        currency, amount = next(iter(fees.items()))
        return Decimal(str(amount)), currency
    return Decimal("0"), "MIXED" if fees else ""
