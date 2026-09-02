from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WorkerMessage(_message.Message):
    __slots__ = ("worker_id", "register", "heartbeat", "acknowledgement", "event", "completion", "cleanup")
    WORKER_ID_FIELD_NUMBER: _ClassVar[int]
    REGISTER_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    ACKNOWLEDGEMENT_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_FIELD_NUMBER: _ClassVar[int]
    CLEANUP_FIELD_NUMBER: _ClassVar[int]
    worker_id: str
    register: Register
    heartbeat: Heartbeat
    acknowledgement: LeaseAcknowledgement
    event: RunEvent
    completion: RunCompletion
    cleanup: CleanupConfirmation
    def __init__(self, worker_id: _Optional[str] = ..., register: _Optional[_Union[Register, _Mapping]] = ..., heartbeat: _Optional[_Union[Heartbeat, _Mapping]] = ..., acknowledgement: _Optional[_Union[LeaseAcknowledgement, _Mapping]] = ..., event: _Optional[_Union[RunEvent, _Mapping]] = ..., completion: _Optional[_Union[RunCompletion, _Mapping]] = ..., cleanup: _Optional[_Union[CleanupConfirmation, _Mapping]] = ...) -> None: ...

class ControlMessage(_message.Message):
    __slots__ = ("lease", "cancel", "drain", "error")
    LEASE_FIELD_NUMBER: _ClassVar[int]
    CANCEL_FIELD_NUMBER: _ClassVar[int]
    DRAIN_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    lease: LeaseOffer
    cancel: CancelRun
    drain: DrainWorker
    error: ProtocolError
    def __init__(self, lease: _Optional[_Union[LeaseOffer, _Mapping]] = ..., cancel: _Optional[_Union[CancelRun, _Mapping]] = ..., drain: _Optional[_Union[DrainWorker, _Mapping]] = ..., error: _Optional[_Union[ProtocolError, _Mapping]] = ...) -> None: ...

class Register(_message.Message):
    __slots__ = ("protocol_version", "worker_version", "cpu_millis", "memory_mb", "pids", "capabilities", "sandbox_backends", "gpu_count", "vram_mb")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    WORKER_VERSION_FIELD_NUMBER: _ClassVar[int]
    CPU_MILLIS_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    PIDS_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_BACKENDS_FIELD_NUMBER: _ClassVar[int]
    GPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    VRAM_MB_FIELD_NUMBER: _ClassVar[int]
    protocol_version: str
    worker_version: str
    cpu_millis: int
    memory_mb: int
    pids: int
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    sandbox_backends: _containers.RepeatedScalarFieldContainer[str]
    gpu_count: int
    vram_mb: int
    def __init__(self, protocol_version: _Optional[str] = ..., worker_version: _Optional[str] = ..., cpu_millis: _Optional[int] = ..., memory_mb: _Optional[int] = ..., pids: _Optional[int] = ..., capabilities: _Optional[_Iterable[str]] = ..., sandbox_backends: _Optional[_Iterable[str]] = ..., gpu_count: _Optional[int] = ..., vram_mb: _Optional[int] = ...) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ("unix_millis", "active_attempt_ids", "free_cpu_millis", "free_memory_mb", "free_gpu_count", "free_vram_mb")
    UNIX_MILLIS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_ATTEMPT_IDS_FIELD_NUMBER: _ClassVar[int]
    FREE_CPU_MILLIS_FIELD_NUMBER: _ClassVar[int]
    FREE_MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    FREE_GPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    FREE_VRAM_MB_FIELD_NUMBER: _ClassVar[int]
    unix_millis: int
    active_attempt_ids: _containers.RepeatedScalarFieldContainer[str]
    free_cpu_millis: int
    free_memory_mb: int
    free_gpu_count: int
    free_vram_mb: int
    def __init__(self, unix_millis: _Optional[int] = ..., active_attempt_ids: _Optional[_Iterable[str]] = ..., free_cpu_millis: _Optional[int] = ..., free_memory_mb: _Optional[int] = ..., free_gpu_count: _Optional[int] = ..., free_vram_mb: _Optional[int] = ...) -> None: ...

class LeaseAcknowledgement(_message.Message):
    __slots__ = ("run_id", "attempt_id", "lease_token", "accepted", "reason")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    attempt_id: str
    lease_token: str
    accepted: bool
    reason: str
    def __init__(self, run_id: _Optional[str] = ..., attempt_id: _Optional[str] = ..., lease_token: _Optional[str] = ..., accepted: _Optional[bool] = ..., reason: _Optional[str] = ...) -> None: ...

class LeaseOffer(_message.Message):
    __slots__ = ("run_id", "attempt_id", "lease_token", "expires_unix_millis", "repository_url", "repository_ref", "argv", "environment", "profile", "image_digest", "cpu_millis", "memory_mb", "pids", "disk_mb", "timeout_seconds", "network_policy", "traceparent", "gpu_count", "vram_mb", "required_capabilities")
    class EnvironmentEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_UNIX_MILLIS_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_URL_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_REF_FIELD_NUMBER: _ClassVar[int]
    ARGV_FIELD_NUMBER: _ClassVar[int]
    ENVIRONMENT_FIELD_NUMBER: _ClassVar[int]
    PROFILE_FIELD_NUMBER: _ClassVar[int]
    IMAGE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CPU_MILLIS_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    PIDS_FIELD_NUMBER: _ClassVar[int]
    DISK_MB_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    NETWORK_POLICY_FIELD_NUMBER: _ClassVar[int]
    TRACEPARENT_FIELD_NUMBER: _ClassVar[int]
    GPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    VRAM_MB_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    attempt_id: str
    lease_token: str
    expires_unix_millis: int
    repository_url: str
    repository_ref: str
    argv: _containers.RepeatedScalarFieldContainer[str]
    environment: _containers.ScalarMap[str, str]
    profile: str
    image_digest: str
    cpu_millis: int
    memory_mb: int
    pids: int
    disk_mb: int
    timeout_seconds: int
    network_policy: str
    traceparent: str
    gpu_count: int
    vram_mb: int
    required_capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, run_id: _Optional[str] = ..., attempt_id: _Optional[str] = ..., lease_token: _Optional[str] = ..., expires_unix_millis: _Optional[int] = ..., repository_url: _Optional[str] = ..., repository_ref: _Optional[str] = ..., argv: _Optional[_Iterable[str]] = ..., environment: _Optional[_Mapping[str, str]] = ..., profile: _Optional[str] = ..., image_digest: _Optional[str] = ..., cpu_millis: _Optional[int] = ..., memory_mb: _Optional[int] = ..., pids: _Optional[int] = ..., disk_mb: _Optional[int] = ..., timeout_seconds: _Optional[int] = ..., network_policy: _Optional[str] = ..., traceparent: _Optional[str] = ..., gpu_count: _Optional[int] = ..., vram_mb: _Optional[int] = ..., required_capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class RunEvent(_message.Message):
    __slots__ = ("run_id", "attempt_id", "sequence", "stream", "data", "unix_millis")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    UNIX_MILLIS_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    attempt_id: str
    sequence: int
    stream: str
    data: bytes
    unix_millis: int
    def __init__(self, run_id: _Optional[str] = ..., attempt_id: _Optional[str] = ..., sequence: _Optional[int] = ..., stream: _Optional[str] = ..., data: _Optional[bytes] = ..., unix_millis: _Optional[int] = ...) -> None: ...

class RunCompletion(_message.Message):
    __slots__ = ("run_id", "attempt_id", "lease_token", "exit_code", "terminal_state", "reason_code", "message")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    TERMINAL_STATE_FIELD_NUMBER: _ClassVar[int]
    REASON_CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    attempt_id: str
    lease_token: str
    exit_code: int
    terminal_state: str
    reason_code: str
    message: str
    def __init__(self, run_id: _Optional[str] = ..., attempt_id: _Optional[str] = ..., lease_token: _Optional[str] = ..., exit_code: _Optional[int] = ..., terminal_state: _Optional[str] = ..., reason_code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class CleanupConfirmation(_message.Message):
    __slots__ = ("run_id", "attempt_id", "successful", "message")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESSFUL_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    attempt_id: str
    successful: bool
    message: str
    def __init__(self, run_id: _Optional[str] = ..., attempt_id: _Optional[str] = ..., successful: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class CancelRun(_message.Message):
    __slots__ = ("run_id", "attempt_id")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    attempt_id: str
    def __init__(self, run_id: _Optional[str] = ..., attempt_id: _Optional[str] = ...) -> None: ...

class DrainWorker(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class ProtocolError(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...
