import json, sys
from google_auth_oauthlib.flow import Flow

# usage: oauth_finish.py '<full redirect URL pasted from browser>'
redirect = sys.argv[1]
pend = json.load(open('/home/suryahalim/google_oauth_pending_manual.json'))
tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
client_config = {'installed': {
    'client_id': tok['client_id'],
    'client_secret': tok['client_secret'],
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'redirect_uri': pend['redirect_uri']}}
scopes = tok.get('scopes') or [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/userinfo.email']
import oauthlib.oauth2.rfc6749.parameters as _prm
_prm.is_secure_transport = lambda uri, *a, **k: True
flow = Flow.from_client_config(client_config, scopes=scopes,
                               state=pend['state'],
                               redirect_uri=pend['redirect_uri'])
if pend.get('verifier'):
    flow.code_verifier = pend['verifier']
flow.fetch_token(authorization_response=redirect)
tok.update(flow.credentials.to_json_dict())
json.dump(tok, open('/home/suryahalim/.hermes/google_token.json', 'w'), indent=1)
print('token refreshed OK; expires:', tok.get('expires_at'))
