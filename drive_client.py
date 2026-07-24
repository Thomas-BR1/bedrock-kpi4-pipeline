"""
Thin Google Drive wrapper used by both fetch_hcp_csv.py (Actions) and run_kpi4.py (Render).

Reads a service-account JSON blob from env var GOOGLE_SERVICE_ACCOUNT and returns
a preconfigured googleapiclient service.
"""
from __future__ import annotations

import io
import json
import os
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
    if not raw:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT env var is empty.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT is not valid JSON: %s" % e)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(service, local_path: str, folder_id: str,
                mime_type: Optional[str] = None, name: Optional[str] = None) -> str:
    """Upload a local file into a Drive folder. Returns the new file's ID."""
    body = {
        "name": name or os.path.basename(local_path),
        "parents": [folder_id],
    }
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=False)
    resp = service.files().create(body=body, media_body=media, fields="id, name").execute()
    print("Drive: uploaded %s (%s)" % (resp["name"], resp["id"]))
    return resp["id"]


def latest_csv_in_folder(service, folder_id: str) -> Optional[dict]:
    """Return the newest .csv file in a folder, or None if empty."""
    q = ("'%s' in parents and trashed = false and "
         "(mimeType='text/csv' or name contains '.csv')") % folder_id
    resp = service.files().list(
        q=q, orderBy="modifiedTime desc", pageSize=1,
        fields="files(id, name, modifiedTime)",
    ).execute()
    files = resp.get("files") or []
    return files[0] if files else None


def download_file(service, file_id: str, local_path: str) -> None:
    req = service.files().get_media(fileId=file_id)
    with io.FileIO(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    print("Drive: downloaded %s" % local_path)


def export_google_file(service, file_id: str, mime_type: str, local_path: str) -> None:
    """Export a Google-native file (Sheets, Docs) to a specific mime type."""
    req = service.files().export_media(fileId=file_id, mimeType=mime_type)
    with io.FileIO(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    print("Drive: exported Google file -> %s" % local_path)


def replace_google_sheet_from_xlsx(service, file_id: str, xlsx_path: str) -> None:
    """
    Replace the contents of an existing Google Sheet by uploading a new .xlsx
    in its place. Preserves the file ID (and thus the sharing URL).
    """
    media = MediaFileUpload(
        xlsx_path,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=False,
    )
    service.files().update(fileId=file_id, media_body=media).execute()
    print("Drive: replaced sheet %s with %s" % (file_id, xlsx_path))
