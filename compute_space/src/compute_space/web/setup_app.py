"""Setup-only Litestar app: served once at first boot to provision the owner.

When the setup handler successfully creates the owner row, it triggers shutdown via
``trigger_restart()``; ``start.py`` then proceeds to boot the full app.
"""

import os
import secrets
from base64 import b64encode
from contextlib import closing
from pathlib import Path
from typing import Any
from typing import NoReturn

import bcrypt
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.background_tasks import BackgroundTask
from litestar.di import NamedDependency
from litestar.di import Provide
from litestar.exceptions import PermissionDeniedException
from litestar.openapi import ResponseSpec
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.response import Template
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig

from compute_space.config import Config
from compute_space.config import provide_config
from compute_space.core.auth.auth import DEFAULT_OWNER_USERNAME
from compute_space.core.auth.auth import create_session
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.domains import primary_domain
from compute_space.core.logging import logger
from compute_space.core.settings_store import CLAIM_TOKEN_KEY
from compute_space.core.settings_store import delete_setting
from compute_space.core.settings_store import get_setting
from compute_space.core.updates import is_shutdown_pending
from compute_space.core.updates import trigger_restart
from compute_space.db import get_db
from compute_space.web.auth.cookies import build_session_cookie
from compute_space.web.helpers.static import STATIC_DIR
from compute_space.web.helpers.static import make_static_url

# Set when setup_post succeeds. /health flips to 503 immediately so clients
# polling for the post-restart main app don't see a stale 200 from the setup
# app during the brief window before hypercorn actually drops the listener.
_setup_completed: bool = False
_static_url = make_static_url(STATIC_DIR)


def _inline_setup_styles() -> str:
    """Bundle the shared styling and decorations for the listener handoff."""
    styles = "\n".join(
        (STATIC_DIR / "css" / name).read_text(encoding="utf-8")
        for name in ("tokens.css", "base.css", "components.css")
    )
    for name in ("cloud.svg", "grass.svg"):
        image = b64encode((STATIC_DIR / "img" / "deco" / name).read_bytes()).decode("ascii")
        styles = styles.replace(f"../img/deco/{name}", f"data:image/svg+xml;base64,{image}")
    return styles


def _verify_claim_token(claim_token: str) -> bool:
    """Compare ``claim_token`` against the token seeded into the DB ``settings`` store (from
    ``first_boot.toml`` or the legacy claim-token file at startup)."""
    if not claim_token:
        return False
    with closing(get_db()) as db:
        stored_token = get_setting(db, CLAIM_TOKEN_KEY)
    if not stored_token:
        return False
    return secrets.compare_digest(claim_token, stored_token)


def _claim_token_required(config: Config) -> bool:
    # Driven by config (fail-safe: defaults to True). When required but no
    # token file is present, _verify_claim_token returns False and the route
    # 403s — meaning a misconfigured deploy fails closed instead of open.
    return config.claim_token_required


def _claim_unauthorized() -> NoReturn:
    raise PermissionDeniedException(detail="Invalid or missing claim token.")


@get("/")
async def root_redirect() -> Response[None]:
    """Redirect to /setup before the owner is provisioned."""
    from litestar.response import Redirect  # noqa: PLC0415

    return Redirect(path="/setup")


@get("/setup", raises=[PermissionDeniedException])
async def setup_get(request: Request[Any, Any, Any], config: NamedDependency[Config]) -> Template:
    claim_token = request.query_params.get("claim", "")
    if _claim_token_required(config) and not _verify_claim_token(claim_token):
        _claim_unauthorized()
    return Template(template_name="setup.html", context={"claim": claim_token})


@post("/setup", status_code=200, raises=[PermissionDeniedException])
async def setup_post(request: Request[Any, Any, Any], config: NamedDependency[Config]) -> Response[Any]:
    form = await request.form()
    form_claim = form.get("claim", "")
    if _claim_token_required(config) and not _verify_claim_token(form_claim):
        _claim_unauthorized()

    password = form.get("password", "")
    confirm = form.get("confirm_password", "")
    username_raw = form.get("username", "").strip()  # blank falls back to DEFAULT_OWNER_USERNAME

    def _error(msg: str) -> Template:
        return Template(
            template_name="setup.html",
            context={"error": msg, "claim": form_claim, "username": username_raw},
        )

    if not password:
        return _error("Password is required")
    if password != confirm:
        return _error("Passwords do not match")
    if username_raw:
        username_error = validate_owner_username(username_raw)
        if username_error is not None:
            return _error(username_error)
        username = username_raw
    else:
        username = DEFAULT_OWNER_USERNAME

    db = get_db()
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    cursor = db.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (username, password_hash),
    )
    user_id = cursor.lastrowid
    assert user_id is not None
    session_token = create_session(user_id, db)
    db.commit()

    # The claim token has done its job — drop it from the settings store, and best-effort remove
    # the legacy file too (it's only a seed source; may linger on upgraded instances).
    delete_setting(db, CLAIM_TOKEN_KEY)
    try:
        os.remove(config.claim_token_path)
    except OSError:
        pass

    try:
        deploy_default_apps(config, db)
    except Exception as exc:
        logger.error("default_apps deploy raised unexpectedly: {}", exc)

    # Keep the browser here while the full app initializes. The page carries
    # its styling and polling script so neither races the closing listener.
    response = Template(template_name="setup_complete.html")
    # Setup is always served on the primary domain; scope the cookie to it (no middleware here to
    # stash a request domain).
    response.set_cookie(build_session_cookie(session_token, primary_domain(db)))

    global _setup_completed  # noqa: PLW0603
    _setup_completed = True

    # Schedule the restart for after the response has been written so the
    # client actually receives the 200 + Set-Cookie before the listener drops.
    response.background = BackgroundTask(_trigger_restart_after_response)
    return response


async def _trigger_restart_after_response() -> None:
    """Let the response finish before closing the setup listener."""
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.05)
    trigger_restart()


@get(
    "/health",
    sync_to_thread=False,
    responses={503: ResponseSpec(data_container=dict[str, str], description="The setup service is restarting.")},
)
def health() -> Response[dict[str, str]]:
    """Liveness probe.  Returns ``{"status": "ok"}`` (or 503 when restarting)."""
    if is_shutdown_pending() or _setup_completed:
        return Response(content={"status": "restarting"}, status_code=503)
    return Response(content={"status": "ok"})


def create_setup_app(config: Config) -> Litestar:
    """Build the minimal Litestar app served until the owner is provisioned."""
    del config  # unused; the config singleton is set in start.py before this is called
    web_dir = Path(__file__).parent
    template_dir = web_dir / "templates"
    static_dir = STATIC_DIR
    setup_styles = _inline_setup_styles()

    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=template_dir,
        engine=JinjaTemplateEngine,
    )
    static_router = create_static_files_router(path="/static", directories=[static_dir])

    def _install_template_globals(app: Litestar) -> None:
        engine = app.template_engine
        # The engine is the one configured just above; if that ever stops being
        # Jinja the templates won't render at all, so assert rather than
        # silently skipping the globals and 500ing on the first render.
        assert isinstance(engine, JinjaTemplateEngine), f"expected a Jinja engine, got {type(engine)}"
        engine.engine.globals["static_url"] = _static_url
        engine.engine.globals["setup_styles"] = setup_styles

    return Litestar(
        route_handlers=[root_redirect, setup_get, setup_post, health, static_router],
        template_config=template_config,
        dependencies={"config": Provide(provide_config, sync_to_thread=False)},
        on_startup=[_install_template_globals],
    )
