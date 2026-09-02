import pytest
from agent_fabric.schemas import RetrySpec, RunCreate
from pydantic import ValidationError


def valid_run() -> dict[str, object]:
    return {
        "repository": {"url": "https://github.com/example/project", "ref": "main"},
        "argv": ["python", "-m", "pytest"],
        "profile": "python",
    }


def test_run_contract_accepts_argv() -> None:
    run = RunCreate.model_validate(valid_run())
    assert run.network == "disabled"
    assert run.resources.cpu_millis == 1000


@pytest.mark.parametrize(
    "url",
    ["http://github.com/a/b", "https://user:token@github.com/a/b", "https://127.0.0.1/a"],
)
def test_repository_rejects_unsafe_urls(url: str) -> None:
    body = valid_run()
    body["repository"] = {"url": url, "ref": "main"}
    with pytest.raises(ValidationError):
        RunCreate.model_validate(body)


def test_multiple_attempts_require_explicit_safety() -> None:
    with pytest.raises(ValidationError):
        RetrySpec(safe_on_worker_loss=False, max_attempts=2)


def test_gpu_run_requires_cuda_and_vram() -> None:
    body = valid_run()
    body["profile"] = "cuda"
    body["resources"] = {"gpu": 1, "vram_mb": 8192}
    with pytest.raises(ValidationError):
        RunCreate.model_validate(body)
    body["required_capabilities"] = ["cuda"]
    run = RunCreate.model_validate(body)
    assert run.resources.gpu == 1
    assert run.resources.vram_mb == 8192


def test_vram_without_gpu_is_rejected() -> None:
    body = valid_run()
    body["resources"] = {"vram_mb": 8192}
    with pytest.raises(ValidationError):
        RunCreate.model_validate(body)
