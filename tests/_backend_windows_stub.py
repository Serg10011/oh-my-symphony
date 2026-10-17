"""Keep injected backend unit-process doubles outside the native supervisor."""

import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def mocked_windows_transport(monkeypatch):
    import symphony.backends.codex as codex
    import symphony.backends.per_turn as per_turn

    original_create = asyncio.create_subprocess_exec

    def adapter(module):
        native_spawn = module.spawn_backend

        async def spawn(argv, **options):
            if asyncio.create_subprocess_exec is original_create:
                return await native_spawn(argv, **options)
            # These tests deliberately supply a synthetic subprocess. Its stdio
            # is backend protocol, not the private READY/GO transport tested natively.
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=options["cwd"], env=options["env"], limit=options["limit"],
                stdin=asyncio.subprocess.PIPE if options["pipe_stdin"] else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            identity = module.process_identity(proc.pid)
            options["track_process"](proc, identity)
            if options["on_identified"] is not None:
                options["on_identified"](proc.pid, identity)
            elif options["on_started"] is not None:
                options["on_started"](proc.pid)
            await options["publish_started"](proc.pid)
            return proc, identity

        return spawn

    for module in (codex, per_turn):
        monkeypatch.setattr(module, "spawn_backend", adapter(module))
