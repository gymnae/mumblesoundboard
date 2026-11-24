"""Who may use /admin: identity established by caddy-security in front of the app.

The OIDC login itself happens entirely in Caddy (caddy-security / AuthCrunch).
Its authorization policy on /admin* injects the user's identity as request
headers; the app only checks what arrives:

* ADMIN_AUTH=proxy enables the admin area (default: off, everything under
  /admin answers 404). The identity header (X-Token-User-Email by default)
  must be present. Safe only when the app is reachable through Caddy alone,
  as with a noauth File Browser behind the same proxy.
* ADMIN_JWT_KEY (shared HMAC secret, as in `crypto key sign-verify`) or
  ADMIN_JWT_PUBLIC_KEY_FILE (RSA/ECDSA/Ed25519 PEM) additionally makes the app
  verify caddy-security's access token itself (cookie or Bearer header), so a
  forged header alone is useless even if port 5000 is reachable directly.
* ADMIN_ROLES optionally names roles of which the user needs at least one.

State-changing requests must also carry X-Soundboard-Admin: 1. Browsers do
not let other sites add custom headers without a CORS preflight (which this
app never grants), so a cross-site form cannot trigger uploads or deletes with
the user's login cookie.
"""
import os

CSRF_HEADER = 'X-Soundboard-Admin'
DEFAULT_TOKEN_COOKIES = ('AUTHP_ACCESS_TOKEN', 'access_token', 'jwt_access_token')
HMAC_ALGORITHMS = ['HS256', 'HS384', 'HS512']
PUBLIC_KEY_ALGORITHMS = ['RS256', 'RS384', 'RS512', 'ES256', 'ES384', 'ES512', 'EdDSA']
ROLE_CLAIMS = ('roles', 'role', 'groups', 'group')
SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


class AuthResult:
    def __init__(self, user=None, roles=(), status=200, error=None):
        self.user = user
        self.roles = list(roles)
        self.status = status
        self.error = error

    @property
    def ok(self):
        return self.status == 200


def _split(value, sep=None):
    return [part.strip() for part in (value or '').split(sep) if part.strip()]


class AdminAuth:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        mode = (env.get('ADMIN_AUTH') or 'off').strip().lower()
        if mode not in ('off', 'proxy'):
            print(f"[ADMIN] Unknown ADMIN_AUTH={mode!r}; admin area stays disabled (use 'proxy').")
            mode = 'off'
        self.enabled = mode == 'proxy'
        self.user_header = env.get('ADMIN_USER_HEADER') or 'X-Token-User-Email'
        self.roles_header = env.get('ADMIN_ROLES_HEADER') or 'X-Token-User-Roles'
        self.required_roles = set(_split(env.get('ADMIN_ROLES'), ','))
        self.token_cookies = tuple(_split(env.get('ADMIN_JWT_COOKIE'), ',')) or DEFAULT_TOKEN_COOKIES
        self.audience = env.get('ADMIN_JWT_AUDIENCE') or None
        self.issuer = env.get('ADMIN_JWT_ISSUER') or None

        self.jwt_key = None
        self.jwt_algorithms = []
        secret = env.get('ADMIN_JWT_KEY')
        key_file = env.get('ADMIN_JWT_PUBLIC_KEY_FILE')
        if secret:
            self.jwt_key, self.jwt_algorithms = secret, HMAC_ALGORITHMS
        elif key_file:
            with open(key_file, 'rb') as f:
                self.jwt_key, self.jwt_algorithms = f.read(), PUBLIC_KEY_ALGORITHMS
        if self.enabled:
            how = 'verifying JWT and ' if self.jwt_key else ''
            print(f"[ADMIN] Admin area enabled ({how}trusting {self.user_header} from the proxy"
                  + (f"; roles: {', '.join(sorted(self.required_roles))})" if self.required_roles else ')'))

    @property
    def verifies_jwt(self):
        return self.jwt_key is not None

    def _token(self, request):
        header = request.headers.get('Authorization', '')
        if header[:7].lower() == 'bearer ':
            return header[7:].strip()
        for name in self.token_cookies:
            value = request.cookies.get(name)
            if value:
                return value
        return None

    def _verify_jwt(self, token):
        import jwt  # only needed when JWT verification is configured
        options = {'require': ['exp'], 'verify_aud': self.audience is not None}
        try:
            return jwt.decode(token, self.jwt_key, algorithms=self.jwt_algorithms,
                              audience=self.audience, issuer=self.issuer, options=options)
        except jwt.PyJWTError as e:
            raise ValueError(str(e))

    def check(self, request):
        """AuthResult for this request (status 200 when allowed)."""
        if not self.enabled:
            return AuthResult(status=404, error='admin area disabled (set ADMIN_AUTH=proxy)')

        user = (request.headers.get(self.user_header) or '').strip()
        roles = _split(request.headers.get(self.roles_header))
        if not user:
            return AuthResult(status=401, error=f'not signed in (no {self.user_header} header from the proxy)')

        if self.verifies_jwt:
            token = self._token(request)
            if not token:
                return AuthResult(status=401, error='no access token (is "enable strip token" set in Caddy?)')
            try:
                claims = self._verify_jwt(token)
            except ValueError as e:
                return AuthResult(status=401, error=f'invalid access token: {e}')
            token_user = str(claims.get('email') or claims.get('sub') or '')
            if token_user.lower() != user.lower() and str(claims.get('sub', '')).lower() != user.lower():
                return AuthResult(status=401, error='identity header does not match the access token')
            roles = []
            for claim in ROLE_CLAIMS:
                value = claims.get(claim)
                if isinstance(value, str):
                    roles += value.split()
                elif isinstance(value, list):
                    roles += [str(v) for v in value]

        if self.required_roles and not self.required_roles.intersection(roles):
            return AuthResult(user, roles, status=403, error='missing a required role')

        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != '1':
            return AuthResult(user, roles, status=403, error=f'missing {CSRF_HEADER} header')

        return AuthResult(user, roles)
