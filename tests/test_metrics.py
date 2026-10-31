"""Both protocols record the same Prometheus metrics."""

from prometheus_client import REGISTRY

from app.grpc_api.generated import counter_pb2


def request_count(protocol: str, operation: str, status: str = "ok") -> float:
    labels = {"protocol": protocol, "operation": operation, "status": status}
    return REGISTRY.get_sample_value("counter_requests_total", labels) or 0


async def test_rest_records_metrics_and_serves_them(rest):
    before = request_count("rest", "LikePost")

    await rest.put("/api/v1/posts/1/like", headers={"X-User-Id": "10"})

    assert request_count("rest", "LikePost") == before + 1
    response = await rest.get("/metrics")
    assert response.status_code == 200
    assert "counter_request_duration_seconds_bucket" in response.text


async def test_grpc_records_metrics(grpc_stub):
    before = request_count("grpc", "RecordView")

    await grpc_stub.RecordView(counter_pb2.ViewRequest(user_id=10, post_id=1))

    assert request_count("grpc", "RecordView") == before + 1


async def test_failed_request_is_counted_as_error(api):
    before = request_count(api.protocol, "GetCounters", "error")
    api.faults.fail_first_n = 1

    await api.client.get_counters(user_id=10, post_ids=[1])  # the first attempt fails, the retry works

    assert request_count(api.protocol, "GetCounters", "error") == before + 1
