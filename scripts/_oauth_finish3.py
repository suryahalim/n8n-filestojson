import json
import oauthlib.oauth2.rfc6749.parameters as _prm
_prm.is_secure_transport = lambda uri, *a, **k: True
from google_auth_oauthlib.flow import Flow

redirect = open('/tmp/oauth_redirect3.txt').read().strip()
pend = json.load(open('/home/suryahalim/google_oauth_pending_manual.json'))
tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
client_config = {'installed': {
    'client_id': tok['client_id'],
    'client_secret': tok['client_secret'],
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'redirect_uri': pend['redirect_uri']}}
flow = Flow.from_client_config(client_config,
                               scopes=tok.get('scopes') or ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'],
                               state=pend['state'], redirect_uri=pend['redirect_uri'])
flow.code_verifier = pend['verifier']
flow.fetch_token(authorization_response=redirect)
c = flow.credentials
exp = c.expiry.strftime('%Y-%m-%dT%H:%M:%SZ') if c.expiry else None
tok.update({'token': c.token, 'refresh_token': c.refresh_token, 'expiry': exp,
            'scope': ' '.join(c.scopes or [])})
tok.pop('expires_at', None)
json.dump(tok, open('/home/suryahalim/.hermes/google_token.json', 'w'), indent=1)
print('TOKEN OK; scopes:', tok.get('scope'))
