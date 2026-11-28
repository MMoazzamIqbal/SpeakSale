import datetime as dt
import json
import os
import pandas as pd
from psycopg2.errors import UniqueViolation
from sqlalchemy import create_engine


db_engine = create_engine(os.environ["SHOP_DB_URL"])


def insert_recording(recording, status="received"):
    """Return (file_id, was_inserted) for a recording path."""
    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO recordings (
                    file_path,
                    recorded_at,
                    status,
                    duration_seconds,
                    issue,
                    error_log,
                    raw_transcript,
                    processed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING file_id;
                """,
                (
                    recording["file_path"],
                    recording.get("recorded_at"),
                    status,
                    recording.get("duration_seconds"),
                    None,
                    json.dumps([]),
                    None,
                    None,
                ),
            )
            file_id = cursor.fetchone()[0]

        conn.commit()
        return file_id, True
    except UniqueViolation:
        conn.rollback()

        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT file_id
                FROM recordings
                WHERE file_path = %s
                """,
                (recording["file_path"],),
            )
            existing_row = cursor.fetchone()

        if existing_row is None:
            raise

        return existing_row[0], False

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_recording(file_id):
    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT file_id, file_path, recorded_at, status, issue, error_log,
                       raw_transcript, extracted_json, processed_at
                FROM recordings
                WHERE file_id = %s
                """,
                (file_id,),
            )
            row = cursor.fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if row is None:
        raise ValueError(f"no recording with file_id {file_id}")

    return {
        "file_id": row[0],
        "file_path": row[1],
        "recorded_at": row[2],
        "status": row[3],
        "issue": row[4],
        "error_log": row[5],
        "raw_transcript": row[6],
        "extracted_json": row[7],
        "processed_at": row[8],
    }


def append_recording_issue(file_id, problem_type, error_message, status=None):
    """Append a structured problem entry to recordings.error_log and update issue/status."""
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    log_entry = {
        "type": problem_type,
        "message": error_message,
        "at": timestamp,
    }
    formatted_issue = f"[{timestamp}] ({problem_type}) {error_message}"

    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE recordings
                SET
                    issue = CASE
                        WHEN issue IS NULL OR issue = '' THEN %s
                        ELSE issue || E'\\n' || %s
                    END,
                    error_log = COALESCE(error_log, '[]'::jsonb) || %s::jsonb,
                    status = COALESCE(%s, status)
                WHERE file_id = %s;
                """,
                (
                    formatted_issue,
                    formatted_issue,
                    json.dumps([log_entry], ensure_ascii=False),
                    status,
                    file_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_recording_transcript_and_status(file_id, raw_transcript, status):
    """Save raw_transcript and update status (plus processed_at when status is 'processed')."""
    set_processed_at = status == "processed"

    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE recordings
                SET
                    raw_transcript = COALESCE(%s, raw_transcript),
                    status = COALESCE(%s, status),
                    processed_at = CASE
                        WHEN %s THEN NOW()
                        ELSE processed_at
                    END
                WHERE file_id = %s;
                """,
                (raw_transcript, status, set_processed_at, file_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_extracted_json(file_id, extracted_order):
    """Persist the extraction so a retry can resume without another AI call."""
    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE recordings
                SET extracted_json = %s::jsonb
                WHERE file_id = %s;
                """,
                (json.dumps(extracted_order, ensure_ascii=False, default=str), file_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_catalog_dfs():
    products_df = pd.read_sql(
        """
        SELECT p.product_id, p.name, p.brand, p.attributes,
               COALESCE(array_to_string(p.alias, ' '), '') AS alias_text,
               s.name AS subcategory_name, p.full_price, p.wholesale_price,
               p.base_unit, p.package_unit, p.package_qty_in_base,
               p.current_cost_price
        FROM products p
        LEFT JOIN subcategories s ON s.subcategory_id = p.subcategory_id
        WHERE p.is_active = TRUE
        """,
        db_engine,
    )
    catalog_matching_df = products_df[
        ["product_id", "name", "brand", "subcategory_name", "alias_text", "attributes"]
    ].copy()
    catalog_pricing_df = products_df[
        ["product_id", "full_price", "wholesale_price", "base_unit",
         "package_unit", "package_qty_in_base", "current_cost_price"]
    ].copy()
    return catalog_matching_df, catalog_pricing_df


def queue_customer_for_review(
    customer_obj,
    review_reason,
    raw_transcript,
    audio_source,
    status="pending",
    file_id=None,
    extracted_order=None,
):
    """Build the review payload and insert an unresolved customer order into pending_customers."""
    review_payload = {
        "customer": customer_obj,
        "raw_transcript": raw_transcript,
        "audio_source": audio_source,
    }

    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pending_customers (status, problem, customer, resolved_at, resolved_sale_id)
                VALUES (%s, %s, %s::jsonb, %s, %s)
                RETURNING pending_id
                """,
                (
                    status,
                    review_reason,
                    json.dumps(review_payload, ensure_ascii=False, default=str),
                    None,
                    None,
                ),
            )
            pending_id = cursor.fetchone()[0]

            if file_id is not None and extracted_order is not None:
                cursor.execute(
                    """
                    UPDATE recordings
                    SET extracted_json = %s::jsonb
                    WHERE file_id = %s;
                    """,
                    (
                        json.dumps(extracted_order, ensure_ascii=False, default=str),
                        file_id,
                    ),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"{customer_obj.get('customer_label')} sent for review [Reason: {review_reason}]")
    return pending_id


def insert_sale_with_items(sale_row, sale_item_rows, file_id=None, extracted_order=None):
    """Insert one sale and all its items in one transaction."""
    file_id = sale_row.get("file_id")

    if file_id is not None:
        file_id = int(file_id)

    conn = db_engine.raw_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sales (sold_at, customer_type, customer_label, file_id)
                VALUES (%s, %s, %s, %s)
                RETURNING sale_id
                """,
                (
                    sale_row["created_at"],
                    sale_row["customer_type"],
                    sale_row["customer_label"],
                    file_id,
                ),
            )
            sale_id = cursor.fetchone()[0]

            for item_row in sale_item_rows:
                cursor.execute(
                    """
                    INSERT INTO sale_items
                        (sale_id, product_id, quantity, unit, unit_price,
                         price_source, cost_price_at_sale, variant, discount_pct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sale_id,
                        item_row["product_id"],
                        float(item_row["quantity"]),
                        item_row["unit"],
                        float(item_row["unit_price"]),
                        item_row["price_source"],
                        float(item_row["cost_price_at_sale"]),
                        json.dumps(item_row["variant"]),
                        (
                            None
                            if item_row.get("discount_pct") is None
                            else float(item_row["discount_pct"])
                        ),
                    ),
                )

            if file_id is not None and extracted_order is not None:
                cursor.execute(
                    """
                    UPDATE recordings
                    SET extracted_json = %s::jsonb
                    WHERE file_id = %s;
                    """,
                    (
                        json.dumps(extracted_order, ensure_ascii=False, default=str),
                        file_id,
                    ),
                )

        conn.commit()
        return sale_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


