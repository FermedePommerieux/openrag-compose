from services.retrieval_progress_service import RetrievalProgressService


def test_retrieval_progress_is_sanitized_and_terminal():
    service = RetrievalProgressService()
    service.start("request-42")
    service.update(
        "request-42",
        phase="provenance",
        message="Following document relations",
        counters={
            "documents": 3,
            "negative": -1,
            "boolean": True,
            "text": "not a counter",
        },
    )
    service.finish("request-42", complete=True)

    snapshot = service.snapshot("request-42")
    assert snapshot is not None
    assert snapshot["phase"] == "complete"
    assert snapshot["counters"] == {"documents": 3}
    assert snapshot["complete"] is True
    assert snapshot["failed"] is False


def test_retrieval_progress_rejects_untrusted_identifiers():
    service = RetrievalProgressService()
    service.start("../../untrusted")

    assert service.snapshot("../../untrusted") is None
