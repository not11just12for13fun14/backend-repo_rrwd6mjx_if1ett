import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Google APIs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================
# Google Drive/Sheets helpers
# =====================
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
FOLDER_NAME = "Don't_Delete_This"
SHEET_TITLE = "Student_Records"
HEADER = ["Name", "Class", "Roll No", "Subject", "Saved At"]


def get_google_services():
    """Create Drive and Sheets service clients from a service account JSON stored in env.

    Expect env var GOOGLE_SERVICE_ACCOUNT_JSON to contain the full JSON string for the
    service account credentials. The service account must have access to the Drive where
    the folder/file will be created. For personal Drives, share the folder with the
    service account email.
    """
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise HTTPException(status_code=500, detail="Google credentials not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON env var with service account JSON.")

    import json
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    return drive_service, sheets_service


def ensure_folder(drive_service) -> str:
    """Find or create the target folder. Returns folder ID."""
    query = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    res = drive_service.files().list(q=query, fields="files(id,name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    # Create folder
    file_metadata = {
        "name": FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = drive_service.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]


def ensure_sheet_in_folder(drive_service, sheets_service, folder_id: str) -> str:
    """Find or create a Google Sheet inside the folder. Returns spreadsheetId."""
    query = f"name = '{SHEET_TITLE}' and mimeType = 'application/vnd.google-apps.spreadsheet' and '{folder_id}' in parents and trashed = false"
    res = drive_service.files().list(q=query, fields="files(id,name)").execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    # Create the sheet
    spreadsheet = {
        "properties": {"title": SHEET_TITLE}
    }
    sheet = sheets_service.spreadsheets().create(body=spreadsheet, fields="spreadsheetId").execute()
    sheet_id = sheet["spreadsheetId"]
    # Move into folder
    drive_service.files().update(fileId=sheet_id, addParents=folder_id, removeParents="", fields="id, parents").execute()
    # Write header
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="A1:E1",
        valueInputOption="RAW",
        body={"values": [HEADER]},
    ).execute()
    return sheet_id


# =====================
# Models
# =====================
class Record(BaseModel):
    name: str = Field(..., min_length=1)
    klass: str = Field(..., alias="class", min_length=1)
    rollno: str = Field(..., min_length=1)
    subject: str = Field(..., min_length=1)

    class Config:
        populate_by_name = True


class BatchRecords(BaseModel):
    records: List[Record]


# =====================
# Routes
# =====================
@app.get("/")
def read_root():
    return {"message": "Google Drive Student Records API"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_services():
    try:
        drive, sheets = get_google_services()
        folder_id = ensure_folder(drive)
        sheet_id = ensure_sheet_in_folder(drive, sheets, folder_id)
        return {"backend": "running", "folder": FOLDER_NAME, "folder_id": folder_id, "sheet_id": sheet_id}
    except HttpError as e:
        raise HTTPException(status_code=500, detail=f"Google API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/records")
def add_record(record: Record):
    try:
        drive, sheets = get_google_services()
        folder_id = ensure_folder(drive)
        sheet_id = ensure_sheet_in_folder(drive, sheets, folder_id)
        from datetime import datetime
        row = [record.name, record.klass, record.rollno, record.subject, datetime.utcnow().isoformat()] 
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="A:E",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return {"status": "saved"}
    except HttpError as e:
        raise HTTPException(status_code=500, detail=f"Google API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/records/batch")
def add_records(batch: BatchRecords):
    try:
        if not batch.records:
            raise HTTPException(status_code=400, detail="No records provided")
        drive, sheets = get_google_services()
        folder_id = ensure_folder(drive)
        sheet_id = ensure_sheet_in_folder(drive, sheets, folder_id)
        from datetime import datetime
        now = datetime.utcnow().isoformat()
        values = [[r.name, r.klass, r.rollno, r.subject, now] for r in batch.records]
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="A:E",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        return {"status": "saved", "count": len(values)}
    except HttpError as e:
        raise HTTPException(status_code=500, detail=f"Google API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/records")
def list_records():
    try:
        drive, sheets = get_google_services()
        folder_id = ensure_folder(drive)
        sheet_id = ensure_sheet_in_folder(drive, sheets, folder_id)
        result = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range="A2:E").execute()
        values = result.get("values", [])
        data = [
            {"name": r[0] if len(r) > 0 else "", "class": r[1] if len(r) > 1 else "", "rollno": r[2] if len(r) > 2 else "", "subject": r[3] if len(r) > 3 else "", "saved_at": r[4] if len(r) > 4 else ""}
            for r in values
        ]
        return {"items": data}
    except HttpError as e:
        raise HTTPException(status_code=500, detail=f"Google API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
