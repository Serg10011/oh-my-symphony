"""A matching GitHub run in the queue is not an executing verification."""
from test_api_cloud_check import RUN, TRIAL, cloud, index_path, indexed, public_api, responses


def test_check_stays_queued_until_github_reports_execution(monkeypatch):
    docs = responses()
    current = indexed(docs, TRIAL)
    docs[index_path()] = {"workflow_runs": [{**current, "status": "queued", "conclusion": None}]}
    public_api(monkeypatch, docs)
    scan = {}
    assert cloud.discover({TRIAL}, 0, scan)[TRIAL]["phase"] == "queued"
    docs[index_path()]["workflow_runs"][0]["status"] = "in_progress"
    found = cloud.discover({TRIAL}, 0, scan)[TRIAL]
    assert found["phase"] == "running" and found["run"] == RUN
