"""User management over the API: ``/api/v1/users`` (list, add, change role or disable, reset a password).

The accounts are the ones ``hlp web-user`` manages (``users.json`` in the data directory, ``accounts.AccountStore``),
with the same rules: a user name of 3 to 64 characters, a password of at least 12, the roles ``viewer`` and ``admin``,
and the **last enabled administrator can never be demoted or disabled**, on any path (the store enforces it under its
file lock).

* **Administrators only**, with the CSRF token, for everything including the list. ``serve --read-only`` refuses every
  change (``users.json`` is a local file).
* **A change takes effect at once.** A session re-checks its account on every request, so a user who is disabled, or
  whose role or password changed, is out on their next request (a role change ends the sessions too, so nobody keeps
  an administrator's session after a demotion) and logs in again.
* **Nothing secret goes out or in the log.** A response never holds a password or a hash; a password is read from the
  body only, is never part of an error, and the audit entries name the user and the role, never the password.
* **Audit.** The events are the ones the command line writes (``user.added``, ``user.role_changed``,
  ``user.disabled``, ``user.enabled``, ``user.password_reset``) with the administrator as the actor and the address of
  the request; when the audit entry cannot be written the change is rolled back (``500 audit_unavailable``).
* **No deleting** here: removing an account is ``hlp web-user delete`` (a disabled account cannot log in).
"""

from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..accounts import (
    AccountError,
    AuditWriteError,
    LastAdministratorError,
    NoSuchUserError,
    PolicyError,
    User,
    UserExistsError,
)
from ..util import printable
from .auth import address_of, admin, local_write
from .errors import ApiError, error_responses

API = "/api/v1/users"
UserP = Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]*$", description="The user name")]
USER_SCHEMA = {
    "type": "object", "required": ["username", "role", "disabled", "created_at", "last_login"],
    "properties": {"username": {"type": "string"}, "role": {"enum": ["viewer", "admin"]},
                   "disabled": {"type": "boolean"}, "created_at": {"type": "string"},
                   "last_login": {"type": ["string", "null"]}},
}
LIST_SCHEMA = {"type": "object", "required": ["items", "total"],
               "properties": {"items": {"type": "array", "items": USER_SCHEMA}, "total": {"type": "integer"}}}
USER_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    200: {"description": "The user", "content": {"application/json": {"schema": USER_SCHEMA}}},
    **error_responses(401, 403, 404, 409, 422, 500)}


def user_dict(user: User) -> Dict[str, Any]:
    """What the API says about an account: never the password hash."""
    return {"username": user.username, "role": user.role, "disabled": user.disabled, "created_at": user.created_at,
            "last_login": user.last_login}


def all_users(request: Request) -> List[User]:
    """Every account, in the order they were added; ``AccountError`` for an accounts file that cannot be used."""
    users: List[User] = request.app.state.auth.accounts.store.users()
    return users


def failure(error: AccountError) -> ApiError:
    """The API error for a refused account operation, by the kind of refusal; the message is fixed text (the policy
    messages of the accounts module never hold a password) except for a damaged file, which is described in the server
    log only."""
    if isinstance(error, PolicyError):
        return ApiError(422, "invalid_user", printable(str(error)))
    if isinstance(error, UserExistsError):
        return ApiError(409, "user_exists", "That user name is taken.")
    if isinstance(error, NoSuchUserError):
        return ApiError(404, "user_not_found", "There is no such user.")
    if isinstance(error, LastAdministratorError):
        return ApiError(409, "last_administrator", "The last enabled administrator cannot be demoted or disabled.")
    if isinstance(error, AuditWriteError):
        return ApiError(500, "audit_unavailable", "The audit log cannot be written, so the change was not made.")
    return ApiError(500, "accounts_unreadable", "The accounts file cannot be used; see the server log.")


class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=256)
    password: str = Field(max_length=4096, repr=False)
    role: Literal["viewer", "admin"] = "viewer"


class Change(BaseModel):
    """What to change; at least one of the two."""

    model_config = ConfigDict(extra="forbid")
    role: Optional[Literal["viewer", "admin"]] = None
    disabled: Optional[StrictBool] = None


class NewPassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=4096, repr=False)


def router() -> APIRouter:
    api = APIRouter(prefix=API, tags=["users"], dependencies=[Depends(admin)])

    def audit(request: Request, event: str, **fields: Any) -> None:
        """Write an audit entry; a failure raises ``AuditWriteError``, which rolls the account change back."""
        request.app.state.auth.audit.write(event, request.state.session.username, address=address_of(request),
                                           **fields)

    @api.get("", summary="The users",
             responses={200: {"description": "Every user, in the order they were added", "content": {
                 "application/json": {"schema": LIST_SCHEMA}}}, **error_responses(401, 403, 500)})
    def users_list(request: Request) -> Dict[str, Any]:
        try:
            users = all_users(request)
        except AccountError as error:
            raise failure(error) from error
        return {"items": [user_dict(user) for user in users], "total": len(users)}

    @api.post("", status_code=201, summary="Add a user", responses={
        201: {"description": "The user that was added", "content": {"application/json": {"schema": USER_SCHEMA}}},
        **error_responses(401, 403, 409, 422, 500)})
    @local_write
    def users_add(request: Request, body: NewUser) -> JSONResponse:
        store = request.app.state.auth.accounts.store
        try:
            user = store.add(body.username, body.role, body.password,
                             on_change=lambda added: audit(request, "user.added", user=added.username,
                                                           role=added.role))
        except AccountError as error:
            raise failure(error) from error
        return JSONResponse(user_dict(user), status_code=201)

    @api.patch("/{username}", summary="Change the role of a user or disable or enable them", responses=USER_RESPONSES)
    @local_write
    def users_change(request: Request, username: UserP, body: Change) -> Dict[str, Any]:
        if body.role is None and body.disabled is None:
            raise ApiError(422, "invalid_parameter", "Give a role and/or disabled.")

        def record(old: User, new: User) -> None:
            if new.role != old.role:
                audit(request, "user.role_changed", user=new.username, role=new.role)
            if new.disabled != old.disabled:
                audit(request, "user.disabled" if new.disabled else "user.enabled", user=new.username)

        try:
            user = request.app.state.auth.accounts.store.update(username, body.role, body.disabled, on_change=record)
        except AccountError as error:
            raise failure(error) from error
        return user_dict(user)

    @api.post("/{username}/password", summary="Reset the password of a user", responses=USER_RESPONSES)
    @local_write
    def users_reset_password(request: Request, username: UserP, body: NewPassword) -> Dict[str, Any]:
        try:
            user = request.app.state.auth.accounts.store.reset_password(
                username, body.password,
                on_change=lambda changed: audit(request, "user.password_reset", user=changed.username))
        except AccountError as error:
            raise failure(error) from error
        return user_dict(user)

    return api
