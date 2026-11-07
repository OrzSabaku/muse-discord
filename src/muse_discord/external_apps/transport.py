"""Invoke only an owner-registered MCP stdio command with explicit secrets."""

import asyncio
import os

from .policy import Refused


def call_tool(app, tool, arguments, root):
    try:
        return asyncio.run(_call(app, tool, arguments, root))
    except Refused:
        raise
    except Exception:
        raise Refused("APP_UNAVAILABLE") from None


async def _call(app, tool, arguments, root):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.types import CallToolResult

    # Credentials are opt-in per registered app. Do not inherit the Bot token,
    # unrelated provider tokens, PYTHONPATH, or shell startup configuration.
    env = {}
    for target, source in app.get("env", {}).items():
        if source not in os.environ:
            raise Refused("APP_CREDENTIALS_UNAVAILABLE")
        env[target] = os.environ[source]
    params = StdioServerParameters(
        command=app["command"][0], args=app["command"][1:], env=env, cwd=root
    )
    # SDK inherits a small OS environment allowlist in addition to env.
    async with asyncio.timeout(60):
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=45) as client:
                    await client.initialize()
                    result = await client.call_tool(tool, arguments)
    if not isinstance(result, CallToolResult) or result.is_error:
        raise Refused("APP_CALL_FAILED")
    payload = result.model_dump(mode="json", by_alias=True)
    from .policy import canonical

    if len(canonical(payload).encode()) > 1024 * 1024:
        raise Refused("APP_RESULT_TOO_LARGE")
    return payload
