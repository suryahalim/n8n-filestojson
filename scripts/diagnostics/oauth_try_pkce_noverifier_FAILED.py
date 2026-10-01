import json, sys
import oauthlib.oauth2.rfc6749.parameters as _prm
_prm.is_secure_transport = lambda uri, *a, **k: True
from google_auth_oauthlib.flow import Flow

redirect = open('/tmp/oauth_redirect2.txt').read().strip()
tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
client_config = {'installed': {
    'client_id': tok['client_id'],
    'client_secret': tok['client_secret'],
    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token',
    'redirect_uri': 'http://localhost'}}
scopes = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
st = 'vbRb6fE18L9OaD9WcTEDD'
flow = Flow.from_client_config(client_config, scopes=scopes, state=st,
                               redirect_uri='http://localhost')
flow.fetch_token(authorization_response=redirect)
c = flow.credentials
import time, calendar
tok.update({'token': c.token, 'refresh_token': c.refresh_token,
            'expiry': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(calendar.timegm(c.expiry.timetuple()))) if c.expiry else None,
            'scope': ' '.join(c.scopes or [])})
tok.pop('expires_at', None)
json.dump(tok, open('/home/suryahalim/.hermes/google_token.json', 'w'), indent=1)
print('TOKEN OK; scopes:', tok.get('scope'))
