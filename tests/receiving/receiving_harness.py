"""Gateway and REST repair through independently assembled receiving services."""

import os
import queue
import sys
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.admission import AdmissionPolicy, SharedServers
from muse_discord.bursts import BurstEffects, CoalescePolicy, GatewayActivity, PendingBursts
from muse_discord.channel_access import ChannelAccess
from muse_discord.conversation import Conversation
from muse_discord.directory.scopes import DirectoryScopes
from muse_discord.muse_settings import Settings
from muse_discord.owner.presence import OwnerPresence
from muse_discord.receiving.gateway import GatewayEvents, GatewayIngress
from muse_discord.receiving.messages import ReceivedMessages, ReceptionEffects
from muse_discord.response_policy import ResponsePolicy


def message(mid, *, dm=False):
    msg = {
        "id": mid,
        "channel_id": "200",
        "author": {"id": "400", "username": "alice"},
        "mentions": [{"id": "999"}],
        "content": "<@999> hello",
    }
    if not dm:
        msg["guild_id"] = "100"
    return msg


def environment(get):
    state = {"last_seen": {"200": "10"}, "history": {}, "guild_joined": {"100": 0}}
    directory = DirectoryScopes({"200": {"guild_id": "100", "name": "general"}}, {}, {})
    settings = Mock(spec=Settings)
    resolved = {
        "who.replies": "when-addressed",
        "who.bots": "ignore",
        "who.allow": "everyone",
        "who.ignore": [],
        "who.dms_from": "owners",
    }
    settings.resolve.return_value = resolved
    settings.get.side_effect = lambda key, _: resolved[key]

    def owners():
        return {"300"}

    conversation = Conversation("999", "muse")
    response = ResponsePolicy(
        lambda: settings,
        owners,
        conversation,
        directory.for_message,
        directory.for_channel,
        Mock(),
        Mock(),
    )
    presence = OwnerPresence(owners, lambda: settings, get, lambda: False)
    admission = AdmissionPolicy(
        response,
        presence,
        directory,
        SharedServers(get, lambda: False, lambda: [], presence.absent),
    )
    bursts = PendingBursts(
        state,
        GatewayActivity(),
        CoalescePolicy(5, 15, conversation.is_bot_author),
        Mock(),
        BurstEffects(Mock(), Mock(return_value=False), Mock()),
    )
    receiver = ReceivedMessages(
        admission, bursts, get, ReceptionEffects(*[Mock() for _ in range(5)])
    )
    gateway = GatewayIngress(
        receiver,
        directory.channels,
        ChannelAccess(directory.channels, get, lambda: False),
        GatewayEvents(*[Mock() for _ in range(4)]),
        set(),
    )
    return receiver, gateway


def drain(gateway, *messages):
    inbox = queue.Queue()
    for msg in messages:
        inbox.put(msg)
    return gateway.drain(inbox)
