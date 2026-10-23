from jinja2 import Environment
from jinja2 import FileSystemLoader

from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import WEB_DIR
from compute_space.web.helpers.static import make_static_url

# For pages rendered outside Litestar's template engine, e.g. from SubdomainProxyMiddleware.
STANDALONE_JINJA_ENV = Environment(loader=FileSystemLoader(str(WEB_DIR / "templates")), autoescape=True)
STANDALONE_JINJA_ENV.globals["static_url"] = make_static_url(STATIC_DIR)
