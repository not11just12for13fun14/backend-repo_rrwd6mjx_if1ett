import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from dotenv import load_dotenv
load_dotenv()

import requests
# Google APIs
from google.oauth2.service_account import Credentials as SACredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import Flow

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

# In-memory storage for a single user's OAuth credentials (demo purpose)
OAUTH_CREDS: Optional[UserCredentials] = None


def get_google_services():
    """Create Drive and Sheets service clients.

    Preference order:
    1) Use stored OAuth user credentials if available (after login)
    2) Fallback to service account JSON from env GOOGLE_SERVICE_ACCOUNT_JSON
    """
    global OAUTH_CREDS

    if OAUTH_CREDS and OAUTH_CREDS.valid:
        drive_service = build("drive", "v3", credentials=OAUTH_CREDS)
        sheets_service = build("sheets", "v4", credentials=OAUTH_CREDS)
        return drive_service, sheets_service

    # Try to refresh if expired and refresh token present
    if OAUTH_CREDS and OAUTH_CREDS.expired and OAUTH_CREDS.refresh_token:
        try:
            OAUTH_CREDS.refresh(requests.Request())  # type: ignore
            drive_service = build("drive", "v3", credentials=OAUTH_CREDS)
            sheets_service = build("sheets", "v4", credentials=OAUTH_CREDS)
            return drive_service, sheets_service
        except Exception:
            pass

    # Fallback to service account from env
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise HTTPException(status_code=401, detail="Google not connected. Either sign in with Google or set GOOGLE_SERVICE_ACCOUNT_JSON.")

    import json
    info = json.loads(sa_json)
    creds = SACredentials.from_service_account_info(info, scopes=SCOPES)
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
# Auth routes (Google OAuth)
# =====================

def get_oauth_flow(request: Request) -> Flow:
    """Create an OAuth flow using env GOOGLE_OAUTH_CLIENT_ID/SECRET.
    Redirect URI is derived from the incoming request.
    """
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="OAuth client not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.")

    # Build redirect URI dynamically to match current host
    redirect_uri = str(request.url_for("google_oauth_callback"))

    flow = Flow(
        client_config={
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
                "javascript_origins": [],
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    return flow


@app.get("/auth/google/login")
def google_oauth_login(request: Request):
    try:
        flow = get_oauth_flow(request)
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        # Store state in memory (single-user demo). In production, store per-session.
        app.state.oauth_state = state
        return RedirectResponse(authorization_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/auth/google/callback")
def google_oauth_callback(request: Request):
    global OAUTH_CREDS
    try:
        flow = get_oauth_flow(request)
        state = getattr(app.state, "oauth_state", None)
        # Verify state if provided
        # Continue to fetch token using full URL including query
        flow.fetch_token(authorization_response=str(request.url))
        creds = flow.credentials
        OAUTH_CREDS = creds
        html = """
        <html>
          <head><title>Google Connected</title></head>
          <body style='font-family: system-ui; padding: 24px;'>
            <h2>Google account connected ✅</h2>
            <p>You can return to the app and save records now.</p>
          </body>
        </html>
        """
        return HTMLResponse(content=html)
    except Exception as e:
        return HTMLResponse(status_code=400, content=f"<pre>OAuth error: {e}</pre>")


@app.get("/auth/status")
def auth_status():
    ok = bool(OAUTH_CREDS and OAUTH_CREDS.valid)
    return {"authenticated": ok}


@app.post("/auth/logout")
def auth_logout():
    global OAUTH_CREDS
    OAUTH_CREDS = None
    return {"ok": True}


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
