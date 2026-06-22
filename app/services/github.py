import base64
from typing import Any

import httpx


class GitHubService:
    """Async GitHub API client for a single authenticated user token."""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def _get(self, path: str, **kwargs: Any) -> Any:
        response = await self._client.get(path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, **kwargs: Any) -> Any:
        response = await self._client.post(path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _delete(self, path: str, **kwargs: Any) -> None:
        response = await self._client.delete(path, **kwargs)
        response.raise_for_status()

    async def get_authenticated_user(self) -> dict:
        """Return the authenticated user's profile."""
        return await self._get("/user")

    async def get_user_orgs(self) -> list[dict]:
        """Return all organizations the authenticated user belongs to."""
        return await self._get("/user/orgs", params={"per_page": 100})

    async def get_org_repos(self, org: str, per_page: int = 100) -> list[dict]:
        """Return all repositories for an organization, handling pagination."""
        repos: list[dict] = []
        page = 1
        while True:
            batch = await self._get(
                f"/orgs/{org}/repos",
                params={"per_page": per_page, "page": page, "type": "all"},
            )
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return repos

    async def get_repo_workflows(self, owner: str, repo: str) -> list[dict]:
        """Return all workflows defined in a repository."""
        data = await self._get(f"/repos/{owner}/{repo}/actions/workflows")
        return data.get("workflows", [])

    async def get_workflow_runs(
        self,
        owner: str,
        repo: str,
        workflow_id: int,
        per_page: int = 30,
    ) -> list[dict]:
        """Return recent runs for a specific workflow."""
        data = await self._get(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
            params={"per_page": per_page},
        )
        return data.get("workflow_runs", [])

    async def get_run_logs_url(self, owner: str, repo: str, run_id: int) -> str:
        """Return the redirect URL for downloading a run's log archive."""
        response = await self._client.get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            follow_redirects=False,
        )
        if response.status_code in (301, 302, 307, 308):
            return response.headers["location"]
        response.raise_for_status()
        return str(response.url)

    async def get_run_logs_text(
        self, owner: str, repo: str, run_id: int, max_lines: int = 1000
    ) -> str:
        """Download the run's log archive (zip), extract all .txt logs, and
        return the concatenated text (last max_lines lines)."""
        import io
        import zipfile

        response = await self._client.get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            follow_redirects=True,
        )
        response.raise_for_status()

        lines: list[str] = []
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith(".txt"):
                        content = zf.read(name).decode("utf-8", errors="replace")
                        lines.append(f"===== {name} =====")
                        lines.extend(content.splitlines())
        except zipfile.BadZipFile:
            lines = response.text.splitlines()

        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)

    async def get_workflow_file(
        self, owner: str, repo: str, path: str, ref: str
    ) -> str:
        """Return the raw YAML content of a workflow file."""
        response = await self._client.get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        return response.text

    async def get_file_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        """Return the blob SHA of a file at the given ref, or None if not found."""
        try:
            data = await self._get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
            if isinstance(data, dict):
                return data.get("sha")
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def create_fix_branch(self, owner: str, repo: str, base_sha: str, branch_name: str) -> None:
        """Create a new branch from base_sha.

        Idempotent: if the branch already exists (e.g. left over from a prior
        Raise PR attempt that failed downstream), reset it to base_sha instead
        of erroring — a retry must not require the user to delete state first.
        """
        try:
            await self._post(
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422 and "already exists" in exc.response.text:
                response = await self._client.patch(
                    f"/repos/{owner}/{repo}/git/refs/heads/{branch_name}",
                    json={"sha": base_sha, "force": True},
                )
                response.raise_for_status()
                return
            raise

    async def commit_fix(
        self,
        owner: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
        current_sha: str | None,
    ) -> None:
        """Create or update a workflow file with the suggested YAML fix."""
        encoded = base64.b64encode(content.encode()).decode()
        payload: dict = {"message": message, "content": encoded, "branch": branch}
        if current_sha:
            payload["sha"] = current_sha
        response = await self._client.put(f"/repos/{owner}/{repo}/contents/{path}", json=payload)
        response.raise_for_status()

    async def create_pr(
        self, owner: str, repo: str, head: str, base: str, title: str, body: str
    ) -> dict:
        """Open a pull request and return the PR object."""
        return await self._post(
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "maintainer_can_modify": True,
            },
        )

    async def create_webhook(self, org: str, secret: str, url: str) -> dict:
        """Create an organization webhook for workflow_run events."""
        return await self._post(
            f"/orgs/{org}/hooks",
            json={
                "name": "web",
                "active": True,
                "events": ["workflow_run"],
                "config": {
                    "url": url,
                    "content_type": "json",
                    "secret": secret,
                    "insecure_ssl": "0",
                },
            },
        )

    async def delete_webhook(self, org: str, hook_id: int) -> None:
        """Remove an organization webhook."""
        await self._delete(f"/orgs/{org}/hooks/{hook_id}")

    async def aclose(self) -> None:
        await self._client.aclose()
