"""Changing one's own password over the API: ``POST /api/v1/auth/password`` (issue #241).

Any logged-in user may change their own password, with the CSRF token; an administrator who resets *someone else's*
password uses ``/api/v1/users/{username}/password`` instead.

* **The current password is verified** and every wrong one counts in the same ``LoginThrottle`` as a failed login (the
  address and the user name), so the route cannot be used to guess the password of a session someone left open; while
  the wait lasts every attempt gets ``429`` whatever it carries. The new password follows the policy of the accounts
  module (the sentences are ``errors.POLICY_MESSAGES``); the policy is checked before the current password, so a
  request that breaks it says nothing about whether the current one was right, and it does not count as a failed guess.
* **Other sessions end, this one goes on.** The store changes the password under its lock (the verification and the
  change are one step), then ``SessionStore.renew`` ends every session of the user and gives the caller a new one: a
  new cookie value and a new CSRF token (in the answer), the same start, so the change does not lengthen the login.
* **Audit.** ``user.password_changed`` (actor and ``user`` are the account, plus the address), written inside the same
  step as the change, so one that cannot be written rolls the change back (``500 audit_unavailable``). A wrong current
  password is ``auth.password_failed`` and the start of a wait ``auth.throttled``, as for a login. No password is ever
  in an answer, an audit entry or a log; the error messages are fixed sentences.
* ``serve --read-only`` refuses (the route is ``@local_write``: ``users.json`` is a local file), and an authenticator
  that does not keep the passwords here (``can_change_password`` false, as ``GET /api/v1/auth/me`` says) answers 403
  ``password_not_changeable``.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..accounts import AccountError, NoSuchUserError, PolicyError, WrongPasswordError
from .auth import (
    address_of,
    audit_event,
    local_write,
    refuse_if_throttled,
    session_cookie,
    session_document,
)
from .errors import ApiError, error_responses, policy_message
from .sessions import Session
from .users_api import failure as account_failure

API = "/api/v1/auth"
WORDING = {403: "Not allowed: a read-only server, or the passwords are not kept here",
           422: "The current password is wrong or the new one is not acceptable",
           500: "The accounts file or the audit log cannot be used"}
ME_SCHEMA = {"type": "object", "properties": {
    "username": {"type": "string"}, "role": {"enum": ["viewer", "admin"]}, "csrf_token": {"type": "string"},
    "idle_seconds_left": {"type": "integer"}, "session_seconds_left": {"type": "integer"},
    "can_change_password": {"type": "boolean"}}}


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(max_length=4096, repr=False)
    new_password: str = Field(max_length=4096, repr=False)


def failure(error: AccountError) -> ApiError:
    """The API error of a refused change, by the kind of refusal and with a fixed sentence (never the text of it)."""
    if isinstance(error, WrongPasswordError):
        return ApiError(422, "invalid_current_password", "The current password is wrong.")
    if isinstance(error, NoSuchUserError):          # the account went away while the request ran
        return ApiError(401, "not_logged_in", "Log in first.")
    if isinstance(error, PolicyError):
        return ApiError(422, "invalid_password", policy_message(error))
    return account_failure(error)


def router() -> APIRouter:
    api = APIRouter(prefix=API, tags=["auth"])

    @api.post("/password", summary="Change your own password", responses={
        200: {"description": "The password was changed: the other sessions of the user ended and this one has a new "
                             "cookie and CSRF token", "content": {"application/json": {"schema": ME_SCHEMA}}},
        **error_responses(401, 403, 422, 429, 500, text=WORDING)})
    @local_write
    def password_change(request: Request, body: PasswordChange) -> JSONResponse:
        auth = request.app.state.auth
        session: Session = request.state.session
        if not auth.accounts.can_change_password:
            raise ApiError(403, "password_not_changeable", "The password of this account cannot be changed here.")
        address = address_of(request)
        refuse_if_throttled(auth, address, session.username)
        try:
            user = auth.accounts.change_password(
                session.username, body.current_password, body.new_password,
                on_change=lambda changed: auth.audit.write("user.password_changed", changed.username,
                                                           address=address, user=changed.username))
        except WrongPasswordError as error:
            auth.throttle.failed(address, session.username)
            audit_event(request, "auth.password_failed", session.username)
            if auth.throttle.wait(address, session.username) > 0:
                audit_event(request, "auth.throttled", session.username)
            raise failure(error) from error
        except AccountError as error:
            raise failure(error) from error
        auth.throttle.succeeded(address, session.username)
        value, renewed = auth.sessions.renew(session, user)
        response = JSONResponse(session_document(auth, renewed))
        session_cookie(request, response, value)
        return response

    return api
