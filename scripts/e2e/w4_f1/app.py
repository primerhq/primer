"""Owned probe child: production app/lifespan with a read-only runtime proof."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import uvicorn


def runtime_identity(app) -> dict:
    from primer.bus.postgres import PostgresEventBus
    from primer.claim.postgres import PostgresClaimEngine
    from primer.scheduler.postgres import PostgresScheduler
    from primer.storage.postgres import PostgresStorageProvider

    pairs = {'scheduler': PostgresScheduler, 'event_bus': PostgresEventBus,
             'claim_engine': PostgresClaimEngine, 'storage_provider': PostgresStorageProvider}
    for name, cls in pairs.items():
        if not isinstance(getattr(app.state, name), cls):
            raise ValueError('F1 requires actual PostgreSQL ' + name)
    from .probe import process_identity
    native = process_identity(os.getpid())
    return {name: type(getattr(app.state, name)).__name__ for name in pairs} | {
        'exe': native['exe'], 'starttime': native['starttime'], 'command': [os.fsdecode(v) for v in native['cmdline']],
        'pid': os.getpid(), 'worker_id': app.state.worker_pool.worker_id,
    }


def main():
    from primer.api.app import create_app
    from primer.api.config import AppConfig

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    config = AppConfig.model_validate(json.loads(args.config.read_text()))
    app = create_app(config)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def checked_lifespan(target):
        async with original(target):
            from .contracts import digest
            proof = runtime_identity(target) | {'config_path': str(args.config), 'config_sha256': digest(args.config.read_bytes())}
            with args.config.with_name('runtime.jsonl').open('a') as stream:
                stream.write(json.dumps(proof) + '\n')
            yield

    app.router.lifespan_context = checked_lifespan
    uvicorn.run(app, host=config.host, port=config.port, log_level='warning', timeout_graceful_shutdown=3)


if __name__ == '__main__':
    main()
