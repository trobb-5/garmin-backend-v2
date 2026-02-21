from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime
import io

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

        # Version-proof session dumping
        try:
            dump = client.dumps()
        except:
            f = io.StringIO()
            client.dump(f)
            dump = f.getvalue()

        db.collection("users").document(firebase_uid).set({
            "garmin_dump": dump,
            "last_sync": firestore.SERVER_TIMESTAMP
        }, merge=True)

        return {"status": "success", "message": "Garmin connected successfully"}
    except Exception as e:
        print(f"Login Failure: {e}")
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
        
        # Version-proof session loading
        try:
            client.loads(garmin_dump)
        except:
            client.load(io.StringIO(garmin_dump))
        
        today = datetime.now().date().isoformat()
        
        # USE RAW SESSION: This is the most stable way to bypass 403s and attribute errors
        # It uses the authenticated session directly without the buggy Garth wrappers
        sess = client.sess
        data = {}
        
        # These URLs are the "source of truth" for the Garmin dashboard
        endpoints = {
            "summary": f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}",
            "sleep": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}"
        }

        # Adding essential browser headers to the session
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Referer": "https://connect.garmin.com/modern/dashboards/daily-summary",
            "NK": "NT"
        })

        for key, url in endpoints.items():
            try:
                resp = sess.get(url)
                if resp.status_code == 200:
                    data[key] = resp.json()
                else:
                    print(f"Error {key}: {resp.status_code}")
                    data[key] = None
            except Exception as e:
                print(f"Fetch failed for {key}: {e}")
                data[key] = None

        return data

    except Exception as e:
        print(f"Critical Backend Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
