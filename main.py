from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import sqlite3
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
from datetime import datetime

load_dotenv()

app = FastAPI(title="Garmin Backend for Nutrient Sync")

# Initialize Firebase
cred = credentials.Certificate("firebase-adminsdk.json")
firebase_admin.initialize_app(cred)

# SQLite database for session persistence
conn = sqlite3.connect("sessions.db", check_same_thread=False)
conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        firebase_uid TEXT PRIMARY KEY,
        garmin_dump TEXT,
        last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

class GarminLoginRequest(BaseModel):
    username: str
    password: str
    mfa_code: str | None = None

@app.post("/garmin/login")
async def garmin_login(request: GarminLoginRequest, authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        client = garth.Client()

        if request.mfa_code:
            # The prompt_mfa lambda allows garth to use the code provided by Flutter
            client.login(request.username, request.password, prompt_mfa=lambda: request.mfa_code)
        else:
            client.login(request.username, request.password)

        # FIXED: Use dumps() (string) for database storage compatibility
        dump = client.dumps()

        conn.execute(
            "INSERT OR REPLACE INTO users (firebase_uid, garmin_dump) VALUES (?, ?)",
            (firebase_uid, dump)
        )
        conn.commit()

        return {"status": "success", "message": "Garmin connected successfully"}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/garmin/today")
async def garmin_today(authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        row = conn.execute(
            "SELECT garmin_dump FROM users WHERE firebase_uid = ?", 
            (firebase_uid,)
        ).fetchone()

        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="No Garmin session found.")

        # Reconstruct client and load session
        client = garth.Client()
        client.loads(row[0])
        
        # Use ISO format (YYYY-MM-DD) which is strictly required by Garmin proxy
        today = datetime.now().date().isoformat()

        # Fetch metrics individually so one failure doesn't break the whole request
        data = {}

        try:
            data["summary"] = client.get(f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}").json()
        except Exception:
            data["summary"] = None

        try:
            data["sleep"] = client.get(f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}").json()
        except Exception:
            data["sleep"] = None

        try:
            data["hrv"] = client.get(f"https://connect.garmin.com/modern/proxy/hrv-service/hrv/{today}").json()
        except Exception:
            data["hrv"] = None

        try:
            data["hr"] = client.get(f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailyHeartRate/{today}").json()
        except Exception:
            data["hr"] = None

        # Logic check: if we have at least the summary, we consider it a success
        if data["summary"] is None and data["sleep"] is None:
             raise Exception("Garmin returned no data for today yet.")

        return data

    except Exception as e:
        print(f"Fetch Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
