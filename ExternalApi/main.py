from fastapi import FastAPI, HTTPException
from azure.storage.blob import BlobServiceClient
import os

app = FastAPI()

BLOB_CONNECTION_STRING = os.getenv(BLOB_CONNECTION_STRING)
UPLOAD_CONTAINER_NAME = os.getenv(UPLOAD_CONTAINER_NAME, upload-container)

blob_service = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
upload_container = blob_service.get_container_client(UPLOAD_CONTAINER_NAME)


@app.get(mockexternal-apilist)
async def list_files()
    try
        blobs = upload_container.list_blobs()

        files = []
        for blob in blobs
            files.append({
                blob_name blob.name,
                blob_url f{upload_container.url}{blob.name}
            })

        return {files files}

    except Exception as e
        raise HTTPException(status_code=500, detail=str(e))
