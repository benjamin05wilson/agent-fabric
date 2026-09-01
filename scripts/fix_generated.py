"""Make protoc's flat Python imports package-relative after Buf generation."""

from pathlib import Path

root = Path(__file__).resolve().parents[1] / "control-plane" / "agent_fabric" / "generated"
grpc_file = root / "worker_pb2_grpc.py"
source = grpc_file.read_text(encoding="utf-8")
source = source.replace("import worker_pb2 as worker__pb2", "from . import worker_pb2 as worker__pb2")
grpc_file.write_text(source, encoding="utf-8")
(root / "__init__.py").write_text('"""Generated protobuf modules."""\n', encoding="utf-8")
