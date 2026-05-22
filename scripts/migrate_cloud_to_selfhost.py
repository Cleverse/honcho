#!/usr/bin/env -S uv run python
"""
Export Honcho Cloud workspace data and import into a self-hosted Honcho instance.

Preserves peer IDs, session IDs, and message content so LINE AI clients keep working.

Usage:
  # Export only (checkpoint JSONL under ./honcho-migration-checkpoint/)
  CLOUD_HONCHO_API_KEY=... uv run python scripts/migrate_cloud_to_selfhost.py export \\
    --workspace-id pr_cOROM7QwO1Ao

  # Import into self-hosted (after deploy + optional auth bootstrap)
  SELF_HOSTED_HONCHO_API_KEY=... uv run python scripts/migrate_cloud_to_selfhost.py import \\
    --self-hosted-url http://honcho-api:8000 \\
    --workspace-id pr_cOROM7QwO1Ao

  # Full pipeline
  CLOUD_HONCHO_API_KEY=... SELF_HOSTED_HONCHO_API_KEY=... \\
    uv run python scripts/migrate_cloud_to_selfhost.py run \\
    --self-hosted-url http://honcho-api:8000 \\
    --workspace-id pr_cOROM7QwO1Ao
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from honcho import Honcho
from honcho.api_types import MessageCreateParams, PeerConfig, SessionConfiguration

CLOUD_BASE_URL = "https://api.honcho.dev"
DEFAULT_WORKSPACE = "pr_cOROM7QwO1Ao"
CHECKPOINT_DIR = Path("honcho-migration-checkpoint")
BATCH_SIZE = 100


@dataclass
class PeerRecord:
    id: str
    metadata: dict[str, Any] | None
    configuration: dict[str, Any] | None


@dataclass
class SessionRecord:
    id: str
    metadata: dict[str, Any] | None
    configuration: dict[str, Any] | None
    peer_ids: list[str]


@dataclass
class MessageRecord:
    session_id: str
    peer_id: str
    content: str
    metadata: dict[str, Any] | None
    created_at: str | None


def _write_jsonl(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def export_workspace(workspace_id: str, cloud_api_key: str, checkpoint: Path) -> None:
    cloud = Honcho(
        workspace_id=workspace_id,
        api_key=cloud_api_key,
        base_url=CLOUD_BASE_URL,
    )

    print(f"Exporting workspace {workspace_id} from {CLOUD_BASE_URL}")

    peers: list[PeerRecord] = []
    for peer in cloud.peers(size=BATCH_SIZE):
        peers.append(
            PeerRecord(
                id=peer.id,
                metadata=peer.metadata,
                configuration=(
                    peer.configuration.model_dump(exclude_none=True)
                    if peer.configuration
                    else None
                ),
            )
        )
    _write_jsonl(checkpoint / "peers.jsonl", [asdict(p) for p in peers])
    print(f"  peers: {len(peers)}")

    sessions: list[SessionRecord] = []
    for session in cloud.sessions(size=BATCH_SIZE):
        session_peers = [p.id for p in session.peers()]
        sessions.append(
            SessionRecord(
                id=session.id,
                metadata=session.metadata,
                configuration=(
                    session.configuration.model_dump(exclude_none=True)
                    if session.configuration
                    else None
                ),
                peer_ids=session_peers,
            )
        )
    _write_jsonl(checkpoint / "sessions.jsonl", [asdict(s) for s in sessions])
    print(f"  sessions: {len(sessions)}")

    messages: list[MessageRecord] = []
    for session_rec in sessions:
        session = cloud.session(session_rec.id)
        for msg in session.messages(size=BATCH_SIZE):
            messages.append(
                MessageRecord(
                    session_id=session_rec.id,
                    peer_id=msg.peer_id,
                    content=msg.content,
                    metadata=msg.metadata,
                    created_at=(
                        msg.created_at.isoformat() if msg.created_at else None
                    ),
                )
            )
    _write_jsonl(checkpoint / "messages.jsonl", [asdict(m) for m in messages])
    print(f"  messages: {len(messages)}")

    summary = {
        "workspace_id": workspace_id,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "peer_count": len(peers),
        "session_count": len(sessions),
        "message_count": len(messages),
    }
    (checkpoint / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"Checkpoint written to {checkpoint.resolve()}")


def import_workspace(
    workspace_id: str,
    self_hosted_api_key: str,
    self_hosted_url: str,
    checkpoint: Path,
    *,
    dry_run: bool = False,
) -> None:
    target = Honcho(
        workspace_id=workspace_id,
        api_key=self_hosted_api_key,
        base_url=self_hosted_url,
    )

    peer_rows = _read_jsonl(checkpoint / "peers.jsonl")
    session_rows = _read_jsonl(checkpoint / "sessions.jsonl")
    message_rows = _read_jsonl(checkpoint / "messages.jsonl")

    print(
        f"Importing into {self_hosted_url} "
        f"({len(peer_rows)} peers, {len(session_rows)} sessions, {len(message_rows)} messages)"
    )
    if dry_run:
        print("Dry run — no writes.")
        return

    for row in peer_rows:
        config = (
            PeerConfig.model_validate(row["configuration"])
            if row.get("configuration")
            else None
        )
        target.peer(
            row["id"],
            metadata=row.get("metadata"),
            configuration=config,
        )
    print(f"  imported peers: {len(peer_rows)}")

    for row in session_rows:
        config = (
            SessionConfiguration.model_validate(row["configuration"])
            if row.get("configuration")
            else None
        )
        peer_ids = row.get("peer_ids") or []
        target.session(
            row["id"],
            metadata=row.get("metadata"),
            configuration=config,
            peers=peer_ids if peer_ids else None,
        )
    print(f"  imported sessions: {len(session_rows)}")

    by_session: dict[str, list[MessageRecord]] = {}
    for row in message_rows:
        by_session.setdefault(row["session_id"], []).append(
            MessageRecord(**row)
        )

    imported_messages = 0
    for session_id, session_messages in by_session.items():
        session = target.session(session_id)
        batch: list[MessageCreateParams] = []
        for msg in session_messages:
            created_at = None
            if msg.created_at:
                created_at = datetime.fromisoformat(
                    msg.created_at.replace("Z", "+00:00")
                )
            batch.append(
                MessageCreateParams(
                    content=msg.content,
                    peer_id=msg.peer_id,
                    metadata=msg.metadata,
                    created_at=created_at,
                )
            )
            if len(batch) >= BATCH_SIZE:
                session.add_messages(batch)
                imported_messages += len(batch)
                batch = []
        if batch:
            session.add_messages(batch)
            imported_messages += len(batch)

    print(f"  imported messages: {imported_messages}")
    print("Import complete. Monitor deriver queue until representation tasks drain.")


def validate_migration(
    workspace_id: str,
    cloud_api_key: str,
    self_hosted_api_key: str,
    self_hosted_url: str,
    checkpoint: Path,
) -> None:
    summary_path = checkpoint / "summary.json"
    if not summary_path.exists():
        print("No export summary found. Run export first.", file=sys.stderr)
        sys.exit(1)

    expected = json.loads(summary_path.read_text(encoding="utf-8"))
    cloud = Honcho(
        workspace_id=workspace_id, api_key=cloud_api_key, base_url=CLOUD_BASE_URL
    )
    target = Honcho(
        workspace_id=workspace_id,
        api_key=self_hosted_api_key,
        base_url=self_hosted_url,
    )

    def count_peers(client: Honcho) -> int:
        return sum(1 for _ in client.peers(size=BATCH_SIZE))

    def count_sessions(client: Honcho) -> int:
        return sum(1 for _ in client.sessions(size=BATCH_SIZE))

    cloud_peers = count_peers(cloud)
    cloud_sessions = count_sessions(cloud)
    target_peers = count_peers(target)
    target_sessions = count_sessions(target)

    print("Validation counts:")
    print(f"  peers     cloud={cloud_peers} self-hosted={target_peers} expected={expected['peer_count']}")
    print(f"  sessions  cloud={cloud_sessions} self-hosted={target_sessions} expected={expected['session_count']}")

    ok = (
        target_peers >= expected["peer_count"]
        and target_sessions >= expected["session_count"]
    )
    if not ok:
        print("WARNING: self-hosted counts below export checkpoint.", file=sys.stderr)
        sys.exit(1)
    print("Counts OK.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Honcho Cloud → self-hosted")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace-id", default=DEFAULT_WORKSPACE)
    common.add_argument(
        "--checkpoint-dir", type=Path, default=CHECKPOINT_DIR
    )

    export_p = sub.add_parser("export", parents=[common])
    export_p.add_argument(
        "--cloud-api-key",
        default=os.getenv("CLOUD_HONCHO_API_KEY"),
    )

    import_p = sub.add_parser("import", parents=[common])
    import_p.add_argument(
        "--self-hosted-url",
        default=os.getenv("SELF_HOSTED_HONCHO_URL", "http://localhost:8000"),
    )
    import_p.add_argument(
        "--self-hosted-api-key",
        default=os.getenv("SELF_HOSTED_HONCHO_API_KEY"),
    )
    import_p.add_argument("--dry-run", action="store_true")

    run_p = sub.add_parser("run", parents=[common])
    run_p.add_argument("--cloud-api-key", default=os.getenv("CLOUD_HONCHO_API_KEY"))
    run_p.add_argument(
        "--self-hosted-url",
        default=os.getenv("SELF_HOSTED_HONCHO_URL", "http://localhost:8000"),
    )
    run_p.add_argument(
        "--self-hosted-api-key",
        default=os.getenv("SELF_HOSTED_HONCHO_API_KEY"),
    )

    validate_p = sub.add_parser("validate", parents=[common])
    validate_p.add_argument(
        "--cloud-api-key", default=os.getenv("CLOUD_HONCHO_API_KEY")
    )
    validate_p.add_argument(
        "--self-hosted-url",
        default=os.getenv("SELF_HOSTED_HONCHO_URL", "http://localhost:8000"),
    )
    validate_p.add_argument(
        "--self-hosted-api-key",
        default=os.getenv("SELF_HOSTED_HONCHO_API_KEY"),
    )

    args = parser.parse_args()
    checkpoint = args.checkpoint_dir

    if args.command == "export":
        if not args.cloud_api_key:
            print("Set CLOUD_HONCHO_API_KEY or --cloud-api-key", file=sys.stderr)
            sys.exit(1)
        export_workspace(args.workspace_id, args.cloud_api_key, checkpoint)
    elif args.command == "import":
        if not args.self_hosted_api_key:
            print(
                "Set SELF_HOSTED_HONCHO_API_KEY or --self-hosted-api-key",
                file=sys.stderr,
            )
            sys.exit(1)
        import_workspace(
            args.workspace_id,
            args.self_hosted_api_key,
            args.self_hosted_url,
            checkpoint,
            dry_run=args.dry_run,
        )
    elif args.command == "run":
        if not args.cloud_api_key or not args.self_hosted_api_key:
            print(
                "Set CLOUD_HONCHO_API_KEY and SELF_HOSTED_HONCHO_API_KEY",
                file=sys.stderr,
            )
            sys.exit(1)
        export_workspace(args.workspace_id, args.cloud_api_key, checkpoint)
        import_workspace(
            args.workspace_id,
            args.self_hosted_api_key,
            args.self_hosted_url,
            checkpoint,
        )
        validate_migration(
            args.workspace_id,
            args.cloud_api_key,
            args.self_hosted_api_key,
            args.self_hosted_url,
            checkpoint,
        )
    elif args.command == "validate":
        if not args.cloud_api_key or not args.self_hosted_api_key:
            print(
                "Set CLOUD_HONCHO_API_KEY and SELF_HOSTED_HONCHO_API_KEY",
                file=sys.stderr,
            )
            sys.exit(1)
        validate_migration(
            args.workspace_id,
            args.cloud_api_key,
            args.self_hosted_api_key,
            args.self_hosted_url,
            checkpoint,
        )


if __name__ == "__main__":
    main()
