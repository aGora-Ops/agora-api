import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AUTH_COOKIE_NAME, get_current_user
from app.core.config import settings
from app.core.limiter import limiter
from app.core.security import create_access_token, encrypt_token
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import UserMe

router = APIRouter()

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

@router.get("/github")
@limiter.limit("10/minute")
async def github_login(request: Request) -> RedirectResponse:
    """Redirect the browser to GitHub's OAuth authorization page."""
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": settings.GITHUB_REDIRECT_URI,
        # workflow scope is required to create/update files under .github/workflows/ —
        # without it GitHub 404s the Contents API write (masked, like private-repo access)
        # even though repo grants write everywhere else.
        "scope": "repo,workflow,admin:org_hook,read:org",
        "state": state,
    }
    url = f"{GITHUB_AUTH_URL}?{urlencode(params)}"
    response = RedirectResponse(url=url)
    response.set_cookie(
        "oauth_state",
        state,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=600,
    )
    return response

@router.get("/callback")
@limiter.limit("10/minute")
async def github_callback(
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    oauth_state: str | None = Cookie(default=None),
) -> RedirectResponse:
    """Exchange OAuth code for an access token, upsert user, and set JWT cookie."""
    if not oauth_state or oauth_state != state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.GITHUB_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        token_data = token_response.json()

    github_access_token = token_data.get("access_token")
    if not github_access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to obtain GitHub access token",
        )

    async with httpx.AsyncClient() as client:
        user_response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {github_access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        user_response.raise_for_status()
        gh_user = user_response.json()

    result = await db.execute(select(User).where(User.github_id == gh_user["id"]))
    user = result.scalar_one_or_none()

    encrypted_token = encrypt_token(github_access_token)

    if user is None:
        user = User(
            github_id=gh_user["id"],
            login=gh_user["login"],
            name=gh_user.get("name"),
            avatar_url=gh_user.get("avatar_url", ""),
            email=gh_user.get("email"),
            access_token_encrypted=encrypted_token,
        )
        db.add(user)
        await db.flush()
    else:
        user.login = gh_user["login"]
        user.name = gh_user.get("name")
        user.avatar_url = gh_user.get("avatar_url", "")
        user.email = gh_user.get("email")
        user.access_token_encrypted = encrypted_token

    await db.commit()
    await db.refresh(user)

    jwt_token = create_access_token({"sub": str(user.id), "login": user.login})

    redirect = RedirectResponse(
        url=f"{settings.FRONTEND_URL}/dashboard",
        status_code=status.HTTP_302_FOUND,
    )
    redirect.set_cookie(
        AUTH_COOKIE_NAME,
        jwt_token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * settings.ACCESS_TOKEN_EXPIRE_DAYS,
        secure=settings.cookie_secure,
    )
    redirect.delete_cookie("oauth_state")
    return redirect

@router.get("/me", response_model=UserMe)
async def get_me(user: User = Depends(get_current_user)) -> UserMe:
    """Return the currently authenticated user's profile."""
    return UserMe.model_validate(user)

@router.post("/logout")
async def logout(response: Response) -> dict:
    """Clear the JWT cookie to log the user out."""
    response.delete_cookie(AUTH_COOKIE_NAME, httponly=True, samesite="lax")
    return {"message": "Logged out successfully"}
