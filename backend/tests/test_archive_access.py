from fastapi.testclient import TestClient

from tests.conftest import login_client


def test_viewer_cannot_access_archived_tool_apis(m0_app):
    client = TestClient(m0_app)
    csrf = login_client(client, "viewer_noscope", "StrongViewerPassword!3")
    headers = {"X-CSRF-Token": csrf}
    archived_reads = (
        "execution/capabilities",
        "direct-lab/capabilities",
        "direct-lab/suites",
        "direct-lab/scorers",
        "direct-lab/runs",
        "direct-lab/runs/run-old",
        "direct-lab/runs/run-old/diff/run-other",
        "toolkit/offline-reports",
        "toolkit/offline-reports/report-old",
        "toolkit/params",
        "synth/datasets",
        "synth/selection/batches",
        "synth/selection/batch-old",
        "training/runs",
        "training/runs/run-old",
    )
    archived_mutations = (
        ("POST", "execution/plan"),
        ("POST", "execution/jobs"),
        ("POST", "direct-lab/validate"),
        ("POST", "direct-lab/validate-cases"),
        ("POST", "direct-lab/runs"),
        ("POST", "direct-lab/compare"),
        ("DELETE", "direct-lab/runs/run-old"),
        ("POST", "toolkit/verify/batch-old"),
        ("POST", "toolkit/offline-calc"),
        ("DELETE", "toolkit/offline-reports/report-old"),
        ("POST", "toolkit/params"),
        ("DELETE", "toolkit/params/group-old"),
        ("POST", "synth/generate"),
        ("POST", "synth/check"),
        ("POST", "synth/selection/batch-old/preview"),
        ("POST", "training/plan"),
    )
    for method, path in (*(("GET", path) for path in archived_reads), *archived_mutations):
        response = client.request(method, f"/api/v1/{path}", headers=headers, json={} if method == "POST" else None)
        assert response.status_code == 403, (method, path, response.text)
        error = response.json()["error"]
        assert error["code"] == "SCOPE_FORBIDDEN"
        assert error["detail"]["required_role"] == "researcher"

    response = client.put("/api/v1/toolkit/official-result/batch-old", headers=headers, json={})
    assert response.status_code == 403
    assert response.json()["error"]["detail"]["required_role"] == "admin"

    for path in ("toolkit/manuals", "toolkit/methodology"):
        assert client.get(f"/api/v1/{path}").status_code == 200
