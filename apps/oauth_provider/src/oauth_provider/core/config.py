import os

APP_NAME = os.environ["OPENHOST_APP_NAME"]
ZONE_DOMAIN = os.environ["OPENHOST_ZONE_DOMAIN"]
MY_REDIRECT_DOMAIN = os.environ["OPENHOST_MY_REDIRECT_DOMAIN"]
APP_TOKEN = os.environ["OPENHOST_APP_TOKEN"]
ROUTER_URL = os.environ["OPENHOST_ROUTER_URL"]


# This routes to a static callback URL because it needs pre-registered in the OAuth provider dashboard
# (e.g. Google, GitHub) and those typically don't support dynamic URLs.
# The my. page forwards /redirect/... paths to the instance's router, which sends the browser on to this app's
# /callback (named in the OAuth state).
OAUTH_REDIRECT_URI = f"https://{MY_REDIRECT_DOMAIN}/redirect/oauth_callback"

OAUTH_SERVICE_URL = "github.com/imbue-openhost/openhost/services/oauth"
