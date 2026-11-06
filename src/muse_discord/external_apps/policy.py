"""Strict, owner-managed app policies. Never infer effects from tool names."""
import base64
import hashlib
import json
from pathlib import Path
import re

import yaml

LEVELS = ('off', 'read-only', 'confirm', 'write', 'unconditional')
EFFECTS = ('read', 'write', 'dangerous')


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise Refused('INVALID_POLICY')


def load_policy(path):
    try:
        data = yaml.safe_load(Path(path).read_text())
        _keys(data, ('version', 'apps', 'approval_public_key'), ('version', 'apps'))
        if type(data['version']) is not int or data['version'] != 1 or not isinstance(data['apps'], dict):
            raise Refused('INVALID_POLICY')
        key = data.get('approval_public_key')
        if key is not None and (not isinstance(key, str)
                                or len(base64.b64decode(key, validate=True)) != 32):
            raise Refused('INVALID_POLICY')
        for name, app in data['apps'].items():
            if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name):
                raise Refused('INVALID_POLICY')
            _keys(app, ('level', 'command', 'env', 'tools'), ('level', 'command', 'tools'))
            command = app['command']
            if (app['level'] not in LEVELS or not isinstance(command, list) or not command
                    or not all(isinstance(v, str) and v and '\0' not in v for v in command)
                    or not Path(command[0]).is_absolute() or not isinstance(app['tools'], dict)):
                raise Refused('INVALID_POLICY')
            env = app.get('env', {})
            if not isinstance(env, dict) or not all(
                    isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
                    and isinstance(v, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v)
                    for k, v in env.items()):
                raise Refused('INVALID_POLICY')
            for tool, rule in app['tools'].items():
                if not isinstance(tool, str) or not tool:
                    raise Refused('INVALID_POLICY')
                _keys(rule, ('effect', 'auto_write', 'constraints'), ('effect',))
                constraints = rule.get('constraints', {})
                if (rule['effect'] not in EFFECTS
                        or type(rule.get('auto_write', False)) is not bool
                        or not isinstance(constraints, dict)):
                    raise Refused('INVALID_POLICY')
                for parameter, values in constraints.items():
                    if (not isinstance(parameter, str) or not parameter or not isinstance(values, list)
                            or not values or not all(isinstance(v, (str, int, bool)) for v in values)):
                        raise Refused('INVALID_POLICY')
                if rule.get('auto_write') and (rule['effect'] != 'write' or not constraints):
                    raise Refused('INVALID_POLICY')
        canonical(data)
        return data
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise Refused('INVALID_POLICY') from None


def evaluate(policy, global_level, app_id, tool, arguments):
    if global_level not in LEVELS:
        raise Refused('INVALID_POLICY')
    app = policy['apps'].get(app_id)
    rule = app.get('tools', {}).get(tool) if app else None
    if rule is None:
        raise Refused('TOOL_NOT_REGISTERED')
    level = min(LEVELS.index(global_level), LEVELS.index(app['level']))
    effect = rule['effect']
    if level == 0 or (effect != 'read' and level < 2):
        raise Refused('POLICY_DENIED')
    if not isinstance(arguments, dict):
        raise Refused('INVALID_ARGUMENTS')
    try:
        if len(canonical(arguments).encode()) > 65536:
            raise Refused('INVALID_ARGUMENTS')
    except (ValueError, TypeError):
        raise Refused('INVALID_ARGUMENTS') from None
    for parameter, values in rule.get('constraints', {}).items():
        if parameter not in arguments or not any(
                type(arguments[parameter]) is type(value) and arguments[parameter] == value
                for value in values):
            raise Refused('TARGET_NOT_ALLOWED')
    needs_approval = level < LEVELS.index('unconditional') and (
        effect == 'dangerous' or (effect == 'write' and not (
            level == LEVELS.index('write') and rule.get('auto_write') is True)))
    return app, effect, needs_approval
