"""Verify a pasted bot credential, configure intents and save the connection.

No files change before input and existing configuration pass validation and
Discord confirms the bot and app owner. Network access goes through setup support.
"""

import contextlib
import os
import sys
from http import HTTPStatus

from muse_discord.setup import support as C
from muse_discord.setup.credentials import (
    AGAIN,
    WRONG_PASTE,
    classify,
    clean_paste,
    read_token_file,
)
from muse_discord.setup.files import config_problem, save_connection


def app_owner(app):
    """(owner user id, their username or None, owned by a team?)."""
    team = app.get("team")
    if isinstance(team, dict) and team.get("owner_user_id"):
        uid = str(team["owner_user_id"])
        name = next(
            (
                ((m.get("user") or {}).get("username"))
                for m in team.get("members") or []
                if str((m.get("user") or {}).get("id")) == uid
            ),
            None,
        )
        return uid, name, True
    owner = app.get("owner") or {}
    return str(owner.get("id") or ""), owner.get("username"), False


def get_token(args, rep):
    """The pasted token, from --token-file, --saved or stdin. Returns
    (token, exit code when it failed)."""
    if args.saved:
        try:
            token = C.read_token(C.load_settings(args.config))
        except C.settings_module().SettingsError:
            token = None
        if not token:
            return None, rep.fail("no_token", "No saved token yet.", 2)
        return token, None
    if args.token_file:
        saved = None
        with contextlib.suppress(Exception):
            saved = C.token_file(C.load_settings(args.config))
        if saved and os.path.realpath(os.path.expanduser(args.token_file)) == os.path.realpath(
            saved
        ):
            return None, rep.fail(
                "usage", "That is the saved token file; use --saved to reuse it.", 2
            )
        token, problem = read_token_file(args.token_file)
        if problem:
            return None, rep.fail("token_file", problem, 2)
        return token, None
    return sys.stdin.read(), None


def configure_intents(token, app, rep):
    # Message Content: GATEWAY_MESSAGE_CONTENT_LIMITED (an unverified
    # app's form of the intent) is one of the few flags the API lets an
    # app set itself. Either form counts as on.
    flags = int(app.get("flags") or 0)
    intent_ok = bool(flags & (C.MESSAGE_CONTENT | C.MESSAGE_CONTENT_LIMITED))
    if not intent_ok:
        try:
            try:
                C.call(
                    "PATCH",
                    "/applications/@me",
                    token,
                    {"flags": flags | C.MESSAGE_CONTENT_LIMITED},
                )
            except C.ApiError as e:
                if e.status != HTTPStatus.BAD_REQUEST:
                    raise
                # Some flags in the app's value may not be ours to send
                # back: send only the editable ones.
                C.call(
                    "PATCH",
                    "/applications/@me",
                    token,
                    {"flags": (flags & C.EDITABLE_FLAGS) | C.MESSAGE_CONTENT_LIMITED},
                )
            again = C.call("GET", "/applications/@me", token) or {}
            intent_ok = bool(
                int(again.get("flags") or 0) & (C.MESSAGE_CONTENT | C.MESSAGE_CONTENT_LIMITED)
            )
        except C.ApiError as e:
            rep.data["intent_error"] = str(e)
    rep.data["message_content"] = intent_ok
    if intent_ok:
        rep.line("OK: Message Content is on.")
    else:
        rep.step("intent")
    if app.get("bot_require_code_grant"):
        rep.step("code_grant")
    if app.get("interactions_endpoint_url"):
        rep.step("interactions_url")


def connected_identity(token, rep):
    try:
        me = C.call("GET", "/users/@me", token)
        if not (me or {}).get("bot"):
            return None, rep.fail("user_token", f"{WRONG_PASTE['user_token']} {AGAIN}", 2)
        app = C.call("GET", "/applications/@me", token) or {}
    except C.ApiError as e:
        if e.status == HTTPStatus.UNAUTHORIZED:
            return None, rep.fail("rejected", f"{WRONG_PASTE['rejected']} {AGAIN}", 2)
        return None, rep.fail(
            "unreachable", f"Could not reach Discord ({e}). Try again in a minute.", 3
        )
    app_id = str(app.get("id") or "")
    owner_id, owner_name, team = app_owner(app)
    if not app_id or not owner_id:
        return None, rep.fail(
            "unreachable", "Discord did not say which app this is. Try again in a minute.", 3
        )
    return (me, app, app_id, owner_id, owner_name, team), None


def connect(args, rep):
    token, code = get_token(args, rep)
    if code is not None:
        return code
    token = clean_paste(token)
    kind = classify(token)
    if kind != "token":
        return rep.fail(kind, f"{WRONG_PASTE[kind]} {AGAIN}", 2)
    bad = config_problem(args.config)
    if bad:
        return rep.fail(
            "config",
            f"config.yaml has a problem, so nothing "
            f"was changed. Fix config.yaml first (python3 "
            f"bin/settings.py validate names each problem), "
            f"then run this again. {bad}",
            6,
        )
    identity, code = connected_identity(token, rep)
    if code is not None:
        return code
    assert identity is not None
    me, app, app_id, owner_id, owner_name, team = identity
    bot_name = me.get("username") or "the bot"
    name = args.name or bot_name
    rep.data.update(app_id=app_id, owner_id=owner_id, bot_name=bot_name)
    rep.line(
        f"OK: This is the bot {bot_name} (app {app_id}); its owner "
        f"is Discord user {owner_id}" + (f" (@{owner_name})." if owner_name else ".")
    )
    if team:
        rep.data["team"] = True
        rep.line(
            "NOTE: This app belongs to a Discord team, so the owner "
            "set here is the team's owner, "
            + (f"@{owner_name}" if owner_name else f"user {owner_id}")
            + ". Ask the owner whether that is their Discord account; "
            "if not, change owners in config.yaml."
        )
    if args.name and args.name.casefold() != bot_name.casefold():
        rep.line(
            f"NOTE: The app is named {bot_name!r}, not {args.name!r}. "
            f"The bot still calls itself {args.name}; to change the "
            f"name people see, rename it on the Bot page."
        )

    configure_intents(token, app, rep)

    code = save_connection(args, token, owner_id, rep, name)
    if code is not None:
        return code

    url = C.invite_url(app_id)
    rep.data["invite_url"] = url
    rep.line(f"INVITE: {url}")
    code = 4 if rep.data["owner_steps"] else 0
    rep.finish()
    return code
