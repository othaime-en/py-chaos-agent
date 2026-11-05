"""The Prometheus check, against a fake Prometheus."""

import pytest

from src.steady_state import run_check, run_prometheus_check, run_round
from tests.fake_prometheus import FakePrometheus, scalar, vector

MATRIX = b'{"status": "success", "data": {"resultType": "matrix", "result": []}}'
BAD_NUMBER = (
    b'{"status": "success", "data": {"resultType": "vector", '
    b'"result": [{"value": [1, "abc"]}]}}'
)
NO_VALUE = (
    b'{"status": "success", "data": {"resultType": "vector", "result": [{"nope": 1}]}}'
)


@pytest.fixture
def prom():
    p = FakePrometheus()
    yield p
    p.close()


def check(prom, **overrides):
    return {
        "type": "prometheus",
        "name": "error-rate",
        "url": prom.url,
        "query": "sum(rate(errors_total[1m])) / sum(rate(requests_total[1m]))",
        "operator": "<",
        "threshold": 0.01,
        "timeout_seconds": 2,
        "on_no_data": "fail",
        "bearer_token_env": None,
        **overrides,
    }


class TestValues:
    def test_a_value_within_the_limit_passes(self, prom):
        prom.body = vector(0.002)
        result = run_prometheus_check(check(prom))
        assert result.ok and not result.unavailable
        assert result.detail == "0.002 < 0.01"
        assert result.latency_ms is not None

    def test_a_value_over_the_limit_is_a_breach_not_unavailable(self, prom):
        prom.body = vector(0.05)
        result = run_prometheus_check(check(prom))
        assert not result.ok and not result.unavailable
        assert result.detail == "0.05 is not < 0.01"

    @pytest.mark.parametrize(
        "op, value, passes",
        [
            ("<", 1.0, False),
            ("<", 0.9, True),
            ("<=", 1.0, True),
            ("<=", 1.1, False),
            (">", 1.0, False),
            (">", 1.1, True),
            (">=", 1.0, True),
            (">=", 0.9, False),
        ],
    )
    def test_each_operator_at_the_boundary(self, prom, op, value, passes):
        prom.body = vector(value)
        assert (
            run_prometheus_check(check(prom, operator=op, threshold=1.0)).ok is passes
        )

    def test_a_lower_limit(self, prom):
        """Availability style: the value must stay above a floor."""
        prom.body = vector(0.99)
        assert (
            run_prometheus_check(check(prom, operator=">=", threshold=0.995)).ok
            is False
        )
        prom.body = vector(0.999)
        assert run_prometheus_check(check(prom, operator=">=", threshold=0.995)).ok

    def test_scalar_results_work(self, prom):
        prom.body = scalar(0.5)
        assert run_prometheus_check(check(prom, threshold=1)).ok

    def test_infinity_is_a_value_and_breaches(self, prom):
        prom.body = vector("+Inf")
        result = run_prometheus_check(check(prom))
        assert not result.ok and not result.unavailable

    def test_the_query_and_path_are_what_was_configured(self, prom):
        run_prometheus_check(check(prom, query='up{job="api"} == 1'))
        assert prom.queries == [("/api/v1/query", 'up{job="api"} == 1')]

    def test_a_trailing_slash_on_the_url_is_fine(self, prom):
        run_prometheus_check(check(prom, url=prom.url + "/"))
        assert prom.queries[0][0] == "/api/v1/query"


class TestNoData:
    def test_an_empty_result_is_unavailable_by_default(self, prom):
        prom.body = vector()
        result = run_prometheus_check(check(prom))
        assert not result.ok and result.unavailable
        assert result.detail == "no data returned"

    def test_it_can_be_treated_as_passing(self, prom):
        prom.body = vector()
        result = run_prometheus_check(check(prom, on_no_data="pass"))
        assert result.ok and not result.unavailable
        assert "no data" in result.detail

    def test_nan_is_no_data(self, prom):
        prom.body = vector("NaN")
        assert run_prometheus_check(check(prom)).unavailable
        assert run_prometheus_check(check(prom, on_no_data="pass")).ok


class TestUnavailable:
    def test_several_series_are_refused_with_advice(self, prom):
        prom.body = vector(0.001, 0.002)
        result = run_prometheus_check(check(prom))
        assert result.unavailable
        assert "2 series, expected 1" in result.detail and "aggregate" in result.detail

    def test_a_rejected_query(self, prom):
        prom.status = 400
        prom.body = {
            "status": "error",
            "errorType": "bad_data",
            "error": "parse error at char 5",
        }
        result = run_prometheus_check(check(prom))
        assert result.unavailable and result.detail == "HTTP 400"
        assert "parse error" not in result.detail

    def test_an_error_status_in_a_200_response(self, prom):
        prom.body = {
            "status": "error",
            "errorType": "timeout",
            "error": "query timed out",
        }
        result = run_prometheus_check(check(prom))
        assert result.unavailable and result.detail == "Prometheus error: timeout"

    def test_a_server_error(self, prom):
        prom.status = 503
        assert run_prometheus_check(check(prom)).detail == "HTTP 503"

    def test_a_timeout(self, prom):
        prom.delay = 1.5
        result = run_prometheus_check(check(prom, timeout_seconds=1))
        assert result.unavailable and "timed out" in result.detail

    def test_a_refused_connection_does_not_leak_the_address(self):
        result = run_prometheus_check(
            {
                "type": "prometheus", "name": "x", "url": "http://127.0.0.1:1",
                "query": "up", "operator": "<", "threshold": 1, "timeout_seconds": 1,
            }
        )  # fmt: skip
        assert result.unavailable and result.detail == "ConnectionError"
        assert "127.0.0.1" not in result.detail

    @pytest.mark.parametrize(
        "raw, detail",
        [
            (b"<html>nope</html>", "response is not JSON"),
            (b"\xff\xfe", "response is not JSON"),
            (b"[1, 2]", "response is not a Prometheus result"),
            (b'{"status": "success"}', "response has no data"),
            (MATRIX, "unsupported result type 'matrix'"),
            (BAD_NUMBER, "response is not a number"),
            (NO_VALUE, "response is not a number"),
        ],
    )
    def test_malformed_responses(self, prom, raw, detail):
        prom.raw = raw
        result = run_prometheus_check(check(prom))
        assert result.unavailable and result.detail == detail

    def test_a_huge_response_is_refused(self, prom):
        prom.raw = b" " * (1024 * 1024 + 10)
        result = run_prometheus_check(check(prom))
        assert result.unavailable and result.detail == "response too large"

    def test_redirects_are_not_followed(self, prom):
        other = FakePrometheus()
        try:
            prom.redirect = other.url + "/api/v1/query"
            result = run_prometheus_check(check(prom))
            assert result.unavailable and result.detail == "HTTP 302"
            assert other.queries == []
        finally:
            other.close()

    def test_a_broken_probe_is_unavailable_without_its_message(self, prom, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("secret detail")

        monkeypatch.setattr("src.steady_state.requests.get", boom)
        result = run_prometheus_check(check(prom))
        assert result.unavailable and result.detail == "unexpected RuntimeError"


class TestAuth:
    def test_the_bearer_token_comes_from_the_environment(self, prom, monkeypatch):
        monkeypatch.setenv("PROM_TOKEN", "s3cret-value")
        assert run_prometheus_check(check(prom, bearer_token_env="PROM_TOKEN")).ok
        assert prom.headers_seen[0]["Authorization"] == "Bearer s3cret-value"

    def test_no_header_without_a_token_setting(self, prom):
        run_prometheus_check(check(prom))
        assert "Authorization" not in prom.headers_seen[0]

    def test_a_missing_variable_is_unavailable_and_sends_nothing(
        self, prom, monkeypatch
    ):
        monkeypatch.delenv("PROM_TOKEN", raising=False)
        result = run_prometheus_check(check(prom, bearer_token_env="PROM_TOKEN"))
        assert result.unavailable and result.detail == "PROM_TOKEN is not set"
        assert prom.queries == []

    def test_the_token_never_appears_in_a_result(self, prom, monkeypatch):
        monkeypatch.setenv("PROM_TOKEN", "s3cret-value")
        prom.status = 401
        result = run_prometheus_check(check(prom, bearer_token_env="PROM_TOKEN"))
        assert "s3cret-value" not in str(result.to_dict())


class TestDispatch:
    def test_run_check_routes_by_type(self, prom):
        assert run_check(check(prom)).detail == "0.001 < 0.01"

    def test_rounds_can_mix_kinds(self, prom):
        from tests.http_target import Target

        http = Target()
        try:
            results = run_round(
                [
                    {
                        "type": "http",
                        "name": "h",
                        "url": http.url,
                        "timeout_seconds": 2,
                    },
                    check(prom),
                ]
            )
            assert [r.ok for r in results] == [True, True]
        finally:
            http.close()

    def test_unavailable_is_serialized_only_when_true(self, prom):
        ok = run_prometheus_check(check(prom)).to_dict()
        assert "unavailable" not in ok
        prom.body = vector()
        assert run_prometheus_check(check(prom)).to_dict()["unavailable"] is True
