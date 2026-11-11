import httpx
import pytest
import yaml

from heimdall.core.models import ResourceRef
from heimdall.executor.gitops import GitOps, GitOpsError
from tests.unit.fake_gitea import FakeGitea
from tests.unit.fake_kube import FakeKube

ROOT = "online-boutique"
KUSTOMIZATION = f"{ROOT}/kustomization.yaml"
FRONTEND = ResourceRef(kind="Deployment", namespace="boutique", name="frontend")
BASE_KUSTOMIZATION = yaml.safe_dump(
    {"resources": ["upstream.yaml"], "patches": [{"path": "labels.yaml"}]}, sort_keys=False
)


def with_patch(*paths: str) -> str:
    data = yaml.safe_load(BASE_KUSTOMIZATION)
    data["patches"] += [{"path": p} for p in paths]
    return yaml.safe_dump(data, sort_keys=False)


@pytest.fixture
def gitea() -> FakeGitea:
    return FakeGitea({KUSTOMIZATION: BASE_KUSTOMIZATION, f"{ROOT}/labels.yaml": "labels: 1\n"})


def bad_release(gitea: FakeGitea, tag: str = "v0.11.1") -> str:
    """The kind of commit scenario s04 makes: a new override plus its entry."""
    return gitea.commit(
        f"Release frontend {tag}",
        {
            f"{ROOT}/overrides/frontend-image.yaml": f"image: frontend:{tag}\n",
            KUSTOMIZATION: with_patch("overrides/frontend-image.yaml"),
        },
    )


def gitops(gitea: FakeGitea) -> GitOps:
    return GitOps(gitea.client(), "heimdall/gitops")


def test_files_of_a_resource_are_its_overrides_and_the_kustomization(gitea: FakeGitea) -> None:
    assert gitops(gitea).files_for(FRONTEND) == []

    bad_release(gitea)
    gitea.commit("Tune adservice", {f"{ROOT}/overrides/adservice-resources.yaml": "x: 1\n"})

    assert gitops(gitea).files_for(FRONTEND) == [
        f"{ROOT}/overrides/frontend-image.yaml",
        KUSTOMIZATION,
    ]


def test_revert_of_a_new_override_deletes_it_and_its_kustomization_entry(
    gitea: FakeGitea,
) -> None:
    sha = bad_release(gitea)

    revert = gitops(gitea).plan_revert(FRONTEND)

    assert revert.commit == sha
    assert revert.message == "Release frontend v0.11.1"
    assert revert.changes[f"{ROOT}/overrides/frontend-image.yaml"] is None
    assert yaml.safe_load(revert.changes[KUSTOMIZATION]) == yaml.safe_load(BASE_KUSTOMIZATION)
    assert revert.diff_lines == 2


def test_revert_of_an_edited_override_restores_the_previous_content(gitea: FakeGitea) -> None:
    bad_release(gitea, "v0.11.0")
    image_file = f"{ROOT}/overrides/frontend-image.yaml"
    gitea.commit("Release frontend v0.11.1", {image_file: "image: frontend:v0.11.1\n"})

    revert = gitops(gitea).plan_revert(FRONTEND)

    assert revert.changes == {f"{ROOT}/overrides/frontend-image.yaml": "image: frontend:v0.11.0\n"}


def test_only_this_resources_files_are_touched(gitea: FakeGitea) -> None:
    bad_release(gitea)
    gitea.commit(
        "Tune adservice",
        {
            f"{ROOT}/overrides/adservice-resources.yaml": "memory: 40Mi\n",
            KUSTOMIZATION: with_patch(
                "overrides/frontend-image.yaml", "overrides/adservice-resources.yaml"
            ),
        },
    )

    revert = gitops(gitea).plan_revert(FRONTEND)

    assert f"{ROOT}/overrides/adservice-resources.yaml" not in revert.changes
    kept = [p["path"] for p in yaml.safe_load(revert.changes[KUSTOMIZATION])["patches"]]
    assert kept == ["labels.yaml", "overrides/adservice-resources.yaml"]


def test_nothing_to_revert_is_an_error(gitea: FakeGitea) -> None:
    with pytest.raises(GitOpsError, match="no change to frontend to revert"):
        gitops(gitea).plan_revert(FRONTEND)


def test_pull_request_reverts_on_a_branch_and_leaves_main_alone(gitea: FakeGitea) -> None:
    bad_release(gitea)
    main_before = dict(gitea.files("main"))

    pr = gitops(gitea).open_revert_pr("inc_1", FRONTEND, "Revision 7 fails 30% of requests.")

    assert (pr.number, pr.branch) == (1, "heimdall/inc_1")
    assert gitea.files("main") == main_before
    branch = gitea.files("heimdall/inc_1")
    assert f"{ROOT}/overrides/frontend-image.yaml" not in branch
    assert yaml.safe_load(branch[KUSTOMIZATION]) == yaml.safe_load(BASE_KUSTOMIZATION)
    pull = gitea.pulls[0]
    assert pull["title"] == 'Revert "Release frontend v0.11.1" (inc_1)'
    assert "Revision 7 fails 30% of requests." in pull["body"]


def test_opening_twice_returns_the_same_pull_request(gitea: FakeGitea) -> None:
    bad_release(gitea)
    ops = gitops(gitea)

    first = ops.open_revert_pr("inc_1", FRONTEND, "s")
    second = ops.open_revert_pr("inc_1", FRONTEND, "s")

    assert first == second
    assert len(gitea.pulls) == 1


def test_merged_is_reported_after_a_human_merges(gitea: FakeGitea) -> None:
    bad_release(gitea)
    ops = gitops(gitea)
    pr = ops.open_revert_pr("inc_1", FRONTEND, "s")
    assert not ops.merged(pr.number)

    gitea.merge(pr.number)

    assert ops.merged(pr.number)
    assert f"{ROOT}/overrides/frontend-image.yaml" not in gitea.files("main")


def test_unreachable_git_server_is_a_gitops_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://gitea")
    with pytest.raises(GitOpsError, match="unreachable"):
        GitOps(client, "heimdall/gitops").files_for(FRONTEND)


def test_pause_and_resume_patch_only_the_sync_policy(gitea: FakeGitea) -> None:
    kube = FakeKube()
    kube.applications = True
    ops = GitOps(gitea.client(), "heimdall/gitops", kube.client())

    ops.pause_sync()
    ops.resume_sync()

    paths = {p["path"] for p in kube.patches}
    assert paths == {"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/boutique"}
    assert [p["body"] for p in kube.patches] == [
        {"spec": {"syncPolicy": {"automated": None}}},
        {"spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": False}}}},
    ]


def test_pause_without_a_cluster_client_is_an_error(gitea: FakeGitea) -> None:
    with pytest.raises(GitOpsError):
        gitops(gitea).pause_sync()
