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
            client.login(request.username, request.password, prompt_mfa=lambda: request.mfa_code)
        else:
            client.login(request.username, request.password)

        # Use dumps() to store session as string
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
        
        # FIX: Spoof headers to look like a real browser
        client.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Referer": "https://connect.garmin.com/modern/dashboards/daily-summary",
            "Accept": "application/json, text/plain, */*"
        })
        
        # Use ISO format (YYYY-MM-DD)
        today = datetime.now().date().isoformat()
        data = {}

        # Fetch metrics individually with explicit status logging
        endpoints = {
            "summary": f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}",
            "sleep": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}",
            "hrv": f"https://connect.garmin.com/modern/proxy/hrv-service/hrv/{today}",
            "hr": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailyHeartRate/{today}"
        }

        for key, url in endpoints.items():
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    data[key] = resp.json()
                else:
                    print(f"Garmin API Info: {key} returned status {resp.status_code}")
                    data[key] = None
            except Exception as e:
                print(f"Error fetching {key}: {e}")
                data[key] = None

        # Success check: at least summary or sleep should be present
        if data.get("summary") is None and data.get("sleep") is None:
             raise Exception("Garmin returned 200 but no usable data found.")

        return data

    except Exception as e:
        print(f"Fetch Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
