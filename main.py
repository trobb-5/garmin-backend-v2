from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime
import json

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

        # FIXED: dump() requires no arguments in newer versions, or uses dumps() for strings
        # We use dumps() to ensure it returns a string for Firestore storage
        try:
            dump = client.dumps()
        except AttributeError:
            # Fallback for older versions if dumps() doesn't exist
            import io
            f = io.StringIO()
            client.dump(f)
            dump = f.getvalue()

        db.collection("users").document(firebase_uid).set({
            "garmin_dump": dump,
            "last_sync": firestore.SERVER_TIMESTAMP
        }, merge=True)

        return {"status": "success", "message": "Garmin connected successfully"}
    except Exception as e:
        print(f"Login Error: {e}")
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
        
        # Load the session
        try:
            client.loads(garmin_dump)
        except Exception:
            # Fallback for older library versions
            import io
            client.load(io.StringIO(garmin_dump))
        
        today = datetime.now().date().isoformat()
        
        # THE UNIVERSAL FETCH FIX: Use the client's internal session directly
        # This bypasses all 'missing path' or 'attribute' errors in the garth wrapper
        data = {}
        endpoints = {
            "summary": f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}",
            "sleep": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}"
        }

        for key, url in endpoints.items():
            try:
                # client.sess is the raw requests session with all auth cookies already set
                resp = client.sess.get(url, headers={
                    "NK": "NT",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                })
                if resp.status_code == 200:
                    data[key] = resp.json()
                else:
                    print(f"Endpoint {key} failed with status: {resp.status_code}")
                    data[key] = None
            except Exception as e:
                print(f"Error fetching {key}: {e}")
                data[key] = None

        return data

    except Exception as e:
        print(f"Global Fetch Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
