import logging
import json
import os
import time
from datetime import datetime

import azure.functions as func
import requests
from openai import OpenAI

# Document Intelligence
DI_ENDPOINT = os.getenv("DOCUMENT_INTELLIGENCE_ENDPOINT")
DI_KEY = os.getenv("DOCUMENT_INTELLIGENCE_KEY")

# Foundry (Azure AI)
FOUNDRY_ENDPOINT = os.getenv("FOUNDRY_ENDPOINT")      # https://xxx.services.ai.azure.com/openai/v1
FOUNDRY_API_KEY = os.getenv("FOUNDRY_API_KEY")
FOUNDRY_DEPLOYMENT = os.getenv("FOUNDRY_DEPLOYMENT")  # gpt-4.1-mini-1

client = OpenAI(
    base_url=FOUNDRY_ENDPOINT,
    api_key=FOUNDRY_API_KEY
)


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("OCRWorker HTTP Trigger")

    body = req.get_json()
    upload_id = body["upload_id"]
    file_id = body["file_id"]
    blob_url = body["blob_url"]

    # --- ① AIOCR 呼び出し（非同期） ---
    analyze_url = (
        f"{DI_ENDPOINT}/formrecognizer/documentModels/"
        f"prebuilt-read:analyze?api-version=2023-07-31"
    )

    headers = {
        "Ocp-Apim-Subscription-Key": DI_KEY,
        "Content-Type": "application/json",
    }

    payload = {"urlSource": blob_url}

    post_response = requests.post(analyze_url, headers=headers, json=payload)
    operation_location = post_response.headers.get("Operation-Location")

    if not operation_location:
        logging.error("Operation-Location header missing")
        return func.HttpResponse("Invalid DI response", status_code=500)

    # --- ② ポーリング ---
    result = None
    for _ in range(30):
        get_response = requests.get(operation_location, headers=headers)
        try:
            result_json = get_response.json()
        except Exception:
            time.sleep(1)
            continue

        if result_json.get("status") == "succeeded":
            result = result_json
            break

        time.sleep(1)

    if result is None:
        logging.error("DI polling failed")
        return func.HttpResponse("DI polling failed", status_code=500)

    # --- ③ AIOCR の全文テキスト抽出 ---
    analyze_result = result.get("analyzeResult", {})
    text_lines = []

    for page in analyze_result.get("pages", []):
        for line in page.get("lines", []):
            text_lines.append(line.get("content", ""))

    full_text = "\n".join(text_lines)

    # ★★★ OCR全文テキストをログ出力 ★★★
    logging.info("=== OCR Full Text Start ===")
    logging.info(full_text)
    logging.info("=== OCR Full Text End ===")

    # --- ④ Foundry GPT-4.1-mini に抽出依頼（responses API） ---
    prompt = f"""
以下は資格証の OCR 結果です。
この中から以下を抽出してください：

- qualification_name（資格名）
- organization_name（認定組織）
- expiration_date（有効期限）

抽出できない場合は「不明」としてください。

OCR結果:
{full_text}

出力は必ず JSON で返してください。
"""

    try:
        response = client.responses.create(
            model=FOUNDRY_DEPLOYMENT,
            input=prompt
        )

        # ★★★ Foundry の生レスポンスをログ出力 ★★★
        logging.info("=== Foundry Raw Response Start ===")
        logging.info(f"answer: {response.output[0]}")
        logging.info("=== Foundry Raw Response End ===")

        # Foundry responses API の構造
        content = response.output[0].content[0].text
        
        # ★★★ LLM の生 JSON をログ出力 ★★★
        logging.info("=== LLM JSON Text Start ===")
        logging.info(content)
        logging.info("=== LLM JSON Text End ===")

        # ★ Markdown の ```json ... ``` を除去して JSON を抽出する
        import re
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
           raise ValueError("JSON not found in LLM output")
        
        json_text = match.group(0)
        parsed = json.loads(json_text)
        
        qualification_name = parsed.get("qualification_name", "不明")
        organization_name = parsed.get("organization_name", "不明")
        expiration_date = parsed.get("expiration_date", "不明")

    except Exception as e:
        logging.error(f"LLM error: {e}")
        qualification_name = "不明"
        organization_name = "不明"
        expiration_date = "不明"

    # --- ⑤ IngestionFunction に返却する JSON ---
    response_json = {
        "upload_id": upload_id,
        "file_id": file_id,
        "qualification_name": qualification_name,
        "organization_name": organization_name,
        "expiration_date": expiration_date,
        "status": "OCR_DONE",
        "ocr_timestamp": datetime.utcnow().isoformat(),
    }

    return func.HttpResponse(
        json.dumps(response_json, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )
