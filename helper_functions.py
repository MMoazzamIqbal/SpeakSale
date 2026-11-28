import datetime as dt
import difflib
import re
import socket
import pandas as pd


def has_internet_connection(host="8.8.8.8", port=53, timeout=3.0):
    """Check whether outbound network connectivity is available."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


NETWORK_TOKENS = (
    "name or service not known", "getaddrinfo failed", "temporary failure in name resolution",
    "network is unreachable", "connection refused", "connection reset",
    "timed out", "read operation timed out",
)

def is_network_error(exc):
    while exc is not None:                      # also check the original cause
        if isinstance(exc, (socket.gaierror, TimeoutError, ConnectionError)):
            return True
        if any(token in str(exc).casefold() for token in NETWORK_TOKENS):
            return True
        exc = exc.__cause__
    return False


def compute_order_timestamp(recorded_at, offset_mm_ss):
    try:
        minutes_str, seconds_str = offset_mm_ss.split(":")
        offset_seconds = int(minutes_str) * 60 + float(seconds_str)
    except (ValueError, AttributeError, IndexError):
        offset_seconds = 0.0

    return (recorded_at + dt.timedelta(seconds=offset_seconds)).isoformat()


def normalize_tokens(text):
    if text is None or pd.isna(text):
        return []
    return re.findall(r"[a-z0-9]+", str(text).casefold())


def keyword_tokenization(catalog_matching_df):
    # Precompute tokenized keyword sets once for fast candidate matching
    catalog_matching_df["keyword_tokens"] = (
        catalog_matching_df[["name", "brand", "subcategory_name", "alias_text"]]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
        .map(lambda text: set(normalize_tokens(text)))
    )
    return catalog_matching_df


def find_product_candidates(product_reference, catalog_df):
    from config import MAX_CANDIDATES

    max_candidates = MAX_CANDIDATES
    reference_tokens = set(normalize_tokens(product_reference))
    if not reference_tokens:
        return []

    def score_keyword_set(catalog_tokens):
        if not catalog_tokens:
            return 0.0
        total_similarity = sum(
            max(difflib.SequenceMatcher(None, ref_token, cat_token).ratio() for cat_token in catalog_tokens)
            for ref_token in reference_tokens
        )
        return round((total_similarity / len(reference_tokens)) * 100, 1)

    match_scores = catalog_df["keyword_tokens"].map(score_keyword_set)
    top_matches_df = (
        catalog_df.loc[match_scores > 0]
        .assign(match_score=match_scores)
        .nlargest(max_candidates, "match_score")
    )

    return top_matches_df[
        ["product_id", "name", "brand", "subcategory_name", "alias_text", "attributes"]
    ].to_dict(orient="records")


def pricing_and_discounts(customer_obj, pricing_lookup):
    # 1. Calculate base quantity, unit price, and cost for each item
    priced_lines = []
    customer_type = customer_obj.get("customer_type")
    customer_items = customer_obj.get("items", [])

    for item in customer_items:
        ai_decision = item.get("ai_decision", {})
        product_id = ai_decision.get("product_id")
        product_pricing = pricing_lookup[product_id]
        package_qty_in_base = product_pricing["package_qty_in_base"]

        # Quantity in pieces
        quantity_unit = item.get("quantity_unit")
        if quantity_unit is None:
            raise ValueError(f"No unit spoken for {product_id}")

        quantity = item.get("quantity")
        base_quantity = quantity if quantity_unit == "piece" else quantity * package_qty_in_base

        # Price per piece
        spoken_price = item.get("spoken_price")
        if spoken_price is not None:
            price_source = "spoken"
            price_unit = item.get("spoken_price_unit") or quantity_unit

            if item.get("price_is_total"):
                unit_price = spoken_price / base_quantity
            elif price_unit == "piece":
                unit_price = spoken_price
            else:
                unit_price = spoken_price / package_qty_in_base
        else:
            price_source = "db"
            unit_price = (
                product_pricing["wholesale_price"]
                if customer_type == "wholesaler"
                else product_pricing["full_price"]
            )

        attributes_to_save = ai_decision.get("attributes_to_save", [])
        variant_dict = {attr["key"]: attr["value"] for attr in attributes_to_save}

        priced_lines.append({
            "product_id": product_id,
            "base_quantity": base_quantity,
            "unit_price": unit_price,
            "full_price": product_pricing["full_price"],
            "cost_price": product_pricing["current_cost_price"],
            "price_source": price_source,
            "variant": variant_dict,
        })

    # 2. Distribute discount proportionally by profit if total_paid was spoken
    calculated_bill_total = sum(line["base_quantity"] * line["unit_price"] for line in priced_lines)
    total_paid = customer_obj.get("total_paid")
    if total_paid is not None:
        discount_amount = calculated_bill_total - total_paid
        line_profits = [
            (line["unit_price"] - line["cost_price"]) * line["base_quantity"]
            for line in priced_lines
        ]
        total_profit = sum(line_profits)

        if total_profit != 0:
            for line, line_profit in zip(priced_lines, line_profits):
                profit_share = discount_amount * line_profit / total_profit
                line["unit_price"] -= profit_share / line["base_quantity"]

    sale_item_rows = [
        {
            "product_id": line["product_id"],
            "quantity": line["base_quantity"],
            "unit": "piece",
            "unit_price": line["unit_price"],
            "price_source": line["price_source"],
            "cost_price_at_sale": line["cost_price"],
            "discount_pct": (
                round(
                    (
                        float(line["full_price"])
                        - float(line["unit_price"])
                    )
                    / float(line["full_price"])
                    * 100,
                    2,
                )
                if line["full_price"] is not None
                and not pd.isna(line["full_price"])
                and float(line["full_price"]) != 0
                else None
            ),
            "variant": line["variant"],
        }
        for line in priced_lines
    ]

    return sale_item_rows
