import json

from helper_functions import (
    has_internet_connection,
    is_network_error,
    compute_order_timestamp,
    find_product_candidates,
    pricing_and_discounts,
    keyword_tokenization,
)
from ai_pipeline import extract_orders_from_audio, disambiguate_items_with_ai
from db import (
    load_catalog_dfs,
    append_recording_issue,
    save_extracted_json,
    update_recording_transcript_and_status,
    queue_customer_for_review,
    insert_sale_with_items,
)


def send_customer_to_review(
    customer,
    review_reason,
    raw_transcript,
    audio_source,
    file_id,
    extracted_order,
):
    """Mark one customer processed and persist it in the review queue."""
    customer["customer_processed"] = True
    return queue_customer_for_review(
        customer_obj=customer,
        review_reason=review_reason,
        raw_transcript=raw_transcript,
        audio_source=audio_source,
        file_id=file_id,
        extracted_order=extracted_order,
    )


def format_customer_error(exc):
    """Create a short, useful review reason for a customer-level exception."""
    detail = str(exc).strip()
    first_line = detail.splitlines()[0][:150] if detail else "no details"
    return f"customer_processing_error: {type(exc).__name__}: {first_line}"


def final_pipeline(recording):
    file_id = recording.get("file_id")
    recorded_at = recording.get("recorded_at")
    file_path = recording.get("file_path")
    cached_extracted_order = recording.get("extracted_json")

    if not has_internet_connection():
        if file_id is not None:
            append_recording_issue(
                file_id=file_id,
                problem_type="internet_loss",
                error_message="No internet connection before AI processing.",
                status="received",
            )
        raise ConnectionError(f"No internet connection while processing file_id={file_id}")

    if cached_extracted_order:
        if isinstance(cached_extracted_order, str):
            extracted_order = json.loads(cached_extracted_order)
        else:
            extracted_order = cached_extracted_order
    else:
        try:
            extracted_order = extract_orders_from_audio(file_path)
        except Exception as exc:
            if file_id is not None:
                problem_type = "internet_loss" if is_network_error(exc) else "ai_extraction_error"
                next_status = "received" if is_network_error(exc) else "failed"
                append_recording_issue(
                    file_id=file_id,
                    problem_type=problem_type,
                    error_message=f"First AI extraction failed: {exc}",
                    status=next_status,
                )
            raise

        if file_id is not None:
            save_extracted_json(file_id, extracted_order)

    raw_transcript = extracted_order.get("raw_transcript")
    customers = extracted_order.get("customers", [])

    if not customers:
        if file_id is not None:
            update_recording_transcript_and_status(
                file_id=file_id,
                raw_transcript=raw_transcript,
                status="no_customers",
            )
        return

    if file_id is not None:
        update_recording_transcript_and_status(
            file_id=file_id,
            raw_transcript=raw_transcript,
            status=None,
        )

    catalog_matching_df, catalog_pricing_df = load_catalog_dfs()
    catalog_matching_df = keyword_tokenization(catalog_matching_df)
    pricing_by_product_id = catalog_pricing_df.set_index("product_id").to_dict(orient="index")

    unnamed_counter = 1
    for customer in customers:
        if customer.get("customer_processed") is True:
            continue

        raw_customer_label = customer.get("customer_label")
        customer_type = customer.get("customer_type")
        order_start_time = customer.get("order_start_time")
        invoice_request = customer.get("invoice_request")
        customer_items = customer.get("items", [])

        if raw_customer_label:
            customer_label = raw_customer_label.strip()
        else:
            customer_label = f"U{unnamed_counter}"
            unnamed_counter += 1

        customer_created_at = compute_order_timestamp(recorded_at, order_start_time)
        customer["customer_label"] = customer_label
        customer["created_at"] = customer_created_at

        # 1. Shortlist database candidates for each item in this customer's order.
        candidate_batch = []
        try:
            item_index = 1
            for item in customer_items:
                spoken_phrase_transcript = item.get("spoken_phrase_transcript")
                product_reference = item.get("product_reference")
                item_local_id = item_index
                item_index += 1

                item["item_local_id"] = item_local_id
                candidates = find_product_candidates(product_reference, catalog_matching_df)

                candidate_batch.append({
                    "item_local_id": item_local_id,
                    "spoken_transcript": spoken_phrase_transcript,
                    "product_reference": product_reference,
                    "candidates": candidates,
                })
        except Exception as exc:
            send_customer_to_review(
                customer=customer,
                review_reason=format_customer_error(exc),
                raw_transcript=raw_transcript,
                audio_source=file_path,
                file_id=file_id,
                extracted_order=extracted_order,
            )
            continue

        # 2. Disambiguate shortlisted items in a single AI call per customer.
        # AI/network failures are recording-level failures, not customer review items.
        try:
            disambiguation_result = (
                disambiguate_items_with_ai(candidate_batch)
                if candidate_batch
                else {}
            )
        except Exception as exc:
            if file_id is not None:
                network_problem = is_network_error(exc)
                append_recording_issue(
                    file_id=file_id,
                    problem_type=(
                        "internet_loss"
                        if network_problem
                        else "ai_disambiguation_error"
                    ),
                    error_message=f"Disambiguation AI call failed: {exc}",
                    status="received" if network_problem else "failed",
                )
            raise

        # 3. Attach decisions and identify product-match review cases.
        try:
            decisions = disambiguation_result.get("decisions", [])
            decisions_by_item_id = {
                decision["item_id"]: decision
                for decision in decisions
            }
            for item in customer_items:
                current_item_id = item["item_local_id"]
                item["ai_decision"] = decisions_by_item_id.get(current_item_id, {})

            if not customer_items:
                raise ValueError("customer has no items")

            requires_review = any(
                item["ai_decision"].get("status") != "matched"
                for item in customer_items
            )
        except Exception as exc:
            send_customer_to_review(
                customer=customer,
                review_reason=format_customer_error(exc),
                raw_transcript=raw_transcript,
                audio_source=file_path,
                file_id=file_id,
                extracted_order=extracted_order,
            )
            continue

        if requires_review:
            send_customer_to_review(
                customer=customer,
                review_reason="product_could_not_match",
                raw_transcript=raw_transcript,
                audio_source=file_path,
                file_id=file_id,
                extracted_order=extracted_order,
            )
            continue

        # 4. Price and save matched customer order in one transaction.
        try:
            sale_item_rows = pricing_and_discounts(customer, pricing_by_product_id)

            sale_row = {
                "customer_label": customer_label,
                "customer_type": customer_type,
                "file_id": file_id,
                "created_at": customer_created_at,
            }
            customer["customer_processed"] = True
            sale_id = insert_sale_with_items(
                sale_row=sale_row,
                sale_item_rows=sale_item_rows,
                file_id=file_id,
                extracted_order=extracted_order,
            )
        except Exception as exc:
            send_customer_to_review(
                customer=customer,
                review_reason=format_customer_error(exc),
                raw_transcript=raw_transcript,
                audio_source=file_path,
                file_id=file_id,
                extracted_order=extracted_order,
            )
            continue

        if invoice_request is True:
            # print_invoice(sale_id)
            # not made yet
            pass

    if file_id is not None:
        update_recording_transcript_and_status(
            file_id=file_id,
            raw_transcript=raw_transcript,
            status="processed",
        )
