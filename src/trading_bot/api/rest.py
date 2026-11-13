"""Common authenticated JSON calls; never retries a mutation."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, repr=False)
class MutationAuthorization:
    """Explicit intent bound to one client, session, account and impact category."""

    client: Any = field(repr=False)
    tokens: tuple[str, str] = field(repr=False)
    account_id: str = field(repr=False)
    impact: str
    generation: int = 0
    resource_id: str | None = None


class RestComponent:
    def _api_call(
        self, method: str, path: str, *, params=None, payload=None, pair=False
    ):
        headers = {"CST": self.cst, "X-SECURITY-TOKEN": self.x_security_token}
        kwargs = {"headers": headers, "allow_redirects": False, "timeout": 20}
        if params:
            kwargs["params"] = {
                k: str(v).lower() if isinstance(v, bool) else v
                for k, v in params.items()
                if v is not None
            }
        if payload is not None:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = payload
        response = self._request(method, self.BASE_URL + "/api/v1" + path, **kwargs)
        if response.status_code != 200:
            self._stop(f"http_{response.status_code}", mutation=method != "get")
        value = {} if not response.content else response.json()
        if not isinstance(value, dict):
            self._stop("invalid_api_response", mutation=method != "get")
        if method != "get" and (
            path.startswith("/positions/") or path.startswith("/workingorders")
        ):
            if (
                not isinstance(value.get("dealReference"), str)
                or not value["dealReference"]
            ):
                self._stop("invalid_api_response", mutation=True)
        return (value, response.status_code) if pair else value

    def authorize_mutation(
        self, impact: str, *, resource_id: str | None = None
    ) -> MutationAuthorization:
        """Explicit DEMO authorization. New LIVE impacts lack an approved risk policy.

        Existing LIVE position open/close remain available exclusively through LiveExecution.
        Authorization never changes an account or preferences by itself.
        """
        self._require_available()
        if impact not in {
            "account-switch",
            "preferences",
            "demo-balance",
            "watchlist",
            "position-update",
            "working-order",
        }:
            raise ValueError("unknown_mutation_impact")
        if self.environment != "demo":
            raise PermissionError("live_mutation_requires_dedicated_risk_policy")
        if resource_id is not None:
            from trading_bot.api.validation import text

            text(resource_id)
        account = self.verify_account_environment()
        return MutationAuthorization(
            self,
            (self.cst, self.x_security_token),
            account["accountId"],
            impact,
            self._session_generation,
            resource_id,
        )

    def _authorize(
        self,
        authorization: MutationAuthorization | None,
        impact: str,
        *,
        resource_id: str | None = None,
    ):
        self._require_available()
        if self.environment != "demo":
            raise PermissionError("live_mutation_requires_dedicated_risk_policy")
        from trading_bot.config.trading_config import DEMO_URL

        if self.BASE_URL != DEMO_URL:
            raise PermissionError("demo_endpoint_required")
        if (
            not isinstance(authorization, MutationAuthorization)
            or authorization.client is not self
            or authorization.impact != impact
            or authorization.tokens != (self.cst, self.x_security_token)
            or authorization.generation != self._session_generation
            or authorization.resource_id != resource_id
            or not all(authorization.tokens)
        ):
            raise PermissionError(
                "explicit_session_bound_mutation_authorization_required"
            )
        account = self.verify_account_environment()
        if account["accountId"] != authorization.account_id:
            raise PermissionError("authorized_account_changed")
        return account

    @contextmanager
    def _live_send_scope(self):
        """Used only inside the existing execution gate's validated callback."""
        self._live_send_depth += 1
        try:
            yield
        finally:
            self._live_send_depth -= 1

    def _gated_send(self, send):
        with self._live_send_scope():
            return send()
