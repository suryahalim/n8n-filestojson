import json, secrets, base64
from google_auth_oauthlib.flow import Flow

tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
client_config = {'installed': {
    'client_id': tok['client_id'],
    'client_secret': tok['client_secret'],
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'redirect_uri': 'http://localhost'}}
scopes = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
flow = Flow.from_client_config(client_config, scopes=scopes,
                               redirect_uri='http://localhost')
# deterministic PKCE verifier we control (saved to disk for the finish step)
verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
flow.code_verifier = verifier
url, state = flow.authorization_url(access_type='offline', prompt='consent',
                                    include_granted_scopes='true')
json.dump({'state': state, 'redirect_uri': 'http://localhost', 'verifier': verifier},
          open('/home/suryahalim/google_oauth_pending_manual.json', 'w'))
print(url)
