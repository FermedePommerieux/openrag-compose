import asyncio
import copy
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from cachetools import LRUCache

from config.paths import get_flows_source_metadata
from config.settings import (
    AGENT_COMPONENT_DISPLAY_NAME,
    LANGFLOW_CHAT_FLOW_ID,
    LANGFLOW_INGEST_FLOW_ID,
    LANGFLOW_URL,
    LANGFLOW_URL_INGEST_FLOW_ID,
    NUDGES_FLOW_ID,
    OPENAI_EMBEDDING_COMPONENT_DISPLAY_NAME,
    OPENAI_LLM_COMPONENT_DISPLAY_NAME,
    clients,
    get_openrag_config,
)
from utils.logging_config import get_logger
from utils.telemetry import Category, MessageId, TelemetryClient

logger = get_logger(__name__)

_UNSET = object()

# This migration is intentionally narrow.  A flow is only eligible when it
# still contains this exact, locked OpenRAG default component from the
# lifecycle baseline; arbitrary user edits must require an explicit update.
_LEGACY_RETRIEVAL_COMPONENT = (
    "ext:openrag:OpenSearchVectorStoreComponentMultimodalMultiEmbedding@extra"
)
_BACKEND_RETRIEVAL_COMPONENT = "ext:openrag:OpenRAGBackendRetrievalComponent@extra"
_RETRIEVAL_FLOW_MIGRATION_VERSION = 13
_LEGACY_SYSTEM_FLOW_ID = "1098eea1-6649-4e1d-aed1-b77249fb8dd0"
# These fields are rewritten by OpenRAG's settings synchronization and by
# Langflow's provider refresh endpoint. Their values and option lists are
# configuration state, not flow-graph customization. Every other field,
# including component code, prompt, nodes and edges, remains fingerprinted.
_RUNTIME_MANAGED_RETRIEVAL_COMPONENTS = frozenset(
    {AGENT_COMPONENT_DISPLAY_NAME, "Embedding Model", "Language Model"}
)
_RUNTIME_MANAGED_RETRIEVAL_FIELDS = frozenset(
    {
        "api_key",
        "api_base",
        "ollama_base_url",
        "base_url_ibm_watsonx",
        "project_id",
        "model",
    }
)
# SHA-256 of ``flows/openrag_agent.json`` at lifecycle baseline 156f3664,
# calculated over canonical ``data`` JSON.  Flow IDs and a lock alone are not
# sufficient proof that startup owns a graph: a customised system flow must be
# left untouched and migrated deliberately by its operator.
_LEGACY_RETRIEVAL_GRAPH_SHA256 = "1b1395db6d9d3890209dfe79820b558ae6e024e436b7985a873751b5ff7c67f0"
# Exact fingerprints of the first Retrieval v2 graph, before and after its
# runtime version marker.  They authorize only the prompt-only upgrade to the
# document-wide evidence rule; arbitrary operator edits still fail closed.
_PREVIOUS_RETRIEVAL_GRAPH_SHA256 = (
    "82980b36b952c2762bb9b5223da66413e8590814fc025caed2e2d52adcf04cd1"
)
_PREVIOUS_VERSIONED_RETRIEVAL_GRAPH_SHA256 = (
    "828d33b5a0b6dc401713e1cfdc7b8d88cbb46d4a53a392ccb866d8858787b16e"
)
# Exact fingerprints of the role-evidence prompt revision. These authorize
# only the next prompt-only upgrade; arbitrary Langflow edits still fail closed.
_PREVIOUS_ROLE_EVIDENCE_GRAPH_SHA256 = (
    "313d28617f1b34904f9d772797f89103ef4d7ceb1d1784255790be922978966a"
)
_PREVIOUS_VERSIONED_ROLE_EVIDENCE_GRAPH_SHA256 = (
    "20012fd2b1e56830e58e2785fd362cc89f87ea766b95cdc23283aaddcf3fd795"
)
# Same repository-owned v3 graph with only the historical named query example
# replaced by its domain-neutral equivalent. Keep both fingerprints so an
# upgrade can recognize either bundled revision without weakening graph checks.
_GENERICIZED_VERSIONED_ROLE_EVIDENCE_GRAPH_SHA256 = (
    "243271a5a255999cffdf8bd3e8616ca812b80741833741ef942104d65d0320da"
)
# Exact fingerprints of the evidence-first prompt before relationship
# attribution became an explicit corpus-wide invariant.
_PREVIOUS_EVIDENCE_FIRST_GRAPH_SHA256 = (
    "a1f64b1b5bbcb07e055df36cdf1e9bd4c05d4818dfa567ef1de5edceba34cc02"
)
_PREVIOUS_VERSIONED_EVIDENCE_FIRST_GRAPH_SHA256 = (
    "15bcca4d7e21f4f98fa2020ff9c76b43ecd0a96c240565434458927b1d57518b"
)
# Exact fingerprint of Retrieval v2 version 5. This is the deployed
# repository-owned graph immediately before exhaustive evidence pagination.
# It authorizes that one upgrade only; any operator edit still fails closed.
_PREVIOUS_VERSIONED_FOCUSED_RETRIEVAL_GRAPH_SHA256 = (
    "4e4a839c17ffa6b36ee5ee4ac93e60c83fd43b8d16af96c1dda8c94cc1b91621"
)
# Exact fingerprints of Retrieval v2 version 6 before the chat tool stopped
# exposing its internal evidence-mode switch. They authorize only the narrow
# repository-owned v6 -> v7 replacement; altered Langflow graphs still fail
# closed and require operator review.
_PREVIOUS_DOCUMENTALIST_RETRIEVAL_GRAPH_SHA256 = (
    "94f6ef27a2d585dbc848b4c44e3a223d04635a651a88586d46cc7f6ec8c153f3"
)
_PREVIOUS_VERSIONED_DOCUMENTALIST_RETRIEVAL_GRAPH_SHA256 = (
    "d1434e9f42495a4f8485c1296b2ed57ee5b0dbe288b310cce8eb0e9422a6a8ee"
)
# The same v6 graph after replacing only settings-managed provider fields with
# sentinels. This recognizes a configured repository-owned flow while still
# rejecting changes to prompts, code, topology, retrieval, or any other field.
_PREVIOUS_VERSIONED_DOCUMENTALIST_RUNTIME_GRAPH_SHA256 = (
    "deda60c4c3957fbdd5a8d6ba00bef685d32e8fde78ef2ef9497f221dcf9a81cc"
)
# Exact fingerprints of the focused-only Retrieval v2 version 7 graph. They
# authorize only the repository-owned v7 -> v8 scope-exhaustive replacement;
# prompt, tool code and topology edits remain ineligible for auto-migration.
_PREVIOUS_VERSIONED_SCOPELESS_RETRIEVAL_GRAPH_SHA256 = (
    "bbd64ea64748792a3f216e0d5f8c9a9fadd08911f16709ba9a3a88eed0630c36"
)
_PREVIOUS_VERSIONED_SCOPELESS_RUNTIME_GRAPH_SHA256 = (
    "5b856087395752a8bd0f146a5eab8d09a72135955e743813996ac3befbb6ae67"
)
# Exact fingerprints of the frozen Phase 1 Retrieval v2 version 8 graph.
# They authorize only the repository-owned artifact-boundary replacement;
# custom code, prompts, topology or other operator edits still fail closed.
_PREVIOUS_VERSIONED_SCOPE_ARTIFACT_GRAPH_SHA256 = (
    "700c41e04b14631c0fb1e19a751ecde0ca9af90d95577d6eb40944b412dd7066"
)
_PREVIOUS_VERSIONED_SCOPE_ARTIFACT_RUNTIME_GRAPH_SHA256 = (
    "93c5ad3b275a94705229298c38db0f22d7adbbf3555b2ce49eff008a478b4293"
)
# Exact fingerprints of the runtime-safe Retrieval v2 version 9 graph. They
# authorize only the repository-owned projection update that exposes the
# versioned scope-policy certificate and bounded context metadata to Langflow.
_PREVIOUS_VERSIONED_RUNTIME_SAFE_GRAPH_SHA256 = (
    "0f8bdfdf0983292eb61c9d3c62a717e16916e8cc217de74239017854cf1a2780"
)
_PREVIOUS_VERSIONED_RUNTIME_SAFE_RUNTIME_GRAPH_SHA256 = (
    "53ea921ab9e9363fdd63d21fa44c2aff419bbf14b409d5ce142ded887e2675b8"
)
# Exact fingerprints of the frozen Phase 2 Retrieval v2 version 10 graph.
# They authorize only the repository-owned deterministic agent-guard upgrade;
# prompt, component code, topology and other operator changes still fail closed.
_PREVIOUS_VERSIONED_SCOPE_POLICY_GRAPH_SHA256 = (
    "e864388b7a98d4422ad7725cc93c72eea3ab1ee8c341d08300dbbf76b0d22622"
)
_PREVIOUS_VERSIONED_SCOPE_POLICY_RUNTIME_GRAPH_SHA256 = (
    "b49c2e5317fa124b3576b7c26bf40622481da82690e3a769ab6f6be07ad67f69"
)
# Exact fingerprints of the deterministic agent-guard Retrieval v2 version 11
# graph. They authorize only the repository-owned bounded multi-query tool
# upgrade; arbitrary code, prompt and topology edits still fail closed.
_PREVIOUS_VERSIONED_AGENT_GUARD_GRAPH_SHA256 = (
    "4eea8e4b855bb55e96fb070ba03374bd6ad98d23eb1ef367af7ede3dfa2dd42b"
)
_PREVIOUS_VERSIONED_AGENT_GUARD_RUNTIME_GRAPH_SHA256 = (
    "353a4e723dc6174982e381b3888cd183744becb876290a1b2e9b8bc789de970a"
)
# Exact fingerprints of Retrieval v2 version 12. They authorize only the
# fail-closed retrieval execution contract and structured warning projection;
# any other component, prompt or topology change remains ineligible.
_PREVIOUS_VERSIONED_MULTI_QUERY_GRAPH_SHA256 = (
    "3db0e2579d66afe41e4d34a790f2847415301aaf3e8a77994e9f901e2255b012"
)
_PREVIOUS_VERSIONED_MULTI_QUERY_RUNTIME_GRAPH_SHA256 = (
    "2410b7449648a6fad712ef6bf00c08920df7685f01641cbd7ad3d1d0d604b494"
)


class FlowsService:
    def __init__(self) -> None:
        # Ephemeral dict of dismissed flow update notifications per user_id
        self._dismissed_updates: dict[str, set[str]] = {}
        # Cache for flow file mappings to avoid repeated filesystem scans
        self._flow_file_cache: dict[str, str] = {}
        # Semaphores to prevent race conditions when updating the same flow concurrently
        self._flow_locks = LRUCache(maxsize=128)
        self._lock_creation_lock = asyncio.Lock()
        # Cache for enabled models to avoid redundant API calls
        self._enabled_models_cache: dict[str, Any] | None = None
        self._enabled_models_cache_time: datetime | None = None
        self._enabled_models_cache_ttl = 60  # seconds
        self._enabled_models_lock = asyncio.Lock()
        # Cache for available custom flow updates
        self._custom_updates_cache: list[dict[str, Any]] | None = None

    def dismiss_flows_updates(
        self, flow_types: list[str] | None = None, user_id: str | None = None
    ) -> None:
        """Dismiss flow update notifications ephemerally in backend memory per user."""
        if not hasattr(self, "_dismissed_updates") or not isinstance(self._dismissed_updates, dict):
            self._dismissed_updates = {}
        target_user = user_id or "default"
        if target_user not in self._dismissed_updates:
            self._dismissed_updates[target_user] = set()

        if flow_types:
            self._dismissed_updates[target_user].update(flow_types)
        else:
            self._dismissed_updates[target_user].update(
                ["nudges", "retrieval", "ingest", "url_ingest"]
            )

    def clear_dismissed_update(self, flow_type: str, user_id: str | None = None):
        if hasattr(self, "_dismissed_updates") and isinstance(self._dismissed_updates, dict):
            if user_id:
                if user_id in self._dismissed_updates:
                    self._dismissed_updates[user_id].discard(flow_type)
            else:
                for user_set in self._dismissed_updates.values():
                    user_set.discard(flow_type)

    async def resolve_ollama_url(self, endpoint: str, force_refresh: bool = False) -> str:
        """Find the correct Ollama URL by probing candidates via Langflow's validate-provider API."""
        from config.config_manager import config_manager

        config = config_manager.get_config()

        from utils.container_utils import (
            get_container_host,
            is_localhost_url,
            replace_localhost_patterns,
            transform_localhost_url,
        )

        # If not forcing, check if we already have a resolved endpoint for this original endpoint
        if not force_refresh and config.providers.ollama.resolved_endpoint:
            # We only use the cached one if the original endpoint is still a localhost one
            if is_localhost_url(endpoint):
                logger.debug(
                    f"Using cached resolved Ollama URL: {config.providers.ollama.resolved_endpoint}"
                )
                return config.providers.ollama.resolved_endpoint

        if not is_localhost_url(endpoint):
            return endpoint

        # Candidates to probe
        candidates = ["host.containers.internal", "host.docker.internal"]
        detected_host = get_container_host()
        if detected_host and detected_host not in candidates:
            candidates.insert(0, detected_host)

        resolved_url = None
        for cand in candidates:
            test_url = replace_localhost_patterns(endpoint, cand)

            logger.debug(f"Probing Ollama candidate via Langflow: {test_url}")
            try:
                response = await clients.langflow_request(
                    "POST",
                    "/api/v1/models/validate-provider",
                    json={"provider": "Ollama", "variables": {"OLLAMA_BASE_URL": test_url}},
                )
                if response.status_code in (200, 201) and response.json().get("valid"):
                    logger.info(f"Resolved Ollama URL via Langflow: {test_url}")
                    resolved_url = test_url
                    break
            except Exception as e:
                logger.debug(f"Probe failed for {test_url}: {e}")
                continue

        if not resolved_url:
            # Fallback to simple transformation if probing fails
            resolved_url = transform_localhost_url(endpoint)

        # Cache the result if it changed
        if resolved_url and resolved_url != config.providers.ollama.resolved_endpoint:
            config.providers.ollama.resolved_endpoint = resolved_url
            config_manager.save_config_file(config)
            logger.debug(f"Saved resolved Ollama URL to config: {resolved_url}")

        return resolved_url

    async def _get_flow_lock(self, flow_id: str) -> asyncio.Lock:
        """Get or create an asyncio.Lock for a specific flow ID"""
        async with self._lock_creation_lock:
            if flow_id not in self._flow_locks:
                self._flow_locks[flow_id] = asyncio.Lock()
            return self._flow_locks[flow_id]

    def _get_flows_directory(self):
        """Get the flows directory path"""
        from config.paths import get_flows_path

        return get_flows_path()

    def _get_backup_directory(self):
        """Get the backup directory path"""
        from config.paths import get_flows_backup_path

        backup_dir = get_flows_backup_path()
        os.makedirs(backup_dir, exist_ok=True)
        return backup_dir

    def _get_latest_backup_path(self, flow_id: str, flow_type: str):
        """
        Get the path to the latest backup file for a flow.

        Args:
            flow_id: The flow ID
            flow_type: The flow type name

        Returns:
            str: Path to latest backup file, or None if no backup exists
        """
        backup_dir = self._get_backup_directory()

        if not os.path.exists(backup_dir):
            return None

        # Find all backup files for this flow
        backup_files = []
        prefix = f"{flow_type}_"

        try:
            for filename in os.listdir(backup_dir):
                if filename.startswith(prefix) and filename.endswith(".json"):
                    file_path = os.path.join(backup_dir, filename)
                    # Get modification time for sorting
                    mtime = os.path.getmtime(file_path)
                    backup_files.append((mtime, file_path))
        except Exception as e:
            logger.warning(f"Error reading backup directory: {str(e)}")
            return None

        if not backup_files:
            return None

        # Return the most recent backup (highest mtime)
        backup_files.sort(key=lambda x: x[0], reverse=True)
        return backup_files[0][1]

    def _compare_flows(self, flow1: dict, flow2: dict):
        """
        Compare two flow structures to see if they're different.
        Normalizes both flows before comparison.

        Args:
            flow1: First flow data
            flow2: Second flow data

        Returns:
            bool: True if flows are different, False if they're the same
        """
        normalized1 = self._normalize_flow_structure(flow1)
        normalized2 = self._normalize_flow_structure(flow2)

        # Compare normalized structures
        return normalized1 != normalized2

    async def backup_all_flows(self, only_if_changed=True):
        """
        Backup all flows from Langflow to the backup folder.
        Only backs up flows that have changed since the last backup.

        Args:
            only_if_changed: If True, only backup flows that differ from latest backup

        Returns:
            dict: Summary of backup operations with success/failure status
        """
        backup_results = {
            "success": True,
            "backed_up": [],
            "skipped": [],
            "failed": [],
        }

        flow_configs = [
            ("nudges", NUDGES_FLOW_ID),
            ("retrieval", LANGFLOW_CHAT_FLOW_ID),
            ("ingest", LANGFLOW_INGEST_FLOW_ID),
            ("url_ingest", LANGFLOW_URL_INGEST_FLOW_ID),
        ]

        logger.info("Starting periodic backup of Langflow flows")

        async def backup_single_flow(flow_type, flow_id):
            if not flow_id:
                return None

            try:
                # Get current flow from Langflow
                response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                if response.status_code != 200:
                    logger.warning(
                        f"Failed to get flow {flow_id} for backup: HTTP {response.status_code}"
                    )
                    return {
                        "type": "failed",
                        "data": {
                            "flow_type": flow_type,
                            "flow_id": flow_id,
                            "error": f"HTTP {response.status_code}",
                        },
                    }

                current_flow = response.json()

                # Check if flow is locked and if we should skip backup
                flow_locked = current_flow.get("locked", False)
                latest_backup_path = self._get_latest_backup_path(flow_id, flow_type)
                has_backups = latest_backup_path is not None

                # If flow is locked and no backups exist, skip backup
                if flow_locked and not has_backups:
                    logger.debug(
                        f"Flow {flow_type} (ID: {flow_id}) is locked and has no backups, skipping backup"
                    )
                    return {
                        "type": "skipped",
                        "data": {
                            "flow_type": flow_type,
                            "flow_id": flow_id,
                            "reason": "locked_without_backups",
                        },
                    }

                # Check if we need to backup (only if changed)
                if only_if_changed and has_backups:
                    try:
                        with open(latest_backup_path) as f:
                            latest_backup = json.load(f)

                        # Compare flows
                        if not self._compare_flows(current_flow, latest_backup):
                            logger.debug(
                                f"Flow {flow_type} (ID: {flow_id}) unchanged, skipping backup"
                            )
                            return {
                                "type": "skipped",
                                "data": {
                                    "flow_type": flow_type,
                                    "flow_id": flow_id,
                                    "reason": "unchanged",
                                },
                            }
                    except Exception as e:
                        logger.warning(
                            f"Failed to read latest backup for {flow_type} (ID: {flow_id}): {str(e)}"
                        )

                # Backup the flow
                backup_path = await self._backup_flow(flow_id, flow_type, current_flow)
                if backup_path:
                    return {
                        "type": "backed_up",
                        "data": {
                            "flow_type": flow_type,
                            "flow_id": flow_id,
                            "backup_path": backup_path,
                        },
                    }
                else:
                    return {
                        "type": "failed",
                        "data": {
                            "flow_type": flow_type,
                            "flow_id": flow_id,
                            "error": "Backup returned None",
                        },
                    }
            except Exception as e:
                logger.error(f"Failed to backup {flow_type} flow (ID: {flow_id}): {str(e)}")
                return {
                    "type": "failed",
                    "data": {
                        "flow_type": flow_type,
                        "flow_id": flow_id,
                        "error": str(e),
                    },
                }

        tasks = [backup_single_flow(ft, fi) for ft, fi in flow_configs]
        results = await asyncio.gather(*tasks)

        for result in results:
            if not result:
                continue
            res_type = result.get("type")
            if res_type in ("backed_up", "skipped", "failed"):
                target_list = backup_results[res_type]
                if isinstance(target_list, list):
                    target_list.append(result.get("data"))
            if res_type == "failed":
                backup_results["success"] = False

        logger.info(
            "Completed periodic backup of flows",
            backed_up_count=len(backup_results["backed_up"]),
            skipped_count=len(backup_results["skipped"]),
            failed_count=len(backup_results["failed"]),
        )

        # Send telemetry event
        if backup_results["failed"]:
            await TelemetryClient.send_event(
                Category.FLOW_OPERATIONS, MessageId.ORB_FLOW_BACKUP_FAILED
            )
        else:
            await TelemetryClient.send_event(
                Category.FLOW_OPERATIONS, MessageId.ORB_FLOW_BACKUP_COMPLETE
            )

        return backup_results

    async def _backup_flow(self, flow_id: str, flow_type: str, flow_data: dict | None = None):
        """
        Backup a single flow to the backup folder.

        Args:
            flow_id: The flow ID to backup
            flow_type: The flow type name (nudges, retrieval, ingest, url_ingest)
            flow_data: The flow data to backup (if None, fetches from API)

        Returns:
            str: Path to the backup file, or None if backup failed
        """
        try:
            # Get flow data if not provided
            if flow_data is None:
                response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                if response.status_code != 200:
                    logger.warning(
                        f"Failed to get flow {flow_id} for backup: HTTP {response.status_code}"
                    )
                    return None
                flow_data = response.json()

            # Create backup directory if it doesn't exist
            backup_dir = self._get_backup_directory()

            # Generate backup filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"{flow_type}_{timestamp}.json"
            backup_path = os.path.join(backup_dir, backup_filename)

            # Save flow to backup file
            with open(backup_path, "w") as f:
                json.dump(flow_data, f, indent=2, ensure_ascii=False)

            logger.info(
                f"Backed up {flow_type} flow (ID: {flow_id}) to {backup_filename}",
                backup_path=backup_path,
            )

            return backup_path

        except Exception as e:
            logger.error(
                f"Failed to backup flow {flow_id} ({flow_type}): {str(e)}",
                error=str(e),
            )
            return None

    def _find_flow_file_by_id(self, flow_id: str):
        """
        Scan the flows directory and find the JSON file that contains the specified flow ID.

        Args:
            flow_id: The flow ID to search for

        Returns:
            str: The path to the flow file, or None if not found
        """
        if not flow_id:
            raise ValueError("flow_id is required")

        # Check cache first
        if flow_id in self._flow_file_cache:
            cached_path = self._flow_file_cache[flow_id]
            if os.path.exists(cached_path):
                return cached_path
            else:
                # Remove stale cache entry
                del self._flow_file_cache[flow_id]

        flows_dir = self._get_flows_directory()

        if not os.path.exists(flows_dir):
            logger.warning(f"Flows directory not found: {flows_dir}")
            return None

        # Scan all JSON files in the flows directory
        try:
            for filename in os.listdir(flows_dir):
                if not filename.endswith(".json"):
                    continue

                file_path = os.path.join(flows_dir, filename)

                try:
                    with open(file_path) as f:
                        flow_data = json.load(f)

                    # Check if this file contains the flow we're looking for
                    if flow_data.get("id") == flow_id:
                        # Cache the result
                        self._flow_file_cache[flow_id] = file_path
                        logger.info(f"Found flow {flow_id} in file: {filename}")
                        return file_path

                except (json.JSONDecodeError, FileNotFoundError) as e:
                    logger.warning(f"Error reading flow file {filename}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error scanning flows directory: {e}")
            return None

        logger.warning(f"Flow with ID {flow_id} not found in flows directory")
        return None

    def _get_flow_id_by_type(self, flow_type: str) -> str:
        if flow_type == "nudges":
            flow_id = NUDGES_FLOW_ID
        elif flow_type == "retrieval":
            flow_id = LANGFLOW_CHAT_FLOW_ID
        elif flow_type == "ingest":
            flow_id = LANGFLOW_INGEST_FLOW_ID
        elif flow_type == "url_ingest":
            flow_id = LANGFLOW_URL_INGEST_FLOW_ID
        else:
            raise ValueError(
                "flow_type must be either 'nudges', 'retrieval', 'ingest', or 'url_ingest'"
            )
        if not flow_id:
            raise ValueError(f"Flow ID not configured for flow_type '{flow_type}'")
        return flow_id

    async def reset_langflow_flow(self, flow_type: str):
        """Reset a Langflow flow by uploading the corresponding JSON file

        Args:
            flow_type: Either 'nudges', 'retrieval', 'ingest', or 'url_ingest'

        Returns:
            dict: Success/error response
        """
        flow_id = self._get_flow_id_by_type(flow_type)
        flow_lock = await self._get_flow_lock(flow_id)
        async with flow_lock:
            result = await self._reset_langflow_flow_locked(flow_type, flow_id)
        if result.get("success"):
            self._custom_updates_cache = None
            try:
                config = get_openrag_config()
                if config.edited:
                    from api.settings import reapply_all_settings

                    await reapply_all_settings()
            except Exception as e:
                logger.error(f"Error reapplying settings after flow reset: {e}")
        return result

    async def _unlock_flow(self, flow_id: str):
        """Unlock a flow before making changes."""
        try:
            response = await clients.langflow_request(
                "PATCH", f"/api/v1/flows/{flow_id}", json={"locked": False}
            )
            if response.status_code not in (200, 404):
                logger.warning(
                    f"Failed to unlock flow {flow_id}: HTTP {response.status_code} - {response.text}"
                )
        except Exception as e:
            logger.warning(f"Error unlocking flow {flow_id}: {e}")

    async def _lock_flow(self, flow_id: str):
        """Lock a flow after making changes."""
        try:
            response = await clients.langflow_request(
                "PATCH", f"/api/v1/flows/{flow_id}", json={"locked": True}
            )
            if response.status_code not in (200, 404):
                logger.warning(
                    f"Failed to lock flow {flow_id}: HTTP {response.status_code} - {response.text}"
                )
        except Exception as e:
            logger.warning(f"Error locking flow {flow_id}: {e}")

    async def _reset_langflow_flow_locked(self, flow_type: str, flow_id: str):
        if not LANGFLOW_URL:
            raise ValueError("LANGFLOW_URL environment variable is required")

        # Dynamically find the flow file by ID
        flow_path = self._find_flow_file_by_id(flow_id)
        if not flow_path:
            raise FileNotFoundError(f"Flow file not found for flow ID: {flow_id}")

        # Load flow JSON file
        try:
            with open(flow_path) as f:
                flow_data = json.load(f)
            logger.info(
                f"Successfully loaded flow data for {flow_type} from {os.path.basename(flow_path)}"
            )
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in flow file {flow_path}: {e}") from e
        except FileNotFoundError as e:
            raise ValueError(f"Flow file not found: {flow_path}") from e

        await self._unlock_flow(flow_id)

        # Make PATCH request to Langflow API to update the flow using shared client
        try:
            response = await clients.langflow_request(
                "PATCH", f"/api/v1/flows/{flow_id}", json=flow_data
            )

            if response.status_code == 200:
                await self._lock_flow(flow_id)
                logger.info(
                    f"Successfully reset {flow_type} flow",
                    flow_id=flow_id,
                    flow_file=os.path.basename(flow_path),
                )
                return {
                    "success": True,
                    "message": f"Successfully reset {flow_type} flow",
                    "flow_id": flow_id,
                    "flow_type": flow_type,
                }
            else:
                error_text = response.text
                logger.error(
                    f"Failed to reset {flow_type} flow",
                    status_code=response.status_code,
                    error=error_text,
                )
                return {
                    "success": False,
                    "error": f"Failed to reset flow: HTTP {response.status_code} - {error_text}",
                }
        except Exception as e:
            logger.error(f"Error while resetting {flow_type} flow", error=str(e))
            return {"success": False, "error": f"Error: {str(e)}"}

    def _find_node_in_flow(self, flow_data, node_id=None, display_name=None):
        """
        Helper function to find a node in flow data by ID or display name.
        Returns tuple of (node, node_index) or (None, None) if not found.
        """
        nodes = flow_data.get("data", {}).get("nodes", [])

        for i, node in enumerate(nodes):
            node_data = node.get("data", {})
            node_template = node_data.get("node", {})

            # Check by ID if provided
            if node_id and node_data.get("id") == node_id:
                return node, i

            # Check by display_name if provided
            if display_name and node_template.get("display_name") == display_name:
                return node, i

        return None, None

    def _find_nodes_in_flow(self, flow_data, display_name=None):
        """Find all nodes in flow data by display name."""
        nodes = flow_data.get("data", {}).get("nodes", [])
        found = []
        for i, node in enumerate(nodes):
            node_data = node.get("data", {})
            node_template = node_data.get("node", {})
            if display_name and node_template.get("display_name") == display_name:
                found.append((node, i))
        return found

    def _get_node_provider(self, node):
        """Get the provider name currently set in a node."""
        template = node.get("data", {}).get("node", {}).get("template", {})
        model_val = template.get("model", {}).get("value")
        if isinstance(model_val, list) and len(model_val) > 0:
            return model_val[0].get("provider")
        return None

    def _get_provider_name_display(self, provider: str):
        if provider == "watsonx":
            return "IBM WatsonX"
        if provider == "ollama":
            return "Ollama"
        if provider == "anthropic":
            return "Anthropic"
        return "OpenAI"

    async def _update_flow_field(
        self,
        flow_id: str,
        field_name: str,
        field_value: Any,
        node_display_name: str | None = None,
        expected_value: Any = _UNSET,
    ):
        """
        Generic helper function to update any field in any Langflow component.

        Args:
            flow_id: The ID of the flow to update
            field_name: The name of the field to update (e.g., 'model_name', 'system_message', 'docling_serve_opts')
            field_value: The new value to set
            node_display_name: The display name to search for (optional)
            expected_value: Optional expected current value to revalidate before writing
        """
        if not flow_id:
            raise ValueError("flow_id is required")

        # Get the current flow data from Langflow
        response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")

        if response.status_code != 200:
            raise Exception(f"Failed to get flow: HTTP {response.status_code} - {response.text}")

        flow_data = response.json()

        # Find the target component by display name first, then by ID as fallback
        target_node, target_node_index = None, None
        if node_display_name:
            target_node, target_node_index = self._find_node_in_flow(
                flow_data, display_name=node_display_name
            )

        if target_node is None:
            identifier = node_display_name
            raise Exception(f"Component '{identifier}' not found in flow {flow_id}")

        # Update the field value directly in the existing node
        template = target_node.get("data", {}).get("node", {}).get("template", {})
        if template.get(field_name):
            if expected_value is not _UNSET:
                current_value = template[field_name].get("value")
                if current_value != expected_value:
                    raise Exception(
                        f"Field '{field_name}' in '{node_display_name}' component "
                        "has changed from expected value"
                    )
            flow_data["data"]["nodes"][target_node_index]["data"]["node"]["template"][field_name][
                "value"
            ] = field_value
            if (
                "options"
                in flow_data["data"]["nodes"][target_node_index]["data"]["node"]["template"][
                    field_name
                ]
                and field_value
                not in flow_data["data"]["nodes"][target_node_index]["data"]["node"]["template"][
                    field_name
                ]["options"]
            ):
                flow_data["data"]["nodes"][target_node_index]["data"]["node"]["template"][
                    field_name
                ]["options"].append(field_value)
        else:
            identifier = node_display_name
            raise Exception(f"{field_name} field not found in {identifier} component")

        await self._unlock_flow(flow_id)

        # Update the flow via PATCH request
        patch_response = await clients.langflow_request(
            "PATCH", f"/api/v1/flows/{flow_id}", json=flow_data
        )

        if patch_response.status_code != 200:
            raise Exception(
                f"Failed to update flow: HTTP {patch_response.status_code} - {patch_response.text}"
            )
        await self._lock_flow(flow_id)

    async def update_chat_flow_model(self, model_name: str, provider: str):
        """Helper function to update the model in the chat flow"""
        if not LANGFLOW_CHAT_FLOW_ID:
            raise ValueError("LANGFLOW_CHAT_FLOW_ID is not configured")

        # Determine target component IDs based on provider
        target_llm_id = self._get_provider_component_ids(provider)[1]

        await self._update_flow_field(
            LANGFLOW_CHAT_FLOW_ID, "model_name", model_name, node_display_name=target_llm_id
        )

    async def get_chat_flow_system_prompt(self) -> str | None:
        """Return the current system prompt from the chat flow."""
        if not LANGFLOW_CHAT_FLOW_ID:
            raise ValueError("LANGFLOW_CHAT_FLOW_ID is not configured")

        response = await clients.langflow_request("GET", f"/api/v1/flows/{LANGFLOW_CHAT_FLOW_ID}")
        if response.status_code != 200:
            raise Exception(
                f"Failed to get flow {LANGFLOW_CHAT_FLOW_ID}: "
                f"HTTP {response.status_code} - {response.text}"
            )

        flow_data = response.json()
        target_node, _ = self._find_node_in_flow(
            flow_data, display_name=AGENT_COMPONENT_DISPLAY_NAME
        )
        if target_node is None:
            raise Exception(
                f"Component '{AGENT_COMPONENT_DISPLAY_NAME}' not found in flow "
                f"{LANGFLOW_CHAT_FLOW_ID}"
            )

        return (
            target_node.get("data", {})
            .get("node", {})
            .get("template", {})
            .get("system_prompt", {})
            .get("value")
        )

    @staticmethod
    def _runtime_model_identity(value: Any) -> tuple[str, str]:
        """Return the provider/model identity selected by a Langflow ModelInput."""

        selected = value[0] if isinstance(value, list) and value else value
        if isinstance(selected, dict):
            provider = str(selected.get("provider") or "").strip().casefold()
            model = str(selected.get("name") or selected.get("model") or "").strip()
        else:
            provider = ""
            model = str(selected or "").strip()
        provider_aliases = {
            "ibm watsonx": "watsonx",
            "ibm watsonx.ai": "watsonx",
        }
        return provider_aliases.get(provider, provider), model

    async def get_chat_flow_behavior(self) -> dict[str, Any]:
        """Observe behavior-bearing fields from the persisted managed chat flow."""

        if not LANGFLOW_CHAT_FLOW_ID:
            raise ValueError("LANGFLOW_CHAT_FLOW_ID is not configured")
        response = await clients.langflow_request("GET", f"/api/v1/flows/{LANGFLOW_CHAT_FLOW_ID}")
        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to get flow {LANGFLOW_CHAT_FLOW_ID}: HTTP {response.status_code}"
            )
        flow_data = response.json()
        target_node, _ = self._find_node_in_flow(
            flow_data, display_name=AGENT_COMPONENT_DISPLAY_NAME
        )
        if target_node is None:
            raise RuntimeError(
                f"Component '{AGENT_COMPONENT_DISPLAY_NAME}' not found in flow "
                f"{LANGFLOW_CHAT_FLOW_ID}"
            )
        template = target_node.get("data", {}).get("node", {}).get("template", {})
        prompt = template.get("system_prompt", {}).get("value")
        provider, model = self._runtime_model_identity(template.get("model", {}).get("value"))
        code = template.get("code", {}).get("value")
        guard_version = None
        if isinstance(code, str):
            match = re.search(r"^_RETRIEVAL_GUARD_VERSION\s*=\s*(\d+)\s*$", code, re.MULTILINE)
            guard_version = int(match.group(1)) if match else None
        return {
            "flow_id": LANGFLOW_CHAT_FLOW_ID,
            "flow_version": flow_data.get("data", {}).get("openrag_retrieval_version"),
            "locked": flow_data.get("locked"),
            "prompt": prompt if isinstance(prompt, str) else "",
            "agent_provider": provider,
            "agent_model": model,
            "agent_guard_version": guard_version,
            "agent_code_sha256": (
                hashlib.sha256(code.encode("utf-8")).hexdigest() if isinstance(code, str) else None
            ),
            "langgraph_max_iterations": template.get("max_iterations", {}).get("value"),
        }

    async def sync_managed_chat_behavior(
        self, *, prompt: str, provider: str, model: str
    ) -> dict[str, Any]:
        """Apply OpenRAG runtime behavior to the managed flow and verify it."""

        result = await self.change_langflow_model_value(
            provider,
            llm_model=model,
            force_llm_update=True,
            flow_configs=[{"name": "retrieval", "flow_id": LANGFLOW_CHAT_FLOW_ID}],
        )
        if not result.get("success"):
            raise RuntimeError(f"Managed agent model synchronization failed: {result}")

        observed = await self.get_chat_flow_behavior()
        if observed["prompt"] != prompt:
            await self.update_chat_flow_system_prompt(prompt, expected_prompt=observed["prompt"])
            observed = await self.get_chat_flow_behavior()

        expected = {
            "prompt": prompt,
            "agent_provider": str(provider or "").strip().casefold(),
            "agent_model": str(model or "").strip(),
        }
        mismatches = {
            field: {"configured": value, "effective": observed.get(field)}
            for field, value in expected.items()
            if observed.get(field) != value
        }
        if mismatches:
            raise RuntimeError(f"Managed runtime behavior mismatch: {mismatches}")
        return observed

    async def update_chat_flow_system_prompt(
        self, system_prompt: str, expected_prompt: Any = _UNSET
    ):
        """Helper function to update the system prompt in the chat flow"""
        if not LANGFLOW_CHAT_FLOW_ID:
            raise ValueError("LANGFLOW_CHAT_FLOW_ID is not configured")

        await self._update_flow_field(
            LANGFLOW_CHAT_FLOW_ID,
            "system_prompt",
            system_prompt,
            node_display_name=AGENT_COMPONENT_DISPLAY_NAME,
            expected_value=expected_prompt,
        )

    async def update_flow_docling_preset(self, preset: str, preset_config: dict):
        """Helper function to update docling preset in the ingest flow"""
        if not LANGFLOW_INGEST_FLOW_ID:
            raise ValueError("LANGFLOW_INGEST_FLOW_ID is not configured")

        from config.settings import DOCLING_COMPONENT_DISPLAY_NAME

        await self._update_flow_field(
            LANGFLOW_INGEST_FLOW_ID,
            "docling_serve_opts",
            preset_config,
            node_display_name=DOCLING_COMPONENT_DISPLAY_NAME,
        )

    async def update_ingest_flow_chunk_size(self, chunk_size: int):
        """Helper function to update chunk size in the ingest flow"""
        if not LANGFLOW_INGEST_FLOW_ID:
            raise ValueError("LANGFLOW_INGEST_FLOW_ID is not configured")
        await self._update_flow_field(
            LANGFLOW_INGEST_FLOW_ID,
            "chunk_size",
            chunk_size,
            node_display_name="Split Text",
        )

    async def update_ingest_flow_chunk_overlap(self, chunk_overlap: int):
        """Helper function to update chunk overlap in the ingest flow"""
        if not LANGFLOW_INGEST_FLOW_ID:
            raise ValueError("LANGFLOW_INGEST_FLOW_ID is not configured")
        await self._update_flow_field(
            LANGFLOW_INGEST_FLOW_ID,
            "chunk_overlap",
            chunk_overlap,
            node_display_name="Split Text",
        )

    async def update_ingest_flow_embedding_model(self, embedding_model: str, provider: str):
        """Helper function to update embedding model in the ingest flow"""
        if not LANGFLOW_INGEST_FLOW_ID:
            raise ValueError("LANGFLOW_INGEST_FLOW_ID is not configured")

        # Determine target component IDs based on provider
        target_embedding_id = self._get_provider_component_ids(provider)[0]

        await self._update_flow_field(
            LANGFLOW_INGEST_FLOW_ID, "model", embedding_model, node_display_name=target_embedding_id
        )

    def _replace_node_in_flow(self, flow_data, old_display_name, new_node):
        """Replace a node in the flow data"""
        nodes = flow_data.get("data", {}).get("nodes", [])
        for i, node in enumerate(nodes):
            if node.get("data", {}).get("node", {}).get("display_name") == old_display_name:
                nodes[i] = new_node
                return True
        return False

    def _normalize_flow_structure(self, flow_data: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize flow structure for comparison by removing dynamic fields.
        Keeps structural elements: nodes (types, display names, template definitions), edges (connections).
        Removes: IDs, timestamps, positions, dynamic runtime field values, and dynamic options lists.
        """
        normalized: dict[str, Any] = {"data": {"nodes": [], "edges": []}}

        # Normalize nodes - keep structural info including template definitions
        nodes = flow_data.get("data", {}).get("nodes", [])
        for node in nodes:
            node_data = node.get("data", {})
            node_template = node_data.get("node", {})

            template = node_template.get("template", {})
            normalized_template: dict[str, Any] = {}
            if isinstance(template, dict):
                for field_name, field_def in template.items():
                    if isinstance(field_def, dict):
                        normalized_template[field_name] = {
                            "type": field_def.get("type"),
                            "display_name": field_def.get("display_name"),
                            "required": field_def.get("required"),
                            "input_types": field_def.get("input_types"),
                        }
                    else:
                        normalized_template[field_name] = {"value": str(field_def)}

            normalized_node = {
                "id": node.get("id"),  # Keep ID for edge matching
                "type": node.get("type"),
                "data": {
                    "node": {
                        "display_name": node_template.get("display_name"),
                        "name": node_template.get("name"),
                        "base_classes": node_template.get("base_classes", []),
                        "template": normalized_template,
                    }
                },
            }
            normalized["data"]["nodes"].append(normalized_node)

        # Normalize edges - keep only connections
        edges = flow_data.get("data", {}).get("edges", [])
        for edge in edges:
            normalized_edge = {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "sourceHandle": edge.get("sourceHandle"),
                "targetHandle": edge.get("targetHandle"),
            }
            normalized["data"]["edges"].append(normalized_edge)

        return normalized

    async def _check_flow_update(
        self, flow_type: str, flow_id: str, flow_data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """
        Check if a flow has a newer version on disk compared to Langflow.
        Returns flow update info dict if an update is available, None otherwise.
        """
        if not flow_id:
            return None
        flow_path = self._find_flow_file_by_id(flow_id)
        if not flow_path or not os.path.exists(flow_path):
            return None
        local_mtime = os.path.getmtime(flow_path)

        if flow_data is None:
            try:
                response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                if response.status_code != 200:
                    return None
                flow_data = response.json()
            except Exception as e:
                logger.error(f"Failed to fetch flow {flow_id} for update check: {e}")
                return None

        langflow_updated_at_str = flow_data.get("updated_at")
        langflow_mtime: float = 0.0
        if langflow_updated_at_str:
            if langflow_updated_at_str.endswith("Z"):
                langflow_updated_at_str = langflow_updated_at_str[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(langflow_updated_at_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                langflow_mtime = dt.timestamp()
            except ValueError:
                pass

        if local_mtime > langflow_mtime + 1.0:
            is_locked = flow_data.get("locked", False)
            return {
                "flow_type": flow_type,
                "flow_id": flow_id,
                "is_custom": not is_locked,
                # This is the provenance of the files that the update action
                # will apply, not the version of the Langflow container.
                "source": get_flows_source_metadata(),
            }
        return None

    @staticmethod
    def is_explicit_custom_retrieval_flow() -> bool:
        """Return whether the configured chat flow opts out of system ownership.

        This is intentionally configuration-only: startup needs this boundary
        before it performs any create or settings-reapplication work that could
        otherwise mutate the configured chat flow.
        """
        return bool(LANGFLOW_CHAT_FLOW_ID and LANGFLOW_CHAT_FLOW_ID != _LEGACY_SYSTEM_FLOW_ID)

    async def ensure_flows_exist(
        self,
        *,
        reapply_settings: bool = True,
        ensure_retrieval_flow: bool = True,
    ) -> set[str]:
        """
        Ensure all configured flows exist in Langflow.

        Creates flows from their JSON files if they are not already present in
        the Langflow database.  This is intentionally create-only: it never
        patches or overwrites an existing flow, preserving any edits the user
        has made in the Langflow UI.

        Returns the set of flow type names that were actually created.

        ``reapply_settings`` and ``ensure_retrieval_flow`` are disabled only
        when startup is serving an explicitly configured custom retrieval
        flow. Creation of the other system flows remains safe and create-only,
        while neither an implicit creation nor the global reapply operation may
        touch the configured custom chat flow.
        """
        flow_configs = [
            ("nudges", NUDGES_FLOW_ID),
            ("ingest", LANGFLOW_INGEST_FLOW_ID),
            ("url_ingest", LANGFLOW_URL_INGEST_FLOW_ID),
        ]
        if ensure_retrieval_flow:
            flow_configs.insert(1, ("retrieval", LANGFLOW_CHAT_FLOW_ID))
        created_flow_types: set[str] = set()

        async def ensure_single_flow_exists(flow_type, flow_id):
            if not flow_id:
                return None
            try:
                response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                if response.status_code == 200:
                    logger.info(
                        f"Flow {flow_type} (ID: {flow_id}) already exists, skipping creation"
                    )
                    return None

                if response.status_code != 404:
                    logger.warning(
                        f"Unexpected status checking {flow_type} flow (ID: {flow_id}): "
                        f"HTTP {response.status_code} — skipping creation to avoid overwriting existing data"
                    )
                    return None

                flow_path = self._find_flow_file_by_id(flow_id)
                if not flow_path:
                    logger.warning(
                        f"No flow file found for {flow_type} (ID: {flow_id}), cannot create"
                    )
                    return None

                with open(flow_path) as f:
                    flow_data = json.load(f)

                response = await clients.langflow_request(
                    "PUT", f"/api/v1/flows/{flow_id}", json=flow_data
                )
                if response.status_code in (200, 201):
                    logger.info(
                        f"Created {flow_type} flow (ID: {flow_id}) from {os.path.basename(flow_path)}"
                    )
                    return flow_type
                else:
                    logger.warning(
                        f"Failed to create {flow_type} flow (ID: {flow_id}): "
                        f"HTTP {response.status_code} — {response.text}"
                    )
                    return None
            except Exception as e:
                logger.error(f"Error ensuring {flow_type} flow (ID: {flow_id}) exists: {e}")
                return None

        tasks = [ensure_single_flow_exists(ft, fi) for ft, fi in flow_configs]
        results = await asyncio.gather(*tasks)
        created_flow_types = {r for r in results if r is not None}

        if created_flow_types and reapply_settings:
            try:
                config = get_openrag_config()
                if config.edited:
                    logger.info(
                        f"Newly created flows detected ({', '.join(created_flow_types)}), reapplying settings."
                    )
                    from api.settings import reapply_all_settings

                    await reapply_all_settings()
                    logger.info("Successfully reapplied settings for newly created flows")
            except Exception as e:
                logger.error(f"Failed to reapply settings for newly created flows: {e}")

        return created_flow_types

    async def validate_explicit_custom_retrieval_flow(self) -> dict[str, Any]:
        """Read-only validation for an explicitly configured custom chat flow."""
        flow_id = LANGFLOW_CHAT_FLOW_ID
        if not self.is_explicit_custom_retrieval_flow():
            raise RuntimeError("Configured retrieval flow is not an explicit custom flow")

        try:
            response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
        except Exception as exc:
            logger.error(
                "Custom retrieval flow readiness check failed", flow_id=flow_id, error=str(exc)
            )
            return {
                "status": "system_migration_failed",
                "reason": "custom_flow_fetch_failed",
                "error": str(exc),
                "flow_id": flow_id,
            }
        if response.status_code == 404:
            return {
                "status": "system_migration_failed",
                "reason": "custom_flow_missing",
                "flow_id": flow_id,
            }
        if response.status_code != 200:
            return {
                "status": "system_migration_failed",
                "reason": "custom_flow_fetch_failed",
                "error": f"HTTP {response.status_code}",
                "flow_id": flow_id,
            }

        logger.warning(
            "Configured retrieval flow is custom and will not be modified automatically",
            flow_id=flow_id,
        )
        return {
            "status": "custom_preserved",
            "reason": "custom_flow_id",
            "flow_id": flow_id,
        }

    @staticmethod
    def _node_component_type(node: dict[str, Any]) -> str:
        data = node.get("data", {}) if isinstance(node, dict) else {}
        component = data.get("node", {}) if isinstance(data, dict) else {}
        return str(
            component.get("namespaced_id") or data.get("type") or component.get("name") or ""
        )

    def _load_retrieval_v2_template(self) -> dict[str, Any]:
        flow_path = self._find_flow_file_by_id(LANGFLOW_CHAT_FLOW_ID)
        if not flow_path:
            raise RuntimeError("Bundled retrieval flow template was not found")
        with open(flow_path) as file:
            template = json.load(file)

        # Keep the protected graph's Agent prompt sourced from the same
        # versioned default as onboarding and settings.  The raw Langflow JSON
        # remains the exact first Retrieval v2 baseline so its fingerprint can
        # still be recognized during an in-place upgrade.
        from config.config_manager import DEFAULT_SYSTEM_PROMPT

        agent_nodes = [
            node
            for node in template.get("data", {}).get("nodes", [])
            if node.get("data", {}).get("node", {}).get("display_name") == "Agent"
        ]
        if len(agent_nodes) != 1:
            raise RuntimeError("Bundled retrieval flow must contain exactly one Agent node")
        agent_nodes[0]["data"]["node"]["template"]["system_prompt"]["value"] = DEFAULT_SYSTEM_PROMPT
        return template

    @staticmethod
    def _graph_fingerprint(flow_data: dict[str, Any]) -> str | None:
        """Return the versioned canonical fingerprint of a persisted graph."""
        graph = flow_data.get("data") if isinstance(flow_data, dict) else None
        if not isinstance(graph, dict):
            return None
        canonical = json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _runtime_normalized_graph_fingerprint(flow_data: dict[str, Any]) -> str | None:
        """Fingerprint a graph while ignoring only settings-owned provider values."""
        graph = flow_data.get("data") if isinstance(flow_data, dict) else None
        if not isinstance(graph, dict):
            return None
        normalized = copy.deepcopy(graph)
        nodes = normalized.get("nodes", [])
        if not isinstance(nodes, list):
            return None
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_data = node.get("data", {})
            if not isinstance(node_data, dict):
                continue
            component = node_data.get("node", {})
            if not isinstance(component, dict):
                continue
            if component.get("display_name") not in _RUNTIME_MANAGED_RETRIEVAL_COMPONENTS:
                continue
            template = component.get("template", {})
            if not isinstance(template, dict):
                continue
            for field in _RUNTIME_MANAGED_RETRIEVAL_FIELDS.intersection(template):
                template[field] = {"runtime_managed": field}
        canonical = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _managed_behavior_normalized_graph_fingerprint(
        flow_data: dict[str, Any],
    ) -> str | None:
        """Fingerprint graph ownership while excluding managed behavior values.

        Historical migration fingerprints remain intentionally unchanged. This
        normalization is only for the already-current managed flow so a prompt
        persisted through OpenRAG settings is not mistaken for graph
        customization on the next restart.
        """

        graph = flow_data.get("data") if isinstance(flow_data, dict) else None
        if not isinstance(graph, dict):
            return None
        normalized = copy.deepcopy(graph)
        for node in normalized.get("nodes", []):
            component = node.get("data", {}).get("node", {}) if isinstance(node, dict) else {}
            if component.get("display_name") not in _RUNTIME_MANAGED_RETRIEVAL_COMPONENTS:
                continue
            template = component.get("template", {})
            if not isinstance(template, dict):
                continue
            fields = set(_RUNTIME_MANAGED_RETRIEVAL_FIELDS)
            if component.get("display_name") == AGENT_COMPONENT_DISPLAY_NAME:
                fields.add("system_prompt")
            for field in fields.intersection(template):
                template[field] = {"runtime_managed": field}
        canonical = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _preserve_runtime_managed_retrieval_fields(
        source_graph: dict[str, Any], target_graph: dict[str, Any]
    ) -> None:
        """Carry settings-owned provider fields into a verified replacement graph."""
        source_nodes = source_graph.get("nodes", [])
        target_nodes = target_graph.get("nodes", [])
        if not isinstance(source_nodes, list) or not isinstance(target_nodes, list):
            return
        source_by_id = {
            node.get("id"): node
            for node in source_nodes
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        }
        for target_node in target_nodes:
            if not isinstance(target_node, dict):
                continue
            source_node = source_by_id.get(target_node.get("id"))
            if not isinstance(source_node, dict):
                continue
            source_component = source_node.get("data", {}).get("node", {})
            target_component = target_node.get("data", {}).get("node", {})
            if not isinstance(source_component, dict) or not isinstance(target_component, dict):
                continue
            display_name = target_component.get("display_name")
            if (
                display_name not in _RUNTIME_MANAGED_RETRIEVAL_COMPONENTS
                or source_component.get("display_name") != display_name
            ):
                continue
            source_template = source_component.get("template", {})
            target_template = target_component.get("template", {})
            if not isinstance(source_template, dict) or not isinstance(target_template, dict):
                continue
            for field in _RUNTIME_MANAGED_RETRIEVAL_FIELDS.intersection(
                source_template, target_template
            ):
                target_template[field] = copy.deepcopy(source_template[field])

    def _is_known_legacy_retrieval_flow(self, flow_data: dict[str, Any]) -> bool:
        """Recognise exactly the immutable 156f3664 system agent graph.

        This is intentionally more restrictive than checking for a legacy node.
        The migration has administrative authority over the persisted flow, so
        a graph changed by an operator must fail closed rather than be silently
        rewritten at application startup.
        """
        if not isinstance(flow_data, dict):
            return False
        if flow_data.get("id") != _LEGACY_SYSTEM_FLOW_ID or not flow_data.get("locked"):
            return False
        if flow_data.get("name") != "OpenRAG OpenSearch Agent Flow":
            return False
        if flow_data.get("description") != "OpenRAG OpenSearch Agent":
            return False
        return self._graph_fingerprint(flow_data) == _LEGACY_RETRIEVAL_GRAPH_SHA256

    def _is_known_migrated_retrieval_flow(self, flow_data: dict[str, Any]) -> bool:
        """Verify the exact bundled Retrieval v2 graph, not just one new node."""
        if not isinstance(flow_data, dict) or flow_data.get("id") != _LEGACY_SYSTEM_FLOW_ID:
            return False
        graph = flow_data.get("data")
        if not isinstance(graph, dict):
            return False
        if graph.get("openrag_retrieval_version") != _RETRIEVAL_FLOW_MIGRATION_VERSION:
            return False
        template = self._load_retrieval_v2_template()
        expected = copy.deepcopy(template)
        expected_graph = expected.get("data", {})
        expected_graph["openrag_retrieval_version"] = _RETRIEVAL_FLOW_MIGRATION_VERSION
        return (
            flow_data.get("name") == expected.get("name")
            and flow_data.get("description") == expected.get("description")
            and self._managed_behavior_normalized_graph_fingerprint(flow_data)
            == self._managed_behavior_normalized_graph_fingerprint(expected)
        )

    def _is_known_unversioned_retrieval_v2_flow(self, flow_data: dict[str, Any]) -> bool:
        """Recognize the exact bundled Retrieval v2 graph before its version marker.

        The GitOps flow bootstrap may synchronize the bundled graph before the
        backend starts.  Its source JSON deliberately does not persist the
        runtime migration marker, so this is a safe, recoverable intermediate
        state rather than an operator customization.
        """
        if (
            not isinstance(flow_data, dict)
            or flow_data.get("id") != _LEGACY_SYSTEM_FLOW_ID
            or not flow_data.get("locked")
        ):
            return False
        template = self._load_retrieval_v2_template()
        return (
            flow_data.get("name") == template.get("name")
            and flow_data.get("description") == template.get("description")
            and self._graph_fingerprint(flow_data) == self._graph_fingerprint(template)
        )

    def _is_known_previous_retrieval_v2_flow(self, flow_data: dict[str, Any]) -> bool:
        """Recognize only the exact first Retrieval v2 prompt revision."""
        if (
            not isinstance(flow_data, dict)
            or flow_data.get("id") != _LEGACY_SYSTEM_FLOW_ID
            or not flow_data.get("locked")
        ):
            return False
        graph = flow_data.get("data")
        if not isinstance(graph, dict):
            return False
        marker = graph.get("openrag_retrieval_version")
        expected = (
            {
                _PREVIOUS_RETRIEVAL_GRAPH_SHA256,
                _PREVIOUS_ROLE_EVIDENCE_GRAPH_SHA256,
                _PREVIOUS_EVIDENCE_FIRST_GRAPH_SHA256,
                _PREVIOUS_DOCUMENTALIST_RETRIEVAL_GRAPH_SHA256,
            }
            if marker is None
            else {
                _PREVIOUS_VERSIONED_RETRIEVAL_GRAPH_SHA256,
                _PREVIOUS_VERSIONED_ROLE_EVIDENCE_GRAPH_SHA256,
                _GENERICIZED_VERSIONED_ROLE_EVIDENCE_GRAPH_SHA256,
            }
            if marker == 3
            else {_PREVIOUS_VERSIONED_EVIDENCE_FIRST_GRAPH_SHA256}
            if marker == 4
            else {_PREVIOUS_VERSIONED_FOCUSED_RETRIEVAL_GRAPH_SHA256}
            if marker == 5
            else {_PREVIOUS_VERSIONED_DOCUMENTALIST_RETRIEVAL_GRAPH_SHA256}
            if marker == 6
            else {_PREVIOUS_VERSIONED_SCOPELESS_RETRIEVAL_GRAPH_SHA256}
            if marker == 7
            else {_PREVIOUS_VERSIONED_SCOPE_ARTIFACT_GRAPH_SHA256}
            if marker == 8
            else {_PREVIOUS_VERSIONED_RUNTIME_SAFE_GRAPH_SHA256}
            if marker == 9
            else {_PREVIOUS_VERSIONED_SCOPE_POLICY_GRAPH_SHA256}
            if marker == 10
            else {_PREVIOUS_VERSIONED_AGENT_GUARD_GRAPH_SHA256}
            if marker == 11
            else {_PREVIOUS_VERSIONED_MULTI_QUERY_GRAPH_SHA256}
            if marker == 12
            else set()
        )
        exact_match = self._graph_fingerprint(flow_data) in expected
        if marker not in {6, 7, 8, 9, 10, 11, 12}:
            return exact_match
        expected_runtime = (
            _PREVIOUS_VERSIONED_DOCUMENTALIST_RUNTIME_GRAPH_SHA256
            if marker == 6
            else _PREVIOUS_VERSIONED_SCOPELESS_RUNTIME_GRAPH_SHA256
            if marker == 7
            else _PREVIOUS_VERSIONED_SCOPE_ARTIFACT_RUNTIME_GRAPH_SHA256
            if marker == 8
            else _PREVIOUS_VERSIONED_RUNTIME_SAFE_RUNTIME_GRAPH_SHA256
            if marker == 9
            else _PREVIOUS_VERSIONED_SCOPE_POLICY_RUNTIME_GRAPH_SHA256
            if marker == 10
            else _PREVIOUS_VERSIONED_AGENT_GUARD_RUNTIME_GRAPH_SHA256
            if marker == 11
            else _PREVIOUS_VERSIONED_MULTI_QUERY_RUNTIME_GRAPH_SHA256
        )
        return (
            exact_match or self._runtime_normalized_graph_fingerprint(flow_data) == expected_runtime
        )

    def _migrate_known_legacy_retrieval_flow(
        self, flow_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Replace only the known legacy retrieval tool in a locked default flow.

        Returning ``None`` means the caller must leave the persisted flow
        untouched.  This is the safety boundary that prevents a startup from
        overwriting an administrator's Langflow changes.
        """
        graph = flow_data.get("data", {}) if isinstance(flow_data, dict) else {}
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        if not isinstance(nodes, list):
            return None
        # The fingerprint checks every node, edge and configured value from the
        # lifecycle baseline.  Do not weaken this to "locked + legacy tool":
        # administrators legitimately lock customised flows too.
        known_legacy = self._is_known_legacy_retrieval_flow(flow_data)
        known_unversioned_v2 = self._is_known_unversioned_retrieval_v2_flow(flow_data)
        known_previous_v2 = self._is_known_previous_retrieval_v2_flow(flow_data)
        if not known_legacy and not known_unversioned_v2 and not known_previous_v2:
            return None

        legacy_nodes = [
            node for node in nodes if _LEGACY_RETRIEVAL_COMPONENT in self._node_component_type(node)
        ]
        if known_legacy and len(legacy_nodes) != 1:
            return None

        template = self._load_retrieval_v2_template()
        template_graph = template.get("data", {})
        template_nodes = template_graph.get("nodes", [])
        new_node = next(
            (
                node
                for node in template_nodes
                if _BACKEND_RETRIEVAL_COMPONENT in self._node_component_type(node)
            ),
            None,
        )
        if not isinstance(new_node, dict):
            raise RuntimeError("Bundled Retrieval v2 node is missing from the flow template")
        new_node_id = new_node.get("id")
        new_edge = next(
            (edge for edge in template_graph.get("edges", []) if edge.get("source") == new_node_id),
            None,
        )
        if not isinstance(new_edge, dict):
            raise RuntimeError("Bundled Retrieval v2 tool edge is missing from the flow template")

        # The source graph matched the complete historical fingerprint, so the
        # bundled flow is the authoritative replacement.  Reusing the complete
        # ``data`` graph (rather than splicing one node) verifies all tool
        # wiring and avoids retaining stale legacy inputs such as embeddings.
        migrated = copy.deepcopy(flow_data)
        migrated_graph = copy.deepcopy(template_graph)
        # A verified system flow may still contain model choices and global
        # credential/endpoint references written by OpenRAG settings. Preserve
        # them exactly; recognizing those fields must never imply resetting
        # them to the bundled template defaults.
        self._preserve_runtime_managed_retrieval_fields(graph, migrated_graph)
        migrated["data"] = migrated_graph
        # Stored with the graph so repeated startups can identify the installed
        # known version without relying on timestamps or an in-memory cache.
        migrated_graph["openrag_retrieval_version"] = _RETRIEVAL_FLOW_MIGRATION_VERSION
        # Langflow rejects PUT updates to a locked flow.  The caller uses its
        # native PATCH API to open this narrow maintenance window and restores
        # the system-flow lock in a finally block after verification.
        migrated["locked"] = False
        return migrated

    async def _patch_flow_lock(self, flow_id: str, *, locked: bool) -> None:
        """Use Langflow's supported PATCH primitive for the lock transition."""
        response = await clients.langflow_request(
            "PATCH", f"/api/v1/flows/{flow_id}", json={"locked": locked}
        )
        if response.status_code != 200:
            raise RuntimeError(f"Langflow lock update failed: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("locked") is not locked:
            raise RuntimeError("Langflow lock update did not return the requested state")

    async def _get_flow_lock_state(self, flow_id: str) -> dict[str, Any]:
        """Return the lock state after a transition, without assuming success."""
        try:
            response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
        except Exception as exc:
            return {"known_state": "unknown", "locked": None, "error": str(exc)}
        if response.status_code == 404:
            return {"known_state": "missing", "locked": None}
        if response.status_code != 200:
            return {
                "known_state": "unknown",
                "locked": None,
                "error": f"HTTP {response.status_code}",
            }
        try:
            payload = response.json()
        except Exception as exc:
            return {"known_state": "unknown", "locked": None, "error": str(exc)}
        if not isinstance(payload, dict):
            return {"known_state": "unknown", "locked": None, "error": "invalid flow payload"}
        locked = payload.get("locked")
        if locked is True:
            return {"known_state": "locked", "locked": True}
        if locked is False:
            return {"known_state": "unlocked", "locked": False}
        return {"known_state": "unknown", "locked": None, "error": "missing lock state"}

    async def _restore_system_flow_lock(self, flow_id: str) -> tuple[str | None, dict[str, Any]]:
        """Re-lock and verify a system flow after any uncertain transition."""
        lock_error = None
        try:
            await self._patch_flow_lock(flow_id, locked=True)
        except Exception as exc:
            lock_error = str(exc)

        state = await self._get_flow_lock_state(flow_id)
        if state["locked"] is True:
            return None, state
        if lock_error is None:
            lock_error = state.get("error", "flow was not confirmed locked")
        return lock_error, state

    async def migrate_persisted_retrieval_flow(self) -> dict[str, Any]:
        """Safely upgrade the known lifecycle agent flow to Retrieval v2.

        A backup is mandatory and a non-recognized graph is never changed. A
        configured flow ID other than the protected system-flow ID is an
        explicit custom-flow opt-out. Every failure for the protected ID is a
        system migration failure; startup must not treat it as a harmless skip.
        """
        flow_id = LANGFLOW_CHAT_FLOW_ID
        if not flow_id:
            return {
                "status": "system_migration_failed",
                "reason": "flow_id_not_configured",
            }

        flow_lock = await self._get_flow_lock(flow_id)
        async with flow_lock:
            try:
                response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
            except Exception as exc:
                logger.error("Retrieval flow migration check failed", error=str(exc))
                return {
                    "status": "system_migration_failed",
                    "reason": "fetch_failed",
                    "error": str(exc),
                    "flow_id": flow_id,
                }
            if response.status_code == 404:
                return {
                    "status": "system_migration_failed",
                    "reason": "flow_missing",
                    "flow_id": flow_id,
                }
            if response.status_code != 200:
                return {
                    "status": "system_migration_failed",
                    "reason": "fetch_failed",
                    "error": f"HTTP {response.status_code}",
                    "flow_id": flow_id,
                }

            current = response.json()
            if flow_id != _LEGACY_SYSTEM_FLOW_ID:
                logger.warning(
                    "Configured retrieval flow is custom and will not be migrated automatically",
                    flow_id=flow_id,
                )
                return {
                    "status": "custom_preserved",
                    "reason": "custom_flow_id",
                    "flow_id": flow_id,
                }

            if self._is_known_migrated_retrieval_flow(current) and current.get("locked"):
                return {
                    "status": "already_migrated",
                    "version": _RETRIEVAL_FLOW_MIGRATION_VERSION,
                    "flow_id": flow_id,
                }

            migrated = self._migrate_known_legacy_retrieval_flow(current)
            if migrated is None:
                message = (
                    "System retrieval flow is not the exact known locked lifecycle graph; "
                    "it was not migrated and cannot be served. Use the flow update/rollback procedure."
                )
                logger.error(
                    "System retrieval flow migration requires manual review", flow_id=flow_id
                )
                return {
                    "status": "system_migration_failed",
                    "reason": "system_flow_unverified",
                    "error": message,
                    "flow_id": flow_id,
                }

            backup_path = await self._backup_flow(flow_id, "retrieval", current)
            if backup_path is None:
                message = "Retrieval flow migration aborted because backup creation failed"
                logger.error(message, flow_id=flow_id)
                return {
                    "status": "system_migration_failed",
                    "reason": "backup_failed",
                    "error": message,
                    "flow_id": flow_id,
                }

            lock_transition_attempted = False
            maintenance_window_open = False
            try:
                lock_transition_attempted = True
                await self._patch_flow_lock(flow_id, locked=False)
                maintenance_window_open = True
                update = await clients.langflow_request(
                    "PUT", f"/api/v1/flows/{flow_id}", json=migrated
                )
                if update.status_code not in (200, 201):
                    raise RuntimeError(f"Langflow flow update failed: HTTP {update.status_code}")

                verify = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                if verify.status_code != 200 or not self._is_known_migrated_retrieval_flow(
                    verify.json()
                ):
                    raise RuntimeError(
                        "Langflow persisted flow verification failed after retrieval migration"
                    )

                await self._patch_flow_lock(flow_id, locked=True)
                final_verify = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                if final_verify.status_code != 200 or not (
                    final_verify.json().get("locked")
                    and self._is_known_migrated_retrieval_flow(final_verify.json())
                ):
                    raise RuntimeError(
                        "Langflow flow was not verified locked after retrieval migration"
                    )
                maintenance_window_open = False
            except Exception as exc:
                flow_state = {"known_state": "locked", "locked": True}
                lock_error = None
                if lock_transition_attempted:
                    flow_state = await self._get_flow_lock_state(flow_id)
                    if flow_state["locked"] is not True:
                        lock_error, flow_state = await self._restore_system_flow_lock(flow_id)
                if lock_error is not None or flow_state["locked"] is not True:
                    logger.error(
                        "Retrieval flow migration could not restore the system lock",
                        flow_id=flow_id,
                        error=str(lock_error),
                        flow_state=flow_state,
                    )
                    return {
                        "status": "system_migration_failed",
                        "reason": "lock_restore_failed",
                        "error": str(exc),
                        "lock_error": lock_error or "flow was not confirmed locked",
                        "flow_state": flow_state,
                        "flow_id": flow_id,
                        "version": _RETRIEVAL_FLOW_MIGRATION_VERSION,
                        "backup_path": backup_path,
                    }
                logger.error(
                    "Retrieval flow migration update failed", flow_id=flow_id, error=str(exc)
                )
                return {
                    "status": "system_migration_failed",
                    "reason": (
                        "unlock_failed"
                        if not maintenance_window_open
                        else "update_or_verification_failed"
                    ),
                    "error": str(exc),
                    "flow_state": flow_state,
                    "backup_path": backup_path,
                    "flow_id": flow_id,
                }

            logger.info(
                "Migrated known persisted retrieval flow to Retrieval v2",
                flow_id=flow_id,
                version=_RETRIEVAL_FLOW_MIGRATION_VERSION,
                backup_path=backup_path,
            )
            return {
                "status": "migrated",
                "version": _RETRIEVAL_FLOW_MIGRATION_VERSION,
                "backup_path": backup_path,
                "flow_id": flow_id,
            }

    async def change_langflow_model_value(
        self,
        provider: str,
        embedding_model: str | None = None,
        llm_model: str | None = None,
        force_embedding_update: bool = False,
        force_llm_update: bool = False,
        flow_configs: list | None = None,
    ):
        """
        Change dropdown values for provider-specific components across flows

        Args:
            provider: The provider ("watsonx", "ollama", "openai", "anthropic")
            embedding_model: The embedding model name to set
            llm_model: The LLM model name to set
            force_embedding_update: If True, update embeddings even if model is None
            force_llm_update: If True, update LLM even if model is None
            flow_configs: Optional list of flow configs to update
        """
        if provider not in ["watsonx", "ollama", "openai", "anthropic"]:
            raise ValueError("provider must be 'watsonx', 'ollama', 'openai', or 'anthropic'")

        try:
            # Use provided flow_configs or default to all flows
            if flow_configs is None:
                flow_configs = [
                    {"name": "nudges", "flow_id": NUDGES_FLOW_ID},
                    {"name": "retrieval", "flow_id": LANGFLOW_CHAT_FLOW_ID},
                    {"name": "ingest", "flow_id": LANGFLOW_INGEST_FLOW_ID},
                    {"name": "url_ingest", "flow_id": LANGFLOW_URL_INGEST_FLOW_ID},
                ]

            tasks = []
            for config in flow_configs:
                tasks.append(
                    self._update_provider_components(
                        config,
                        provider,
                        embedding_model=embedding_model,
                        llm_model=llm_model,
                        force_embedding_update=force_embedding_update,
                        force_llm_update=force_llm_update,
                    )
                )

            # Process all flows simultaneously
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

            results: list[Any] = []
            for i, result in enumerate(raw_results):
                if isinstance(result, Exception):
                    flow_name = flow_configs[i]["name"]
                    logger.error(f"Error updating {flow_name} flow: {str(result)}")
                    results.append({"flow": flow_name, "success": False, "error": str(result)})
                else:
                    results.append(result)

            return {
                "success": all(r.get("success", False) for r in results),
                "results": results,
            }
        except Exception as e:
            logger.error(f"Error in change_langflow_model_value: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _update_provider_components(
        self,
        config: dict,
        provider_name: str,
        embedding_model: str | None = None,
        llm_model: str | None = None,
        force_embedding_update: bool = False,
        force_llm_update: bool = False,
    ):
        """Update provider components and their dropdown values in a flow"""
        flow_id = config["flow_id"]
        flow_lock = await self._get_flow_lock(flow_id)
        async with flow_lock:
            return await self._update_provider_components_locked(
                config,
                provider_name,
                embedding_model=embedding_model,
                llm_model=llm_model,
                force_embedding_update=force_embedding_update,
                force_llm_update=force_llm_update,
            )

    async def _update_provider_components_locked(
        self,
        config: dict,
        provider_name: str,
        embedding_model: str | None = None,
        llm_model: str | None = None,
        force_embedding_update: bool = False,
        force_llm_update: bool = False,
    ):
        """Internal helper to update provider components when flow_lock is already held"""
        flow_name = config["name"]
        flow_id = config["flow_id"]
        # Get flow data from Langflow API instead of file
        response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")

        if response.status_code != 200:
            raise Exception(
                f"Failed to get flow from Langflow: HTTP {response.status_code} - {response.text}"
            )

        flow_data = response.json()
        provider = provider_name
        updates_made = []
        node_tasks: list[Any] = []

        async def wrap_node_update(node, p, m, label):
            if await self._update_component_fields(node, p, m):
                return label
            return None

        # Update embedding component
        if not get_openrag_config().knowledge.disable_ingest_with_langflow and (
            embedding_model or force_embedding_update
        ):
            # Get all embedding nodes in the flow
            embedding_nodes = self._find_nodes_in_flow(
                flow_data, display_name=OPENAI_EMBEDDING_COMPONENT_DISPLAY_NAME
            )
            logger.info(
                f"Found {len(embedding_nodes)} embedding nodes in flow {flow_name} with display name '{OPENAI_EMBEDDING_COMPONENT_DISPLAY_NAME}'"
            )

            # Count configured embedding-enabled providers
            config_obj = get_openrag_config()
            configured_providers = []
            if config_obj.providers.openai.configured:
                configured_providers.append("openai")
            if config_obj.providers.watsonx.configured:
                configured_providers.append("watsonx")
            if config_obj.providers.ollama.configured:
                configured_providers.append("ollama")

            # Ensure current provider is in the list for counting purposes if it's being configured
            if provider in ["openai", "watsonx", "ollama"] and provider not in configured_providers:
                configured_providers.append(provider)

            all_possible = ["openai", "watsonx", "ollama"]
            configured_providers = [p for p in all_possible if p in configured_providers]
            provider_count = len(configured_providers)
            logger.info(
                f"Configured embedding providers: {configured_providers} (count: {provider_count})"
            )

            # 1. Check if any node is already this provider - always update those first
            matched_nodes = []
            provider_display = self._get_provider_name_display(provider)
            for node, idx in embedding_nodes:
                if self._get_node_provider(node) == provider_display:
                    matched_nodes.append((node, idx))

            if matched_nodes:
                logger.info(
                    f"Found {len(matched_nodes)} nodes already configured for provider '{provider}'"
                )
                for node, _idx in matched_nodes:
                    node_tasks.append(
                        wrap_node_update(
                            node,
                            provider,
                            embedding_model,
                            f"embedding model: {embedding_model} (updated existing {provider} node)",
                        )
                    )
            else:
                # 2. No existing node matched, use slot-based logic
                try:
                    p_index = configured_providers.index(provider)
                    logger.info(
                        f"Using slot-based logic for provider '{provider}' (p_index: {p_index}, total configured: {provider_count})"
                    )

                    if provider_count == 1:
                        # Single provider mode: update all available nodes (up to 3)
                        logger.info(
                            f"Single provider mode: updating all available embedding nodes (available: {len(embedding_nodes)})"
                        )
                        for i in range(min(3, len(embedding_nodes))):
                            node, idx = embedding_nodes[i]
                            node_tasks.append(
                                wrap_node_update(
                                    node,
                                    provider,
                                    embedding_model,
                                    f"embedding model: {embedding_model} (set node {i + 1})",
                                )
                            )
                    else:
                        # Multiple providers: each gets one slot based on its list index
                        if p_index < len(embedding_nodes):
                            node, idx = embedding_nodes[p_index]
                            logger.info(
                                f"Multiple provider mode: assigning provider '{provider}' to node slot {p_index} (node {p_index + 1})"
                            )
                            node_tasks.append(
                                wrap_node_update(
                                    node,
                                    provider,
                                    embedding_model,
                                    f"embedding model: {embedding_model} (set node {p_index + 1})",
                                )
                            )
                        else:
                            logger.info(
                                f"Provider index {p_index} exceeds available embedding nodes ({len(embedding_nodes)}) - skipping automatic assignment"
                            )
                except ValueError:
                    logger.warning(
                        f"Current provider '{provider}' not found in configured providers list: {configured_providers}"
                    )

        # Update LLM component (if exists in this flow)
        if llm_model or force_llm_update:
            llm_node, _ = self._find_node_in_flow(
                flow_data, display_name=OPENAI_LLM_COMPONENT_DISPLAY_NAME
            )
            if llm_node:
                node_tasks.append(
                    wrap_node_update(llm_node, provider, llm_model, f"llm model: {llm_model}")
                )

            agent_node, _ = self._find_node_in_flow(
                flow_data, display_name=AGENT_COMPONENT_DISPLAY_NAME
            )
            if agent_node:
                node_tasks.append(
                    wrap_node_update(agent_node, provider, llm_model, f"agent model: {llm_model}")
                )

        # Execute all node updates simultaneously
        if node_tasks:
            node_results = await asyncio.gather(*node_tasks)
            updates_made.extend([r for r in node_results if r])

        # If no updates were made, return skip message
        if not updates_made:
            return {
                "flow": flow_name,
                "success": True,
                "message": f"No compatible components found in {flow_name} flow (skipped)",
                "flow_id": flow_id,
            }

        logger.info(f"Updated {', '.join(updates_made)} in {flow_name} flow")

        await self._unlock_flow(flow_id)

        # PATCH the updated flow
        response = await clients.langflow_request(
            "PATCH", f"/api/v1/flows/{flow_id}", json=flow_data
        )

        if response.status_code != 200:
            raise Exception(f"Failed to update flow: HTTP {response.status_code} - {response.text}")

        await self._lock_flow(flow_id)

        return {
            "flow": flow_name,
            "success": True,
            "message": f"Successfully updated {', '.join(updates_made)}",
            "flow_id": flow_id,
        }

    async def _update_component_langflow(self, template, model: Any):
        # Call custom_component/update endpoint to get updated template
        # Only call if code field exists (custom components should have code)
        if "code" in template and "value" in template["code"]:
            code_value = template["code"]["value"]

            try:
                update_payload = {
                    "code": code_value,
                    "template": template,
                    "field": "model",
                    "field_value": model,
                    "tool_mode": False,
                }

                response = await clients.langflow_request(
                    "POST", "/api/v1/custom_component/update", json=update_payload
                )

                if response.status_code == 200:
                    response_data = response.json()
                    # Update template with the new template from response.data
                    if "template" in response_data:
                        # Update the template in component_node
                        return response_data["template"]
                    else:
                        logger.warning("Response from custom_component/update missing 'data' field")
                else:
                    logger.warning(
                        f"Failed to call custom_component/update: HTTP {response.status_code} - {response.text}"
                    )
            except Exception as e:
                logger.error(f"Error calling custom_component/update: {str(e)}")
                # Continue with manual updates even if API call fails

    async def _update_component_fields(
        self,
        component_node,
        provider: str,
        model_value: str,
    ):
        """Update fields in a component node based on provider and component type"""
        template = component_node.get("data", {}).get("node", {}).get("template", {})
        if not template:
            return False

        updated = False
        provider_name = self._get_provider_name_display(provider)

        # Update model field and call custom_component/update endpoint
        if "model" in template:
            # Enable the model in Langflow first
            await self._enable_model_in_langflow(provider_name, model_value)
            model_field = template["model"]
            options = model_field.get("options")

            # Agent ModelInput fields intentionally load their catalog from
            # Langflow's external enabled-model registry and therefore do not
            # serialize an ``options`` list. Persist the selected identity and
            # let the component update endpoint enrich its metadata. Treating
            # the missing list as an incompatible field silently left the
            # managed Agent on its previous provider.
            if not isinstance(options, list):
                model_options = [{"provider": provider_name, "name": model_value}]
                model_field["value"] = model_options
                template = (
                    await self._update_component_langflow(template, model_options) or template
                )
                selected_provider, selected_model = self._runtime_model_identity(
                    template.get("model", {}).get("value")
                )
                if (selected_provider, selected_model) != (
                    provider.casefold(),
                    model_value,
                ):
                    template["model"]["value"] = model_options
                component_node["data"]["node"]["template"] = template
                return True

            # Update template via Langflow API to get latest options
            template = (
                await self._update_component_langflow(template, template["model"]["value"])
                or template
            )
            component_node["data"]["node"]["template"] = template

            # Find the specific model option for the provider
            if model_value:
                model_options = [
                    item
                    for item in template["model"].get("options", [])
                    if item.get("provider") == provider_name and item.get("name") == model_value
                ]
            else:
                # If no specific model provided, pick the first available one for this provider
                model_options = [
                    item
                    for item in template["model"].get("options", [])
                    if item.get("provider") == provider_name
                ][:1]
                if model_options:
                    logger.info(
                        f"Using first available model '{model_options[0].get('name')}' for provider {provider_name}"
                    )

            if not model_options:
                logger.warning(
                    f"Model {model_value or 'ANY'} not found for provider {provider_name}"
                )
                return False

            template["model"]["value"] = model_options

            template = await self._update_component_langflow(template, model_options) or template
            component_node["data"]["node"]["template"] = template

            updated = True

        # Update provider-specific fields using Langflow global variable names.
        # "api_base" is the Ollama URL field on the Embedding Model component;
        # "ollama_base_url" is the equivalent field on the Language Model / Agent component.
        field_mappings = {
            "api_key": {
                "openai": "OPENAI_API_KEY",
                "watsonx": "WATSONX_APIKEY",
                "anthropic": "ANTHROPIC_API_KEY",
            },
            "api_base": {
                "ollama": "OLLAMA_BASE_URL",
            },
            "ollama_base_url": {
                "ollama": "OLLAMA_BASE_URL",
            },
            "base_url_ibm_watsonx": {
                "watsonx": "WATSONX_URL",
            },
            "project_id": {
                "watsonx": "WATSONX_PROJECT_ID",
            },
        }

        for field, mapping in field_mappings.items():
            if field in template:
                target_value = mapping.get(provider)
                if target_value:
                    template[field]["value"] = target_value
                    template[field]["load_from_db"] = True
                else:
                    template[field]["value"] = ""
                    template[field]["load_from_db"] = False
                updated = True

        return updated

    async def _enable_model_in_langflow(self, provider_name: str, model_value: str):
        """Ensure the specified model is enabled in Langflow."""
        try:
            enabled_data = None
            async with self._enabled_models_lock:
                # Check if cache is still valid
                if (
                    self._enabled_models_cache
                    and self._enabled_models_cache_time
                    and (datetime.now() - self._enabled_models_cache_time).total_seconds()
                    < self._enabled_models_cache_ttl
                ):
                    enabled_data = self._enabled_models_cache
                else:
                    # Cache miss or stale, fetch from Langflow
                    get_response = await clients.langflow_request(
                        "GET", "/api/v1/models/enabled_models"
                    )
                    if get_response.status_code == 200:
                        enabled_data = get_response.json().get("enabled_models", {})
                        self._enabled_models_cache = enabled_data
                        self._enabled_models_cache_time = datetime.now()
                        logger.debug("Refreshed enabled models cache from Langflow")

            if enabled_data:
                provider_models = enabled_data.get(provider_name, {})
                if provider_models.get(model_value) is True:
                    logger.info(
                        f"Model {model_value} for provider {provider_name} is already enabled. Skipping."
                    )
                    return False

            enable_payload = [{"provider": provider_name, "model_id": model_value, "enabled": True}]

            response = await clients.langflow_request(
                "POST", "/api/v1/models/enabled_models", json=enable_payload
            )

            if response.status_code == 200:
                logger.info(
                    f"Successfully enabled model {model_value} for provider {provider_name}"
                )
                async with self._enabled_models_lock:
                    self._enabled_models_cache = None
                return True
            else:
                logger.warning(
                    f"Failed to enable model: HTTP {response.status_code} - {response.text}"
                )
                async with self._enabled_models_lock:
                    self._enabled_models_cache = None
        except Exception as e:
            logger.error(f"Error enabling model in Langflow: {str(e)}")
        return False

    def _get_provider_component_ids(self, provider: str):
        """Helper to get component display names for various providers."""
        from config.settings import (
            OLLAMA_EMBEDDING_COMPONENT_DISPLAY_NAME,
            OLLAMA_LLM_COMPONENT_DISPLAY_NAME,
            OPENAI_EMBEDDING_COMPONENT_DISPLAY_NAME,
            OPENAI_LLM_COMPONENT_DISPLAY_NAME,
            WATSONX_EMBEDDING_COMPONENT_DISPLAY_NAME,
            WATSONX_LLM_COMPONENT_DISPLAY_NAME,
        )

        if provider == "openai":
            return (OPENAI_EMBEDDING_COMPONENT_DISPLAY_NAME, OPENAI_LLM_COMPONENT_DISPLAY_NAME)
        elif provider == "watsonx":
            return (WATSONX_EMBEDDING_COMPONENT_DISPLAY_NAME, WATSONX_LLM_COMPONENT_DISPLAY_NAME)
        elif provider == "ollama":
            return (OLLAMA_EMBEDDING_COMPONENT_DISPLAY_NAME, OLLAMA_LLM_COMPONENT_DISPLAY_NAME)
        elif provider == "anthropic":
            return (None, "Anthropic")
        return (None, None)

    async def get_flows_updates_available(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """
        Check for available flow updates.
        Uses cached update results from startup/previous checks if available.
        Surfaces all available updates regardless of locked status.
        """
        target_user = user_id or "default"
        if not hasattr(self, "_custom_updates_cache") or self._custom_updates_cache is None:
            flow_configs = [
                ("nudges", NUDGES_FLOW_ID),
                ("retrieval", LANGFLOW_CHAT_FLOW_ID),
                ("ingest", LANGFLOW_INGEST_FLOW_ID),
                ("url_ingest", LANGFLOW_URL_INGEST_FLOW_ID),
            ]
            cache = []
            for flow_type, flow_id in flow_configs:
                update_info = await self._check_flow_update(flow_type, flow_id)
                if update_info:
                    cache.append(update_info)
            self._custom_updates_cache = cache

        updates = []
        for item in self._custom_updates_cache:
            is_dismissed = False
            if hasattr(self, "_dismissed_updates") and isinstance(self._dismissed_updates, dict):
                is_dismissed = item["flow_type"] in self._dismissed_updates.get(target_user, set())
            updates.append(
                {
                    **item,
                    "dismissed": is_dismissed,
                }
            )

        return updates

    async def bulk_update_flows(self, flow_types: list[str], backup_custom: bool = True):
        """
        Bulk updates multiple flows.
        If backup_custom is True, flows are backed up in Langflow before resetting.
        Local file backups are always saved via _backup_flow prior to resetting.
        Returns the backup flow details if any.
        """
        results: list[dict[str, Any]] = []
        flow_configs_dict = {
            "nudges": NUDGES_FLOW_ID,
            "retrieval": LANGFLOW_CHAT_FLOW_ID,
            "ingest": LANGFLOW_INGEST_FLOW_ID,
            "url_ingest": LANGFLOW_URL_INGEST_FLOW_ID,
        }

        for flow_type in flow_types:
            flow_id = flow_configs_dict.get(flow_type)
            if not flow_id:
                results.append(
                    {"flow_type": flow_type, "success": False, "error": "Invalid flow_type"}
                )
                continue

            flow_lock = await self._get_flow_lock(flow_id)
            async with flow_lock:
                backup_path = None
                backup_flow_id = None
                flow_check_ok = False

                try:
                    response = await clients.langflow_request("GET", f"/api/v1/flows/{flow_id}")
                    if response.status_code == 200:
                        flow_check_ok = True
                        flow_data = response.json()

                        # Always attempt local file backup for safety
                        backup_path = await self._backup_flow(flow_id, flow_type, flow_data)

                        if backup_custom:
                            logger.info(f"Backing up flow {flow_type} in Langflow before update.")
                            # Create a backup flow directly in Langflow
                            backup_payload = dict(flow_data)
                            backup_payload.pop("id", None)
                            flow_name = flow_data.get("name", flow_type.capitalize())
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                            backup_payload["name"] = f"Backup - {flow_name} ({timestamp})"
                            backup_payload["description"] = (
                                f"Backup of flow '{flow_name}' created before flow update on {timestamp}. "
                                "Use this flow to reference or redo your modifications."
                            )
                            backup_payload["locked"] = False

                            lf_create_res = await clients.langflow_request(
                                "POST", "/api/v1/flows/", json=backup_payload
                            )
                            if lf_create_res.status_code in (200, 201):
                                created_flow = lf_create_res.json()
                                backup_flow_id = created_flow.get("id")
                                logger.info(
                                    f"Created backup flow in Langflow for {flow_type}: '{backup_payload['name']}' (ID: {backup_flow_id})"
                                )
                            else:
                                logger.warning(
                                    f"Failed to create backup flow in Langflow for {flow_type}: HTTP {lf_create_res.status_code}"
                                )
                except Exception as e:
                    logger.error(f"Failed to check/backup flow {flow_id} before update: {e}")

                if not flow_check_ok:
                    logger.error(
                        f"Pre-flight check failed for flow {flow_type}; aborting update to protect flow modifications."
                    )
                    results.append(
                        {
                            "flow_type": flow_type,
                            "success": False,
                            "error": "Pre-flight check failed; update aborted to avoid losing changes",
                            "backup_path": None,
                            "backup_flow_id": None,
                        }
                    )
                    continue

                # If backup_custom is enabled, abort update if BOTH backups failed
                if backup_custom and backup_path is None and backup_flow_id is None:
                    logger.error(
                        f"Backup failed for flow {flow_type}; aborting update to protect flow modifications."
                    )
                    results.append(
                        {
                            "flow_type": flow_type,
                            "success": False,
                            "error": "Backup failed; update aborted to avoid losing changes",
                            "backup_path": None,
                            "backup_flow_id": None,
                        }
                    )
                    continue

                try:
                    reset_res = await self._reset_langflow_flow_locked(flow_type, flow_id)
                    if reset_res.get("success"):
                        self.clear_dismissed_update(flow_type)
                    results.append(
                        {
                            "flow_type": flow_type,
                            "success": reset_res.get("success", False),
                            "error": reset_res.get("error"),
                            "backup_path": backup_path,
                            "backup_flow_id": backup_flow_id,
                        }
                    )
                except Exception as e:
                    results.append(
                        {
                            "flow_type": flow_type,
                            "success": False,
                            "error": str(e),
                            "backup_path": backup_path,
                            "backup_flow_id": backup_flow_id,
                        }
                    )

        if results and any(r.get("success") for r in results):
            self._custom_updates_cache = None
            try:
                config = get_openrag_config()
                if config.edited:
                    from api.settings import reapply_all_settings

                    await reapply_all_settings()
            except Exception as e:
                logger.error(f"Error reapplying settings after bulk update: {e}")

        return results
