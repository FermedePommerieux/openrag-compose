import json

import pytest

from benchmarks.discovery.retrieval_correctness_benchmark import _parse_remote_output


def test_parse_remote_output_uses_last_marker_and_requires_object():
    payload = {"schema_version": 1, "identity_audit": {"missing_chunk_id": 0}}
    output = "diagnostic log\nRETRIEVAL_CORRECTNESS_JSON=" + json.dumps(payload)

    assert _parse_remote_output(output) == payload

    with pytest.raises(ValueError, match="marker"):
        _parse_remote_output("diagnostic log only")
    with pytest.raises(ValueError, match="object"):
        _parse_remote_output("RETRIEVAL_CORRECTNESS_JSON=[]")
