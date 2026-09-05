"""Controlled live validation against existing OpenSearch/Langflow.

Run in the backend environment with this checkout on PYTHONPATH. Creates only
uniquely named canary indices, a new temporary application DB, local accounts
via the product bootstrap/admin/login APIs, and isolated chat sessions. Never
runs application startup migrations against the source workspace or changes
OpenSearch security, managed flows, production auth, or functional configuration.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import uuid
from pathlib import Path


async def validate(args):
    # Copy only workspace configuration, with its existing encrypted envelopes.
    # No production user/session/credential rows enter the controlled user store.
    with sqlite3.connect(f"file:{args.workspace_db}?mode=ro", uri=True) as source:
        sections = [
            (section, json.loads(value))
            for section, value in source.execute("SELECT section,value FROM workspace_config")
        ]
    scratch = Path(args.scratch).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    database = scratch / "controlled-users.db"
    if database.exists():
        raise RuntimeError("Refusing to reuse a controlled credential database")
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
            "OPENRAG_DATA_PATH": str(scratch / "data"),
            "OPENRAG_CONFIG_PATH": str(scratch / "config"),
            "OPENRAG_KEYS_PATH": args.keys,
            "OPENRAG_AUTH_MODE": "local",
            "OPENRAG_RBAC_ENFORCE": "true",
            "OPENRAG_AUTH_COOKIE_SECURE": "false",  # isolated HTTP callback listener
            "IBM_AUTH_ENABLED": "false",
            "GOOGLE_OAUTH_CLIENT_ID": "",
            "GOOGLE_OAUTH_CLIENT_SECRET": "",
            "MICROSOFT_OAUTH_CLIENT_ID": "",
            "MICROSOFT_OAUTH_CLIENT_SECRET": "",
            "MICROSOFT_GRAPH_OAUTH_CLIENT_ID": "",
            "MICROSOFT_GRAPH_OAUTH_CLIENT_SECRET": "",
            "OPENRAG_STORAGE_MODE": "db",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "DO_NOT_TRACK": "1",
            "OPENRAG_BACKEND_INTERNAL_URL": f"http://{args.callback_host}:{args.port}",
        }
    )
    import httpx
    import uvicorn
    from fastapi import FastAPI

    from api import auth, chat, local_auth, search, users
    from auth.local_admin import bootstrap_admin
    from config import settings
    from config.auth_mode import validate_auth_configuration
    from db import engine
    from db.migrations_runtime import run_alembic_upgrade_async
    from db.repositories.workspace_config_repo import WorkspaceConfigRepo
    from db.seed import seed_roles_and_permissions
    from models.document_metadata import DocumentMetadataProfile, MetadataObservation
    from models.metadata_filter import MetadataFilter, MetadataFilterClause
    from models.metadata_filter_projection import (
        MetadataFilterProjectionSourceContext,
        metadata_filter_projection_index_body,
    )
    from services.auth_service import AuthService
    from services.chat_service import ChatService
    from services.dls_principal_service import DLSPrincipalService
    from services.flows_service import FlowsService
    from services.knowledge_filter_service import KnowledgeFilterService
    from services.metadata_candidate_restriction import resolve_metadata_candidates
    from services.metadata_filter_projection import (
        build_projection_side_document,
        generate_metadata_filter_projection,
    )
    from services.models_service import ModelsService
    from services.rbac_service import RBACService
    from services.search_service import SearchService, register_search_service
    from services.workspace_config_service import WorkspaceConfigService
    from session_manager import SessionManager
    from utils.embedding_fields import get_embedding_field_name

    validate_auth_configuration()
    await run_alembic_upgrade_async()
    engine.init_engine()
    factory = engine.SessionLocal
    assert factory is not None
    admin_password = secrets.token_urlsafe(24)
    async with factory() as session:
        await seed_roles_and_permissions(session)
        for section, value in sections:
            await WorkspaceConfigRepo(session).upsert(section, value)
        admin_user = await bootstrap_admin(session, "controlled-admin", admin_password)
        await session.commit()
    workspace = WorkspaceConfigService(settings.config_manager, factory)
    await workspace.hydrate_on_startup()
    config = settings.get_openrag_config()
    functional_before = config.to_dict()
    source_index = config.knowledge.index_name
    suffix = uuid.uuid4().hex
    index = f"documents_local_auth_{suffix}"
    metadata_index = f"documents_metadata_local_auth_{suffix}"
    filter_index = f"knowledge_filters_local_auth_{suffix}"
    control_index = f"openrag_generation_control_auth_{suffix}"
    config.knowledge.index_name = index
    import services.knowledge_filter_service as filter_module
    import services.search_service as search_module

    filter_module.KNOWLEDGE_FILTERS_INDEX_NAME = filter_index

    async def canary_metadata_candidates(client, metadata_filter):
        return await resolve_metadata_candidates(
            client, metadata_filter, projection_alias=metadata_index
        )

    search_module.resolve_metadata_candidates = canary_metadata_candidates
    # All retrieval behavior/model/prompt fields remain the authoritative values.
    manager = SessionManager()
    os_admin = settings.clients.create_index_admin_opensearch_client()
    models = ModelsService()
    search_service = SearchService(manager, models)
    register_search_service(search_service)
    flows = FlowsService()
    chat_service = ChatService(flows_service=flows, search_service=search_service)
    dls = DLSPrincipalService(None, opensearch_client=os_admin)
    app = FastAPI()
    app.state.services = {
        "session_manager": manager,
        "auth_service": AuthService(manager),
        "rbac_service": RBACService(factory),
        "search_service": search_service,
        "chat_service": chat_service,
        "dls_principal_service": dls,
    }
    app.include_router(local_auth.router)
    app.include_router(users.router)
    for path, handler, methods in [
        ("/auth/me", auth.auth_me, ["GET"]),
        ("/auth/logout", auth.auth_logout, ["POST"]),
        ("/search", search.search, ["POST"]),
        ("/search/metadata-agent", search.metadata_agent_search, ["POST"]),
        ("/langflow", chat.langflow_endpoint, ["POST"]),
    ]:
        app.add_api_route(path, handler, methods=methods)
    traces = []

    class TraceIdentity:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            async def capture(message):
                if message["type"] == "http.response.start":
                    user = scope.get("state", {}).get("user")
                    traces.append(
                        {
                            "path": scope["path"],
                            "status": message["status"],
                            "user_id": user.user_id if user else None,
                        }
                    )
                await send(message)

            await self.inner(scope, receive, capture)

    server = uvicorn.Server(
        uvicorn.Config(
            TraceIdentity(app),
            host="0.0.0.0",
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        if server_task.done():
            await server_task
            raise RuntimeError("Controlled listener did not start")
        await asyncio.sleep(0.05)
    created_indices = []
    user_clients = {}
    local_clients = {}
    user_ids = {}
    result = {
        "source_sha": args.source_sha,
        "production_auth_changed": False,
        "external_providers_configured": False,
        "checks": {},
        "users": {},
        "runtime_behavior_config": "MATCH",
        "trace": traces,
    }
    evidence_path = scratch / "validation.json"
    key_id = None
    lf_login_headers = None
    lf_http = httpx.AsyncClient(base_url=settings.LANGFLOW_URL, timeout=60)

    def passed(name, details=True):
        result["checks"][name] = details
        evidence_path.write_text(json.dumps(result, indent=2) + "\n")
        print("CHECK", name, "PASS", flush=True)

    try:
        # A dedicated Langflow API key is removed in finally; managed flows stay untouched.
        response = await lf_http.post(
            "/api/v1/login",
            data={
                "username": settings.LANGFLOW_SUPERUSER,
                "password": settings.LANGFLOW_SUPERUSER_PASSWORD,
            },
        )
        response.raise_for_status()
        lf_login_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        response = await lf_http.post(
            "/api/v1/api_key/",
            headers=lf_login_headers,
            json={"name": f"local-auth-validation-{suffix}"},
        )
        response.raise_for_status()
        key_data = response.json()
        key_id = key_data.get("id")
        settings.LANGFLOW_KEY = key_data["api_key"]
        await settings.clients.initialize()
        settings.clients.opensearch = os_admin
        mapping = (await os_admin.indices.get_mapping(index=source_index))[source_index]["mappings"]
        source_settings = (await os_admin.indices.get_settings(index=source_index))[source_index][
            "settings"
        ]["index"]
        body = {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0, "index.knn": True},
            "mappings": mapping,
        }
        if "knn.algo_param.ef_search" in source_settings:
            body["settings"]["index.knn.algo_param.ef_search"] = source_settings[
                "knn.algo_param.ef_search"
            ]
        for name, definition in [
            (index, body),
            (metadata_index, metadata_filter_projection_index_body()),
            (
                filter_index,
                {
                    "mappings": {
                        "properties": {
                            "owner": {"type": "keyword"},
                            "allowed_users": {"type": "keyword"},
                            "updated_at": {"type": "date"},
                            "query_data": {"type": "text"},
                        }
                    }
                },
            ),
            (control_index, {}),
        ]:
            await os_admin.indices.create(index=name, body=definition)
            created_indices.append(name)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{args.port}", timeout=120
        ) as operator:
            response = await operator.post(
                "/auth/local/login", json={"login": "controlled-admin", "password": admin_password}
            )
            response.raise_for_status()
            for side in ["A", "B"]:
                password = secrets.token_urlsafe(24)
                response = await operator.post(
                    "/users/local", json={"login": f"user-{side.lower()}", "password": password}
                )
                response.raise_for_status()
                uid = response.json()["user_id"]
                user_ids[side] = uid
                browser = httpx.AsyncClient(base_url=f"http://127.0.0.1:{args.port}", timeout=300)
                local_clients[side] = browser
                response = await browser.post(
                    "/auth/local/login",
                    json={"login": f"user-{side.lower()}", "password": password},
                )
                response.raise_for_status()
                me = await browser.get("/users/me")
                me.raise_for_status()
                assert me.json()["user_id"] == uid and me.json()["roles"] == ["user"]
                result["users"][side] = {
                    "user_id": uid,
                    "roles": me.json()["roles"],
                    "real_login": True,
                }
                token = browser.cookies.get("auth_token")
                user_clients[side] = settings.clients.create_user_opensearch_client(token)
        assert user_ids["A"] != user_ids["B"]
        passed("two_real_local_principals")
        documents = {}
        from datetime import UTC, datetime

        for side in ["A", "B"]:
            code = f"LOCAL-AUTH-{side}-{suffix[:12]}"
            content = (
                f"Pommerieux local authentication validation. Private validation code: {code}."
            )
            digest = hashlib.sha256(content.encode()).hexdigest()
            document_id = f"local-auth-{suffix}-{side}"
            entity_id = f"urn:openrag:local-auth:{suffix}:{side}"
            target = f"urn:openrag:local-auth:{suffix}:{'B' if side == 'A' else 'A'}"
            formatted_model = await models.get_litellm_model_name(
                config.knowledge.embedding_model, strict=True
            )
            embedding = await settings.clients.patched_embedding_client.embeddings.create(
                model=formatted_model, input=[content]
            )
            document = {
                "chunk_id": f"{document_id}:0",
                "document_id": document_id,
                "text": content,
                "document_content_sha256": digest,
                "chunk_content_sha256": digest,
                "document_profile_version": 1,
                "document_order_verified": True,
                "document_chunk_count": 1,
                "document_page_count": 1,
                "document_max_page": 1,
                "document_character_count": len(content),
                "chunk_index": 0,
                "page": 1,
                "filename": f"{side.lower()}-local-auth.txt",
                "mimetype": "text/plain",
                "embedding_model": config.knowledge.embedding_model,
                get_embedding_field_name(config.knowledge.embedding_model): embedding.data[
                    0
                ].embedding,
                "owner": user_ids[side],
                "allowed_users": [user_ids[side]],
                "allowed_groups": [],
                "allowed_principals": [],
                "ingest_run_id": suffix,
                "source_entity_id": entity_id,
                "source_entity_type": "email_message",
                "source_entity_system": "local-auth-validation",
                "source_entity_alternate_ids": [],
                "source_relation_target_ids": [target],
                "source_relation_roles": ["reply_to"],
                "source_provenance": {
                    "schema_version": "1.0",
                    "entity": {
                        "id": entity_id,
                        "type": "email_message",
                        "source_system": "local-auth-validation",
                    },
                    "relations": [
                        {
                            "role": "reply_to",
                            "target": {
                                "id": target,
                                "type": "email_message",
                                "source_system": "local-auth-validation",
                            },
                            "prov_predicate": "http://www.w3.org/ns/prov#wasInfluencedBy",
                        }
                    ],
                },
            }
            from services.retrieval_service import (
                document_manifest_sha256,
                verified_chunk_manifest,
            )

            document["document_content_sha256"] = document_manifest_sha256(
                [verified_chunk_manifest(document)]
            )
            documents[side] = document
            await os_admin.index(index=index, id=document["chunk_id"], body=document, refresh=True)
            observation_args = {
                "section": "embedded",
                "source": "pdf_info_dictionary",
                "source_type": "format_native",
                "trust_class": "embedded_document_metadata",
                "extracted_at": datetime.now(UTC),
            }
            profile = DocumentMetadataProfile(
                entity_id=entity_id,
                embedded=[
                    MetadataObservation(
                        **observation_args,
                        field="creator",
                        value=f"Author {side}",
                        normalization_status="normalized",
                    ),
                    MetadataObservation(
                        **observation_args,
                        field="embedded_created_at",
                        value="2026-09-05T12:00:00Z",
                        raw_value="2026-09-05T12:00:00Z",
                        timezone="Z",
                        normalization_status="timezone_explicit",
                    ),
                ],
            )
            projection = generate_metadata_filter_projection(
                profile,
                source_context=MetadataFilterProjectionSourceContext(
                    source_entity_id=entity_id,
                    source_entity_type="document",
                    source_system="local-auth-validation",
                    mime_type="text/plain",
                    filename=document["filename"],
                ),
            )
            side_document = build_projection_side_document(
                projection,
                source_document_id=document_id,
                source_entity_id=entity_id,
                representative_chunk_id=document["chunk_id"],
                owner=user_ids[side],
            )
            # Two visible rows exercise pagination; both refer to the same harmless source.
            for page in range(2):
                row = side_document.model_dump(mode="json")
                row["projection_document_id"] += str(page)
                await os_admin.index(
                    index=metadata_index, id=row["projection_document_id"], body=row, refresh=True
                )

        await os_admin.index(
            index=filter_index,
            id="shared",
            body={
                "id": "shared",
                "name": "Controlled shared filter",
                "owner": user_ids["A"],
                "allowed_users": list(user_ids.values()),
                "updated_at": datetime.now(UTC).isoformat(),
                "query_data": json.dumps(
                    {"filters": {"data_sources": [d["filename"] for d in documents.values()]}}
                ),
            },
            refresh=True,
        )
        await os_admin.index(
            index=control_index, id="head", body={"occurrence": "control-canary"}, refresh=True
        )
        query = "Pommerieux local authentication validation"
        original_mode = config.knowledge.retrieval_mode
        for side, hidden in [("A", "B"), ("B", "A")]:
            browser, os_user = local_clients[side], user_clients[side]
            own, other = documents[side], documents[hidden]
            token = f"Bearer {browser.cookies.get('auth_token')}"
            # Lexical, dense, hybrid each use the actual backend product endpoint.
            for mode in ["lexical", "vector", "hybrid"]:
                config.knowledge.retrieval_mode = mode
                response = await browser.post("/search", json={"query": query, "limit": 10})
                response.raise_for_status()
                payload = response.json()
                assert payload.get("retrieval_execution_complete") is True, payload.get(
                    "retrieval_failure_codes"
                )
                assert (
                    own["document_id"] in response.text
                    and other["document_id"] not in response.text
                )
                assert other["source_entity_id"] not in response.text
                hidden_query = await browser.post(
                    "/search", json={"query": other["text"], "limit": 10}
                )
                hidden_query.raise_for_status()
                assert hidden_query.json()["retrieval_execution_complete"] is True
                assert other["document_id"] not in hidden_query.text
                assert other["source_entity_id"] not in hidden_query.text
                passed(f"{side}_{mode}")
            config.knowledge.retrieval_mode = original_mode
            for field, operator, values, extras in [
                ("mime", "EQUAL", ["text/plain"], {}),
                ("creator_observation", "EQUAL", [f"author {side}"], {}),
                ("creator_observation", "EXISTS", [], {}),
                (
                    "production_month",
                    "EQUAL",
                    ["2026-09"],
                    {
                        "calendar_basis": "SOURCE_LOCAL",
                        "source_policy": "ANY_VALID_PRODUCTION_OBSERVATION",
                    },
                ),
            ]:
                predicate = MetadataFilter(
                    clauses=(
                        MetadataFilterClause(
                            field=field, operator=operator, values=values, **extras
                        ),
                    )
                )
                candidates = await resolve_metadata_candidates(
                    os_user, predicate, projection_alias=metadata_index, page_size=1
                )
                assert candidates.source_entity_ids == (own["source_entity_id"],)
                assert (
                    candidates.diagnostics.visible_projection_count == 2
                    and candidates.diagnostics.eligible_count == 1
                )
                assert candidates.diagnostics.pages == 3
                response = await browser.post(
                    "/search",
                    json={"query": query, "metadata_filter": predicate.model_dump(mode="json")},
                )
                response.raise_for_status()
                assert (
                    own["document_id"] in response.text
                    and other["document_id"] not in response.text
                )
            count = await os_user.count(index=metadata_index)
            aggregate = await os_user.search(
                index=metadata_index,
                body={"size": 0, "aggs": {"owners": {"terms": {"field": "owner"}}}},
            )
            assert count["count"] == 2
            assert aggregate["aggregations"]["owners"]["buckets"][0]["key"] == user_ids[side]
            assert len(aggregate["aggregations"]["owners"]["buckets"]) == 1
            hidden_predicate = MetadataFilter(
                clauses=(
                    MetadataFilterClause(
                        field="creator_observation", operator="EQUAL", values=[f"author {hidden}"]
                    ),
                )
            )
            hidden_candidates = await resolve_metadata_candidates(
                os_user, hidden_predicate, projection_alias=metadata_index, page_size=1
            )
            assert hidden_candidates.source_entity_ids == ()
            assert hidden_candidates.diagnostics.visible_projection_count == 2
            assert hidden_candidates.diagnostics.eligible_count == 0
            passed(f"{side}_metadata_search_exists_date_creator_count_aggregation_pagination")
            filters = await KnowledgeFilterService(manager).search_knowledge_filters(
                "", user_ids[side], token
            )
            assert filters["success"] and filters["filters"][0]["active_source_count"] == 1
            scoped = await browser.post(
                "/search",
                json={
                    "query": query,
                    "filters": {"data_sources": [d["filename"] for d in documents.values()]},
                },
            )
            scoped.raise_for_status()
            assert own["document_id"] in scoped.text and other["document_id"] not in scoped.text
            passed(f"{side}_ASTRA_020_shared_filter_reader_count")
            response = await browser.post(
                "/search", json={"evidenceMode": "exhaustive", "documentId": other["document_id"]}
            )
            response.raise_for_status()
            assert response.json()["results"] == [] and not response.json()["coverage"]["complete"]
            citations = await search_service.resolve_cited_chunks(
                [other["chunk_id"]], user_id=user_ids[side], jwt_token=token
            )
            assert citations == []
            own_citations = await search_service.resolve_cited_chunks(
                [own["chunk_id"]], user_id=user_ids[side], jwt_token=token
            )
            assert len(own_citations) == 1
            passed(f"{side}_direct_read_citation")
            response = await browser.post(
                "/search", json={"query": query, "evidenceMode": "scope_exhaustive"}
            )
            response.raise_for_status()
            payload = response.json()
            assert (
                other["document_id"] not in response.text
                and other["source_entity_id"] not in response.text
            )
            assert own["document_id"] in response.text
            # A backend-proven DLS boundary is outside this reader's closure.
            # Missing provenance still fails closed in the P0 regression suite.
            assert payload["coverage"]["complete"] is True, payload["coverage"]
            assert payload["coverage"]["documents_discovered"] == 1
            passed(
                f"{side}_prov_o_isolation",
                {"hidden_identifiers_absent": True, "coverage": payload.get("coverage")},
            )
            try:
                await os_user.search(index=control_index, body={"query": {"match_all": {}}})
                raise AssertionError("User reached backend-only GenerationHead control index")
            except Exception as error:
                assert getattr(error, "status_code", None) == 403
            passed(f"{side}_backend_only_control_index_denied")

        # Ordinary leaf closure must still certify with these same real readers.
        for document in documents.values():
            leaf = copy.deepcopy(document)
            leaf["source_provenance"]["relations"] = []
            leaf["source_relation_target_ids"] = []
            leaf["source_relation_roles"] = []
            await os_admin.index(index=index, id=leaf["chunk_id"], body=leaf, refresh=True)
        for side in ["A", "B"]:
            response = await local_clients[side].post(
                "/search", json={"query": query, "evidenceMode": "scope_exhaustive"}
            )
            response.raise_for_status()
            payload = response.json()
            assert payload["coverage"]["complete"] is True, payload["coverage"]
            passed(f"{side}_accessible_leaf_coverage_complete", payload["coverage"])
        # Exercise Agent and streaming with the actual cross-owner relations.
        for document in documents.values():
            await os_admin.index(index=index, id=document["chunk_id"], body=document, refresh=True)
        for side, hidden in [("A", "B"), ("B", "A")]:
            for kind, prompt in [
                (
                    "retrieval",
                    "Find every document about Pommerieux local authentication validation. Read each visible document completely and quote its private validation code with citations.",
                ),
                (
                    "metadata",
                    "Use the metadata search tool to find documents produced in September 2026 about Pommerieux local authentication validation. Read each matching document completely and quote its private validation code with citations.",
                ),
            ]:
                before_trace = len(traces)
                async with local_clients[side].stream(
                    "POST", "/langflow", json={"prompt": prompt, "stream": True}
                ) as response:
                    response.raise_for_status()
                    stream_text = "".join([part async for part in response.aiter_text()])
                (scratch / f"stream-{side}-{kind}.txt").write_text(stream_text)
                assert f"LOCAL-AUTH-{hidden}-{suffix[:12]}" not in stream_text
                assert documents[hidden]["source_entity_id"] not in stream_text
                assert '"type": "response.completed"' in stream_text
                callbacks = [t for t in traces[before_trace:] if t["path"].startswith("/search")]
                assert all(t["user_id"] == user_ids[side] for t in callbacks), callbacks
                evidence = {
                    "callback_count": len(callbacks),
                    "bytes": len(stream_text),
                    "sha256": hashlib.sha256(stream_text.encode()).hexdigest(),
                    "own_evidence_present": f"LOCAL-AUTH-{side}-{suffix[:12]}" in stream_text,
                    "callback_success": bool(callbacks)
                    and all(t["status"] == 200 for t in callbacks),
                    "metadata_callback": any(
                        t["path"] == "/search/metadata-agent" for t in callbacks
                    ),
                }
                if (
                    evidence["own_evidence_present"]
                    and evidence["callback_success"]
                    and (kind != "metadata" or evidence["metadata_callback"])
                ):
                    passed(f"{side}_agent_streaming_{kind}", evidence)
                else:
                    result.setdefault("failed_product_checks", {})[
                        f"{side}_agent_streaming_{kind}"
                    ] = evidence
                    print("CHECK", f"{side}_agent_streaming_{kind}", "FAIL", flush=True)
            before_trace = len(traces)
            response = await local_clients[side].post(
                "/langflow",
                json={
                    "prompt": "Read every visible document about Pommerieux local authentication validation and quote its private validation code with citations.",
                    "stream": False,
                },
            )
            response.raise_for_status()
            (scratch / f"agent-{side}.json").write_text(response.text)
            assert f"LOCAL-AUTH-{side}-{suffix[:12]}" in response.text
            assert f"LOCAL-AUTH-{hidden}-{suffix[:12]}" not in response.text
            assert documents[hidden]["source_entity_id"] not in response.text
            callbacks = [t for t in traces[before_trace:] if t["path"].startswith("/search")]
            assert callbacks and all(
                t["user_id"] == user_ids[side] and t["status"] == 200 for t in callbacks
            )
            passed(
                f"{side}_agent_nonstream",
                {
                    "callback_count": len(callbacks),
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                },
            )
        config.knowledge.index_name = source_index
        assert config.to_dict() == functional_before
        passed("RuntimeBehavior_functional_configuration_unchanged")
        result["production_activation_gate"] = (
            "PASS"
            if not result.get("failed_product_checks")
            and all(
                result["checks"][f"{s}_prov_o_isolation"]["coverage"]["complete"]
                for s in ["A", "B"]
            )
            else "PARTIAL"
        )
        result["status"] = result["production_activation_gate"]
    except Exception as error:
        result["status"] = "FAIL"
        # Do not serialize exceptions carrying HTTP credentials or SQL parameters.
        result["failure_type"] = type(error).__name__
        import traceback

        traceback.print_exc()
    finally:
        server.should_exit = True
        await server_task
        for browser in local_clients.values():
            await browser.aclose()
        for client in user_clients.values():
            await client.close()
        for name in reversed(created_indices):
            await os_admin.indices.delete(index=name)
        for uid in [admin_user.id, *user_ids.values()]:
            await os_admin.delete(
                index=settings.DLS_PRINCIPAL_INDEX_NAME, id=uid, ignore=[404], refresh=True
            )
        if lf_login_headers:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as controlled_db:
                conversation_ids = [
                    row[0]
                    for row in controlled_db.execute("SELECT response_id FROM session_ownership")
                ]
            result["langflow_sessions_cleaned"] = []
            for conversation_id in conversation_ids:
                deletion = await lf_http.delete(
                    f"/api/v1/monitor/messages/session/{conversation_id}", headers=lf_login_headers
                )
                result["langflow_sessions_cleaned"].append(
                    {"response_id": conversation_id, "status": deletion.status_code}
                )
        if key_id and lf_login_headers:
            deletion = await lf_http.delete(f"/api/v1/api_key/{key_id}", headers=lf_login_headers)
            result["langflow_key_cleaned"] = deletion.status_code in {200, 204}
        await lf_http.aclose()
        await settings.clients.cleanup()
        await engine.dispose_engine()
        result["canary_indices_cleaned"] = created_indices
        evidence_path.write_text(json.dumps(result, indent=2) + "\n")
        print("VALIDATION=" + json.dumps(result), flush=True)
    return result["status"] == "PASS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-db", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--keys", required=True)
    parser.add_argument("--callback-host", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    return 0 if asyncio.run(validate(args)) else 1


if __name__ == "__main__":
    sys.exit(main())
