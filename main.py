from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime

load_dotenv()

app = FastAPI(title="Garmin Backend for Nutrient Sync")

# Initialize Firebase & Firestore
cred = credentials.Certificate("firebase-adminsdk.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

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

        dump = client.dumps()
        db.collection("users").document(firebase_uid).set({
            "garmin_dump": dump,
            "last_sync": firestore.SERVER_TIMESTAMP
        }, merge=True)

        return {"status": "success", "message": "Garmin connected successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/garmin/today")
async def garmin_today(authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        doc = db.collection("users").document(firebase_uid).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="No Garmin session found.")
        
        garmin_dump = doc.to_dict().get("garmin_dump")
        client = garth.Client()
        client.loads(garmin_dump)
        
        # Ensure the session is actually alive
        if client.expired:
            print("Session expired, attempting internal refresh...")
            client.refresh()

        today = datetime.now().date().isoformat()
        
        # This specific URL is the most reliable for current daily totals
        url = f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}"
        
        # THE FIX: Use client.connect_get which handles all internal Garmin headers automatically
        # This bypasses the 403 Forbidden error seen in your logs
        resp = client.connect_get(url)
        
        data = {
            "summary": resp.json() if resp.status_code == 200 else None,
            "sleep": None,
            "hrv": None
        }

        # Attempt sleep separately
        try:
            sleep_url = f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}"
            sleep_resp = client.connect_get(sleep_url)
            if sleep_resp.status_code == 200:
                data["sleep"] = sleep_resp.json()
        except:
            pass

        return data

    except Exception as e:
        print(f"Global Fetch Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
