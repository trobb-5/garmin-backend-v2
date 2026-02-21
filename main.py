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

        # Store session in Firestore
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
        
        # Date for Garmin requests
        today = datetime.now().date().isoformat()
        data = {}

        # These endpoints are the most stable for real-time data
        endpoints = {
            "summary": f"/usersummary-service/usersummary/daily/{today}",
            "sleep": f"/wellness-service/wellness/dailySleepData/{today}",
            "hrv": f"/hrv-service/hrv/{today}"
        }

        # THE CRITICAL FIX: Use client.connect_get() with ONLY the path
        # Garth handles the base URL and the authentication headers automatically
        for key, path in endpoints.items():
            try:
                resp = client.connect_get(path)
                if resp.status_code == 200:
                    data[key] = resp.json()
                else:
                    print(f"Garmin error {key}: {resp.status_code}")
                    data[key] = None
            except Exception as e:
                print(f"Failed to parse {key}: {e}")
                data[key] = None

        # Fallback check
        if not data.get("summary"):
            raise Exception("Garmin session valid but returned no data.")

        return data

    except Exception as e:
        print(f"Global Fetch Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
