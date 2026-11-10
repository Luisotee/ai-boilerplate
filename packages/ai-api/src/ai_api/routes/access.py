"""The effective whitelist, for the chat clients' primary gate.

The TS clients used to gate on their own startup copy of `WHITELIST_PHONES`, so
an entry added through `PATCH /admin/settings` stayed blocked by every client
until its env was edited and it restarted. They now read this value (cached
about 10 s, failing closed when it can't be fetched) and parse it with their
byte-identical `utils/whitelist.ts`. See `docs/access-control.md`.
"""

from fastapi import APIRouter
from starlette.requests import Request

from ..deps import limiter
from ..runtime_config import runtime_config
from ..schemas import WhitelistResponse

router = APIRouter()


@router.get("/chat/whitelist", response_model=WhitelistResponse, tags=["Chat"])
@limiter.exempt  # polled by every client every ~10 s; still behind X-API-Key
async def get_whitelist(request: Request):
    """The raw effective `whitelist_phones` value: the `/admin` override, else the env.

    Empty means the whitelist is disabled. Returned unparsed, so the clients'
    matcher (`utils/whitelist.ts`) stays the one that parses it.
    """
    return WhitelistResponse(whitelist_phones=runtime_config.get("whitelist_phones") or "")
