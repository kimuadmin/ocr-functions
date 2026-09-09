import logging
import json
import os
from datetime import datetime, timedelta

import azure.functions as func
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
    ContentSettings,
)
import requests

from sqlalchemy import text, create_engine
from sqlalchemy.orm import sessionmaker

# --- DB 接続 ---
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# --- Blob 接続 ---
BLOB_CONNECTION_STRING = os.getenv("BLOB_CONNECTION_STRING")
blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)

UPLOAD_CONTAINER_NAME = os.getenv("UPLOAD_CONTAINER_NAME", "upload-container")
REVIEW_CONTAINER_NAME = os.getenv("REVIEW_CONTAINER_NAME", "review-container")

# --- OCR Worker URL ---
OCRWORKER_URL = os.getenv("OCRWORKER_URL")

# --- SAS 用 ---
STORAGE_ACCOUNT_NAME = os.getenv("STORAGE_ACCOUNT_NAME")
STORAGE_ACCOUNT_KEY = os.getenv("STORAGE_ACCOUNT_KEY")


def is_allowed_org(org: str) -> bool:
    allowed = ["Amazon", "Amazon Web Services", "aws", "Microsoft", "マイクロソフト"]
    return org in allowed


def main(blob: func.InputStream):
    logging.info(f"[Ingestion] Triggered: {blob.name}")

    # uploadId / fileId 抽出
    parts = blob.name.split("/")
    upload_id = parts[1]
    file_name = parts[2]
    file_id = os.path.splitext(file_name)[0]

    # --- ① review-container にコピー ---
    try:
        review_blob_client = blob_service_client.get_blob_client(
            REVIEW_CONTAINER_NAME, f"{upload_id}/{file_name}"
        )
        review_blob_client.upload_blob(
            blob.read(),
            overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )
        review_blob_url = review_blob_client.url
        logging.info(f"[Ingestion] Copied to review-container: {review_blob_url}")

    except Exception as e:
        logging.error(f"[Ingestion] Blob copy failed: {e}")
        return

    # --- ② SAS 生成 ---
    sas_token = generate_blob_sas(
        account_name=STORAGE_ACCOUNT_NAME,
        container_name=REVIEW_CONTAINER_NAME,
        blob_name=f"{upload_id}/{file_name}",
        account_key=STORAGE_ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=1),
    )
    sas_url = f"{review_blob_url}?{sas_token}"

    # --- ③ DB 初期登録 or 既存取得 ---
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT document FROM review_documents WHERE upload_id = :id"),
            {"id": upload_id},
        ).fetchone()

        if row:
            document_json = row[0]
            logging.info("[Ingestion] Existing record found.")
        else:
            logging.info("[Ingestion] Creating new record.")

            document_json = {
                "upload_id": upload_id,
                "source": "UPLOAD_UI",
                "status": "RECEIVED",
                "files": [
                    {
                        "file_id": file_id,
                        "file_name": file_name,
                        "blob_path": f"{REVIEW_CONTAINER_NAME}/{upload_id}/{file_name}",
                        "external_api_source": None,
                        "status": "RECEIVED",
                        "error": None,
                    }
                ],
                "ocr_results": [],
                "auto_review": {
                    "status": "",
                    "reason": "",
                    "timestamp": "",
                },
                "human_review": {
                    "status": "",
                    "reason": None,
                    "comment": "",
                    "reviewer_user_id": "",
                    "timestamp": "",
                },
                "finalized": [],
                "updated_at": datetime.utcnow().isoformat(),
                "intake_id": "",
            }

            db.execute(
                text(
                    """
                    INSERT INTO review_documents (
                        upload_id,
                        blob_name,
                        blob_url,
                        source,
                        document,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :upload_id,
                        :blob_name,
                        :blob_url,
                        :source,
                        :document,
                        'RECEIVED',
                        NOW(),
                        NOW()
                    )
                    """
                ),
                {
                    "upload_id": upload_id,
                    "blob_name": file_name,
                    "blob_url": review_blob_url,
                    "source": "UPLOAD_UI",
                    "document": json.dumps(document_json),
                },
            )
            db.commit()

    except Exception as e:
        logging.error(f"[Ingestion] DB error: {e}")
        db.rollback()
        raise

    finally:
        db.close()

    # --- ④ OCR Worker 呼び出し ---
    payload = {
        "upload_id": upload_id,
        "file_id": file_id,
        "blob_url": sas_url,
    }

    logging.info(f"[Ingestion] Calling OCR Worker: {OCRWORKER_URL}")
    response = requests.post(OCRWORKER_URL, json=payload)

    if response.status_code != 200:
        logging.error(f"[Ingestion] OCR Worker failed: {response.text}")
        return

    ocr_json = response.json()

    # --- ⑤ OCR 結果を JSONB に反映 ---
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT document FROM review_documents WHERE upload_id = :id"),
            {"id": upload_id},
        ).fetchone()

        if not row:
            logging.error("[Ingestion] DB record missing during update.")
            return

        document_json = row[0]

        # files[] 更新
        for f in document_json.get("files", []):
            if f["file_id"] == file_id:
                f["status"] = "OCR_DONE"
                f["error"] = None

        # --- ocr_results[] 追加（仕様完全準拠） ---
        ocr_entry = {
            "file_id": file_id,
            "qualification_name": ocr_json.get("qualification_name", "不明"),
            "organization_name": ocr_json.get("organization_name", "不明"),
            "expiration_date": ocr_json.get("expiration_date", "不明"),

            # ★ raw_ocr_json（CamelCase）仕様準拠
            "raw_ocr_json": {
                "qualificationName": ocr_json.get("qualification_name", "不明"),
                "organizationName": ocr_json.get("organization_name", "不明"),
                "expirationDate": ocr_json.get("expiration_date", "不明"),
            },

            "status": "OCR_DONE",
            "ocr_timestamp": ocr_json.get("ocr_timestamp"),
        }

        document_json["ocr_results"].append(ocr_entry)

        # --- auto_review 判定 ---
        org = ocr_json.get("organization_name", "不明")

        if is_allowed_org(org):
            document_json["auto_review"] = {
                "status": "登録可",
                "reason": "資格名にAWSまたはMicrosoftを含む",
                "timestamp": datetime.utcnow().isoformat(),
            }
        else:
            document_json["auto_review"] = {
                "status": "登録不可",
                "reason": "資格がアマゾン・Microsoft以外です",
                "timestamp": datetime.utcnow().isoformat(),
            }

        # 全体ステータス更新
        document_json["status"] = "OCR_DONE"
        document_json["updated_at"] = datetime.utcnow().isoformat()

        # DB UPDATE
        db.execute(
            text(
                """
                UPDATE review_documents
                SET
                    document = :document,
                    status = 'OCR_DONE',
                    updated_at = NOW()
                WHERE upload_id = :upload_id
                """
            ),
            {
                "document": json.dumps(document_json),
                "upload_id": upload_id,
            },
        )

        db.commit()

    except Exception as e:
        logging.error(f"[Ingestion] DB update error: {e}")
        db.rollback()
        raise

    finally:
        db.close()

    logging.info("[Ingestion] JSONB update completed.")
