from app.connectors.github import GitHubConnector, commit_types, directory_of

PR = {
    "repo": "platform", "number": 412, "title": "Batch offset commits", "body": "Fixes consumer lag. " * 20,
    "author": "p_004", "created_at": "2026-09-01T10:00:00Z", "merged_at": "2026-09-02T10:00:00Z",
    "files": ["shared/kafka-client/src/consumer.py", "shared/kafka-client/src/batch.py",
              "services/payments/consumer/lag.py", "README.md", "deploy/app.yaml"],
    "commits": [{"message": "fix(consumer): commit offsets after batch"}, {"message": "fix: retry"},
                {"message": "test: add batch tests"}, {"message": "wip"}, {"message": "feat!: new api"}],
    "reviews": [{"reviewer": "p_009", "body": "lgtm", "submitted_at": "2026-09-01T12:00:00Z"}],
}


def test_directory_rule():
    assert directory_of("services/payments/consumer/lag.py") == "services/payments/consumer"
    assert directory_of("shared/kafka-client/consumer.py") == "shared/kafka-client"
    assert directory_of("deploy/app.yaml") == "deploy"
    assert directory_of("README.md") is None


def test_commit_types():
    assert commit_types(PR["commits"]) == {"fix": 2, "test": 1, "other": 1, "feat": 1}


def test_one_pr_event_and_one_review_event():
    pr, review = GitHubConnector().normalize(PR)
    assert (pr.kind, pr.direction, pr.person_id, pr.external_id) == ("pr", "did", "p_004", "platform#412")
    assert pr.time.day == 2  # merged_at
    assert pr.metadata["directories"] == ["shared/kafka-client/src", "services/payments/consumer", "deploy"]
    assert pr.metadata["languages"] == ["Python", "YAML"]
    assert pr.metadata["files_changed"] == 5
    assert "commits: 2 fix, 1 test, 1 other, 1 feat" in pr.text
    assert len(pr.text) <= 800

    assert (review.kind, review.direction, review.person_id) == ("review", "did", "p_009")
    assert review.text.startswith('Reviewed PR #412 "Batch offset commits" in shared/kafka-client/src')
    assert "commit_types" not in review.metadata
