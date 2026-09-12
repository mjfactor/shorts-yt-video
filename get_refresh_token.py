"""One-time helper: exchange Google OAuth client for a YouTube refresh token.
1. Create OAuth Desktop Client in Google Cloud Console, enable YouTube Data API v3.
2. set YT_CLIENT_ID / YT_CLIENT_SECRET env vars (or edit below), then: python get_refresh_token.py
3. Browser opens -> log in with your channel account -> paste code back.
4. Copy the printed REFRESH_TOKEN into GitHub repo Secrets as YT_REFRESH_TOKEN.
"""
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

CLIENT_ID = os.getenv("YT_CLIENT_ID", "PASTE_CLIENT_ID_HERE")
CLIENT_SECRET = os.getenv("YT_CLIENT_SECRET", "PASTE_CLIENT_SECRET_HERE")

flow = InstalledAppFlow.from_client_config(
    {"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token"}},
    scopes=SCOPES,
)
creds = flow.run_local_server(port=8080, prompt="consent", access_type="offline")
print("\n=== COPY THIS INTO GITHUB SECRET YT_REFRESH_TOKEN ===")
print(creds.refresh_token)
