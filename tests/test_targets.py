"""Label rules, selector matching, and where the agent's labels come from."""

import pytest

from src.targets import (
    MAX_LABELS,
    TargetLabels,
    TargetLabelsError,
    match,
    parse_label_pairs,
    parse_labels_file,
    validate_label_key,
    validate_label_value,
    validate_selector,
)


class TestLabelRules:
    @pytest.mark.parametrize(
        "key",
        ["env", "a", "app.kubernetes.io/name", "example.com/a_b-c.d", "A1", "x" * 63],
    )
    def test_good_keys(self, key):
        assert validate_label_key(key) == key

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "-a",
            "a-",
            "a b",
            "a=b",
            "x" * 64,
            "/name",
            "Prefix.COM/name",
            "pre fix/name",
            "a/b/c",
            "a/",
            ("p" * 254) + "/name",
            "a,b",
        ],
    )
    def test_bad_keys(self, key):
        with pytest.raises(TargetLabelsError):
            validate_label_key(key)

    @pytest.mark.parametrize("value", ["", "staging", "v1.2_3-x", "9", "x" * 63])
    def test_good_values(self, value):
        assert validate_label_value(value) == value

    @pytest.mark.parametrize(
        "value", ["-x", "x-", "a b", "x" * 64, 'a"b', "a,b", "a=b"]
    )
    def test_bad_values(self, value):
        with pytest.raises(TargetLabelsError):
            validate_label_value(value, "k")

    def test_non_strings_are_rejected(self):
        with pytest.raises(TargetLabelsError):
            validate_label_value(1, "k")  # type: ignore[arg-type]
        with pytest.raises(TargetLabelsError):
            validate_label_key(None)  # type: ignore[arg-type]


class TestParsePairs:
    def test_pairs(self):
        assert parse_label_pairs("env=staging, app=checkout") == {
            "env": "staging",
            "app": "checkout",
        }

    def test_empty_and_blank_parts(self):
        assert parse_label_pairs("") == {}
        assert parse_label_pairs(" , ,") == {}

    def test_an_empty_value_is_allowed(self):
        assert parse_label_pairs("flag=") == {"flag": ""}

    @pytest.mark.parametrize(
        "text", ["env", "env=a,env=b", "bad key=x", "k=bad value", "=x"]
    )
    def test_errors(self, text):
        with pytest.raises(TargetLabelsError):
            parse_label_pairs(text)

    def test_too_many(self):
        text = ",".join(f"k{i}=v" for i in range(MAX_LABELS + 1))
        with pytest.raises(TargetLabelsError, match="at most"):
            parse_label_pairs(text)


class TestParseFile:
    DOWNWARD_API = (
        'app="resilient-app"\n'
        'app.kubernetes.io/name="checkout"\n'
        'pod-template-hash="5d8f9c7b6"\n'
        'env="staging"\n'
    )

    def test_the_kubernetes_downward_api_format(self):
        assert parse_labels_file(self.DOWNWARD_API) == {
            "app": "resilient-app",
            "app.kubernetes.io/name": "checkout",
            "pod-template-hash": "5d8f9c7b6",
            "env": "staging",
        }

    def test_blank_lines_and_empty_values(self):
        assert parse_labels_file('\n\nflag=""\n\n') == {"flag": ""}

    @pytest.mark.parametrize(
        "text",
        [
            "env=staging",
            'env="staging',
            'env=staging"',
            "env",
            'env="a"\nenv="b"',
            'env="a\\"b"',
            'env="a b"',
        ],
    )
    def test_errors(self, text):
        with pytest.raises(TargetLabelsError):
            parse_labels_file(text)


class TestMatch:
    LABELS = {"env": "staging", "app": "checkout", "tier": "canary"}

    def test_no_selector_matches_anything(self):
        assert match(None, {}).matches
        assert match({}, {}).matches

    def test_match_labels_all_must_hold(self):
        sel = {"match_labels": {"env": "staging", "app": "checkout"}}
        assert match(sel, self.LABELS).matches
        wrong = match({"match_labels": {"env": "prod"}}, self.LABELS)
        assert not wrong.matches
        assert wrong.problems == ["env must be 'prod' but is 'staging'"]

    def test_a_missing_label_does_not_match(self):
        result = match({"match_labels": {"region": "eu"}}, self.LABELS)
        assert not result.matches
        assert result.problems == ["region must be 'eu' but is not set"]

    def test_no_labels_matches_no_selector(self):
        assert not match({"match_labels": {"env": "staging"}}, {}).matches

    def test_an_empty_value_must_be_matched_exactly(self):
        assert match({"match_labels": {"flag": ""}}, {"flag": ""}).matches
        assert not match({"match_labels": {"flag": ""}}, {}).matches

    def test_in(self):
        sel = {
            "match_expressions": [
                {"key": "tier", "operator": "In", "values": ["canary", "preview"]}
            ]
        }
        assert match(sel, self.LABELS).matches
        bad = match(sel, {"tier": "prod"})
        assert bad.problems == ["tier must be one of 'canary', 'preview' but is 'prod'"]
        assert not match(sel, {}).matches

    def test_notin_is_satisfied_by_a_missing_key(self):
        sel = {
            "match_expressions": [
                {"key": "tier", "operator": "NotIn", "values": ["prod"]}
            ]
        }
        assert match(sel, {"tier": "canary"}).matches
        assert match(sel, {}).matches
        bad = match(sel, {"tier": "prod"})
        assert bad.problems == ["tier must not be one of 'prod' but is 'prod'"]

    def test_exists(self):
        sel = {"match_expressions": [{"key": "chaos-ok", "operator": "Exists"}]}
        assert match(sel, {"chaos-ok": ""}).matches
        assert match(sel, {"chaos-ok": "anything"}).matches
        assert match(sel, {}).problems == ["chaos-ok must be set but is not"]

    def test_does_not_exist(self):
        sel = {
            "match_expressions": [{"key": "chaos-optout", "operator": "DoesNotExist"}]
        }
        assert match(sel, {}).matches
        assert match(sel, {"chaos-optout": "true"}).problems == [
            "chaos-optout must not be set but is 'true'"
        ]

    def test_labels_and_expressions_are_anded_and_all_problems_listed(self):
        sel = {
            "match_labels": {"env": "prod"},
            "match_expressions": [{"key": "tier", "operator": "In", "values": ["x"]}],
        }
        result = match(sel, self.LABELS)
        assert not result.matches
        assert len(result.problems) == 2
        assert (
            "env must be 'prod'" in result.summary
            and "tier must be one of" in result.summary
        )

    def test_serializes(self):
        assert match({"match_labels": {"env": "x"}}, {}).to_dict() == {
            "matches": False,
            "problems": ["env must be 'x' but is not set"],
        }
        assert match(None, {}).summary == "matches"


class TestValidateSelector:
    def test_valid(self):
        validate_selector(
            {
                "match_labels": {"env": "staging"},
                "match_expressions": [
                    {"key": "tier", "operator": "In", "values": ["a"]},
                    {"key": "x", "operator": "Exists"},
                ],
            }
        )

    def test_empty_is_rejected(self):
        for selector in ({}, {"match_labels": {}, "match_expressions": []}):
            with pytest.raises(TargetLabelsError, match="would match every agent"):
                validate_selector(selector)

    @pytest.mark.parametrize(
        "expr",
        [
            {"key": "a", "operator": "In"},
            {"key": "a", "operator": "In", "values": []},
            {"key": "a", "operator": "NotIn"},
            {"key": "a", "operator": "Exists", "values": ["x"]},
            {"key": "a", "operator": "DoesNotExist", "values": ["x"]},
            {"key": "a", "operator": "Equals", "values": ["x"]},
            {"key": "bad key", "operator": "Exists"},
            {"key": "a", "operator": "In", "values": ["bad value"]},
            {"key": "a", "operator": "In", "values": ["v"] * 21},
        ],
    )
    def test_bad_expressions(self, expr):
        with pytest.raises(TargetLabelsError):
            validate_selector({"match_expressions": [expr]})

    def test_too_many_conditions(self):
        with pytest.raises(TargetLabelsError, match="at most"):
            validate_selector({"match_labels": {f"k{i}": "v" for i in range(21)}})


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestAgentLabels:
    def labels(self, env, clock=None):
        return TargetLabels(environ=env, clock=clock or Clock())

    def test_none_configured(self):
        assert self.labels({}).get() == {}

    def test_from_the_environment(self):
        env = {"CHAOS_TARGET_LABELS": "env=staging,app=checkout"}
        assert self.labels(env).get() == {"env": "staging", "app": "checkout"}

    def test_from_a_file(self, tmp_path):
        path = tmp_path / "labels"
        path.write_text('env="staging"\napp="checkout"\n')
        assert self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)}).get() == {
            "env": "staging",
            "app": "checkout",
        }

    def test_the_file_wins_over_the_environment(self, tmp_path):
        path = tmp_path / "labels"
        path.write_text('env="staging"\n')
        env = {
            "CHAOS_TARGET_LABELS": "env=prod,extra=1",
            "CHAOS_TARGET_LABELS_FILE": str(path),
        }
        assert self.labels(env).get() == {"env": "staging", "extra": "1"}

    def test_get_returns_a_copy(self):
        t = self.labels({"CHAOS_TARGET_LABELS": "a=b"})
        t.get()["a"] = "changed"
        assert t.get() == {"a": "b"}

    def test_changes_are_picked_up_after_the_cache_expires(self, tmp_path):
        path = tmp_path / "labels"
        path.write_text('chaos-ok="true"\n')
        clock = Clock()
        t = self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)}, clock)
        assert t.get() == {"chaos-ok": "true"}
        path.write_text('other="x"\n')
        clock.now = 0.5
        assert t.get() == {"chaos-ok": "true"}, "re-read inside the cache window"
        clock.now = 1.5
        assert t.get() == {"other": "x"}

    def test_an_unreadable_file_means_no_labels_not_stale_ones(self, tmp_path):
        path = tmp_path / "labels"
        path.write_text('chaos-ok="true"\n')
        clock = Clock()
        t = self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)}, clock)
        assert t.get() == {"chaos-ok": "true"}
        path.unlink()
        clock.now = 2
        assert t.get() == {}
        assert "cannot read" in t.error
        assert str(path) not in t.error, "the path should not be echoed back"

    def test_it_recovers_when_the_file_comes_back(self, tmp_path):
        path = tmp_path / "labels"
        clock = Clock()
        t = self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)}, clock)
        assert t.get() == {} and t.error
        path.write_text('a="b"\n')
        clock.now = 2
        assert t.get() == {"a": "b"} and t.error is None

    def test_an_invalid_file_means_no_labels(self, tmp_path):
        path = tmp_path / "labels"
        path.write_text("not a label line\n")
        t = self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)})
        assert t.get() == {}
        assert t.error

    def test_a_huge_file_is_refused(self, tmp_path):
        path = tmp_path / "labels"
        path.write_text('a="b"\n' * 20000)
        t = self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)})
        assert t.get() == {} and "too large" in t.error

    def test_a_binary_file_is_refused(self, tmp_path):
        path = tmp_path / "labels"
        path.write_bytes(b"\xff\xfe\x00")
        t = self.labels({"CHAOS_TARGET_LABELS_FILE": str(path)})
        assert t.get() == {} and "UTF-8" in t.error

    def test_startup_validation_raises(self, tmp_path):
        with pytest.raises(TargetLabelsError):
            self.labels({"CHAOS_TARGET_LABELS": "bad key=x"}).validate_startup()
        with pytest.raises(TargetLabelsError, match="cannot read"):
            self.labels(
                {"CHAOS_TARGET_LABELS_FILE": str(tmp_path / "nope")}
            ).validate_startup()

    def test_startup_validation_returns_the_labels(self):
        t = self.labels({"CHAOS_TARGET_LABELS": "a=b"})
        assert t.validate_startup() == {"a": "b"}

    def test_reads_the_real_environment_when_none_is_given(self, monkeypatch):
        monkeypatch.setenv("CHAOS_TARGET_LABELS", "env=ci")
        assert TargetLabels().get() == {"env": "ci"}

    def test_reset(self, monkeypatch):
        monkeypatch.setenv("CHAOS_TARGET_LABELS", "env=ci")
        t = TargetLabels()
        t.get()
        monkeypatch.setenv("CHAOS_TARGET_LABELS", "env=other")
        t.reset()
        assert t.get() == {"env": "other"}
