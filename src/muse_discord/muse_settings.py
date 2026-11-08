"""Public settings API; implementation lives in the settings package.

The engine is shared by monitor.py, watchdog.sh and the bin/ tools.
PyYAML handles input; settings_schema supplies Pydantic field validation.
Display labels and server-name lookup live in settings_display.

Layers, least specific first (the most specific value that is set wins):

    built-in -> everywhere -> dms                                  (a DM)
    built-in -> everywhere -> all_servers -> servers.<id>
             -> category -> channel                         (a server)

Threads and forum posts use their parent channel. Lists replace, never
merge. Only style.notes combines: the most specific of everywhere /
dms / server, then the category's notes, then the channel's.

At every layer the overlay (config.discord.yaml, written by Discord-side
changes) replaces the file's value at that same layer, unless the file
was edited after the change (the overlay entry's base no longer matches
the file): then the file wins and the entry is retired into history.

Caps (limits.*, media.per_day) count within the scope WHERE THEY
ARE SET: everywhere = one total, a server = that server, dms = all DMs.
Every cap set along the chain is enforced (see Settings.cap_layers)."""

from muse_discord.bot_credentials import DEFAULT_TOKEN_FILE as DEFAULT_TOKEN_FILE
from muse_discord.settings.changes import ApplyResult as ApplyResult
from muse_discord.settings.changes import Change as Change
from muse_discord.settings.changes import Planned as Planned
from muse_discord.settings.changes import WriteActor as WriteActor
from muse_discord.settings.changes import WriteStamp as WriteStamp
from muse_discord.settings.changes import change_path as change_path
from muse_discord.settings.editing import apply as apply
from muse_discord.settings.editing import reset as reset
from muse_discord.settings.editing import set_value as set_value
from muse_discord.settings.folding import backup as backup
from muse_discord.settings.folding import dump_config as dump_config
from muse_discord.settings.folding import fold as fold
from muse_discord.settings.history import group_current as group_current
from muse_discord.settings.history import group_undo_plan as group_undo_plan
from muse_discord.settings.history import history_since as history_since
from muse_discord.settings.history import undo as undo
from muse_discord.settings.history import undo_changes as undo_changes
from muse_discord.settings.history import undo_group as undo_group
from muse_discord.settings.history import undo_lines as undo_lines
from muse_discord.settings.history import undo_plan as undo_plan
from muse_discord.settings.history import undo_preview as undo_preview
from muse_discord.settings.history import undo_results as undo_results
from muse_discord.settings.loading import Watcher as Watcher
from muse_discord.settings.loading import load as load
from muse_discord.settings.loading import shell_value as shell_value
from muse_discord.settings.overlay import HISTORY_CAP as HISTORY_CAP
from muse_discord.settings.overlay import OVERLAY_HEADER as OVERLAY_HEADER
from muse_discord.settings.overlay import OVERLAY_NAME as OVERLAY_NAME
from muse_discord.settings.overlay import entry_role as entry_role
from muse_discord.settings.overlay import new_group_id as new_group_id
from muse_discord.settings.overlay import overlay_lock as overlay_lock
from muse_discord.settings.overlay import overlay_path_for as overlay_path_for
from muse_discord.settings.overlay import owner_layer as owner_layer
from muse_discord.settings.overlay import server_config_paths as server_config_paths
from muse_discord.settings.permissions import admin_changes as admin_changes
from muse_discord.settings.permissions import admin_check as admin_check
from muse_discord.settings.permissions import dm_access as dm_access
from muse_discord.settings.permissions import dms_from_changes as dms_from_changes
from muse_discord.settings.permissions import is_paused as is_paused
from muse_discord.settings.permissions import owners_view as owners_view
from muse_discord.settings.permissions import pause_changes as pause_changes
from muse_discord.settings.permissions import resume_changes as resume_changes
from muse_discord.settings.permissions import resume_value as resume_value
from muse_discord.settings.places import ADMIN_QUIETER as ADMIN_QUIETER
from muse_discord.settings.places import ADMIN_REPLIES as ADMIN_REPLIES
from muse_discord.settings.places import BASIC_KEYS as BASIC_KEYS
from muse_discord.settings.places import BOT_WIDE_KEYS as BOT_WIDE_KEYS
from muse_discord.settings.places import CHANNEL_KEYS as CHANNEL_KEYS
from muse_discord.settings.places import HIDDEN_KEYS as HIDDEN_KEYS
from muse_discord.settings.places import MORE_KEYS as MORE_KEYS
from muse_discord.settings.places import Place as Place
from muse_discord.settings.places import explain_lines as explain_lines
from muse_discord.settings.places import format_place as format_place
from muse_discord.settings.places import key_from_text as key_from_text
from muse_discord.settings.places import parse_place as parse_place
from muse_discord.settings.places import parse_scope_prefix as parse_scope_prefix
from muse_discord.settings.places import parse_value as parse_value
from muse_discord.settings.places import place_keys as place_keys
from muse_discord.settings.places import place_of_path as place_of_path
from muse_discord.settings.places import place_path as place_path
from muse_discord.settings.places import place_value as place_value
from muse_discord.settings.places import scope_prefix as scope_prefix
from muse_discord.settings.places import source_tag as source_tag
from muse_discord.settings.planning import plan_changes as plan_changes
from muse_discord.settings.planning import simulate as simulate
from muse_discord.settings.resolution import Settings as Settings
from muse_discord.settings.values import CHANNEL_ONLY as CHANNEL_ONLY
from muse_discord.settings.values import CHANNEL_SHORT as CHANNEL_SHORT
from muse_discord.settings.values import DEFAULT_PERSONA as DEFAULT_PERSONA
from muse_discord.settings.values import MIN_CHANNEL_KEY_PARTS as MIN_CHANNEL_KEY_PARTS
from muse_discord.settings.values import MIN_SERVER_KEY_PARTS as MIN_SERVER_KEY_PARTS
from muse_discord.settings.values import NO_CHANGE as NO_CHANGE
from muse_discord.settings.values import PAIR_LENGTH as PAIR_LENGTH
from muse_discord.settings.values import SERVER_SCOPE_PARTS as SERVER_SCOPE_PARTS
from muse_discord.settings.values import SOURCE_DATE_INDEX as SOURCE_DATE_INDEX
from muse_discord.settings.values import TOP_DISCORD_EDITABLE as TOP_DISCORD_EDITABLE
from muse_discord.settings.values import Scope as Scope
from muse_discord.settings.values import SettingsError as SettingsError
from muse_discord.settings.values import check_settable as check_settable
from muse_discord.settings.values import format_key as format_key
from muse_discord.settings.values import loosens as loosens
from muse_discord.settings.values import normalize as normalize
from muse_discord.settings.values import parse_key as parse_key
from muse_discord.settings.values import spec_of as spec_of
from muse_discord.settings_display import CATEGORY_TYPE as CATEGORY_TYPE
from muse_discord.settings_display import LABELS as LABELS
from muse_discord.settings_display import VALUE_LABELS as VALUE_LABELS
from muse_discord.settings_display import ServerInfo as ServerInfo
from muse_discord.settings_display import ValueChange as ValueChange
from muse_discord.settings_display import chain_names as chain_names
from muse_discord.settings_display import change_location as change_location
from muse_discord.settings_display import describe_change as describe_change
from muse_discord.settings_display import label as label
from muse_discord.settings_display import name_lookup as name_lookup
from muse_discord.settings_display import names_from as names_from
from muse_discord.settings_display import person_name as person_name
from muse_discord.settings_display import place_name as place_name
from muse_discord.settings_display import value_label as value_label
from muse_discord.settings_schema import CORE as CORE
from muse_discord.settings_schema import CORE_GROUPS as CORE_GROUPS
from muse_discord.settings_schema import NOTES_MAX as NOTES_MAX
from muse_discord.settings_schema import PRESENCE_KINDS as PRESENCE_KINDS
from muse_discord.settings_schema import TOP as TOP
from muse_discord.settings_schema import EngineSettings as EngineSettings
