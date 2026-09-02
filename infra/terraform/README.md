# Agent Fabric on AWS (Terraform)

A small, deployable AWS environment for the control plane described in the
[architecture documentation](../../docs/architecture.md). It demonstrates
modules, environment configuration, IAM boundaries, networking, service
configuration, a remote state strategy, and reproducible create/destroy — not
a production platform. Nothing here is expensive on purpose.

## What gets created

```text
infra/terraform/
├── envs/dev/                 root module: wires the modules, backend, provider, tags
└── modules/
    ├── network/              VPC, 2 public + 2 private subnets, 1 NAT GW, 4 security groups
    ├── database/             RDS PostgreSQL 16, db.t4g.micro, single-AZ, encrypted, private
    ├── redis/                ElastiCache Redis 7, cache.t4g.micro, single node, private
    ├── logs/                 S3 bucket for run logs (SSE-S3, private, 30-day expiry)
    ├── control_plane/        1x t4g.small (arm64 AL2023) running api+grpc+scheduler via compose
    ├── workers/              ASG of t3.small (x86_64 AL2023) with Docker + gVisor running the worker
    └── observability/        2 CloudWatch log groups (awslogs driver from both tiers)
```

| Component | Resource | Notes |
|---|---|---|
| Network | VPC `10.42.0.0/16`, 2 public + 2 private subnets, IGW, **one** NAT gateway | Workers, RDS and Redis live in private subnets. |
| Security groups | `control-plane`, `worker`, `database`, `redis` | Control plane: TCP 8000 from `api_allowed_cidrs` + VPC CIDR, TCP 50051 from the worker SG only. Workers: no inbound, egress only. DB/Redis: only from the control-plane SG. No port 22 anywhere. |
| Database | `aws_db_instance` PostgreSQL 16, `db.t4g.micro`, 20 GiB gp3, encrypted, single-AZ, not public | Master password is a `random_password` stored in Secrets Manager (`<prefix>/database`). |
| Redis | `aws_elasticache_cluster` Redis 7.1, `cache.t4g.micro`, 1 node | Private subnets only. |
| Run logs | S3 bucket `<prefix>-run-logs-<account-id>` | Versioning off, SSE-S3, public access blocked, objects expire after 30 days. |
| Control plane | `t4g.small` in a public subnet (public IP optional), IMDSv2, encrypted root | cloud-init installs Docker + compose, reads the two secrets, writes `/etc/agent-fabric/.env`, runs `api`, `grpc`, `scheduler` from the ECR image under a systemd unit. |
| Workers | ASG min 2 / desired 2 / max 3 of `t3.small`, private subnets | cloud-init installs Docker, downloads `runsc` from the official gVisor release URL (sha512 verified), runs `runsc install`, restarts Docker, then runs the worker container with `CONTROL_PLANE_GRPC=<control-plane-private-ip>:50051`. |
| Registry | 2 ECR repos: `<prefix>/control`, `<prefix>/worker` | Lifecycle keeps the last 10 images. |
| Secrets | `<prefix>/database`, `<prefix>/control-plane` | The second holds the static API key and an IAM access key limited to the run-logs bucket. |
| Logs | CloudWatch `/<prefix>/control-plane`, `/<prefix>/workers` | 14-day retention. |

`<prefix>` is `agent-fabric-dev`.

### IAM boundaries

- **Control-plane instance role**: `AmazonSSMManagedInstanceCore`, ECR pull on
  the `control` repo only, `secretsmanager:GetSecretValue` on the two secrets
  only, `s3:GetObject/PutObject/ListBucket` on the run-logs bucket only,
  `logs:CreateLogStream/PutLogEvents` on its log group only.
- **Worker instance role**: `AmazonSSMManagedInstanceCore`, ECR pull on the
  `worker` repo only, CloudWatch logs on its log group only. **No S3, no
  Secrets Manager, no RDS/ElastiCache.** A compromised worker holds no
  cloud-wide credentials (see `docs/threat-model.md`).
- **`<prefix>-run-logs` IAM user**: exists because the control plane uses the
  MinIO client with explicit credentials (`control-plane/agent_fabric/log_store.py`).
  Its policy is limited to the run-logs bucket. The access key is in Terraform
  state and in the `control-plane` secret; rotate with
  `terraform taint module.control_plane.aws_iam_access_key.logs_writer`.

### Service configuration

The control-plane bootstrap writes `/etc/agent-fabric/.env` mapping to
`control-plane/agent_fabric/config.py`:

```text
DATABASE_URL=postgresql+asyncpg://<user>:<pass>@<rds-host>:5432/agent_fabric
REDIS_URL=redis://<elasticache-host>:6379/0
MINIO_ENDPOINT=s3.<region>.amazonaws.com    # plain AWS S3 through the MinIO client
MINIO_SECURE=true
MINIO_BUCKET=<run-logs bucket>
MINIO_ACCESS_KEY / MINIO_SECRET_KEY         # from the control-plane secret
API_KEY / API_KEY_PROJECT                   # from the control-plane secret / "dev"
GRPC_BIND=0.0.0.0:50051
```

Workers receive `CONTROL_PLANE_GRPC`, `WORKER_ID` (= instance id) and
`WORKSPACE_ROOT`. The workspace directory is bind-mounted at the same path on
the host and in the worker container because `worker/sandbox/gvisor.go` passes
host paths to the Docker daemon.

## Estimated monthly cost

Rough on-demand figures for `eu-west-2`, running 24x7, September 2026 list
prices may differ — check the AWS pricing calculator before relying on this:

| Item | Approx. USD / month |
|---|---|
| NAT gateway (hourly, before data processing) | 32 |
| 2 x public IPv4 (NAT EIP + control plane) | 7 |
| Control plane `t4g.small` | 12 |
| Workers 2 x `t3.small` | 34 |
| RDS `db.t4g.micro` + 20 GiB gp3 | 16 |
| ElastiCache `cache.t4g.micro` | 13 |
| EBS 20 + 2 x 30 GiB gp3 | 7 |
| Secrets Manager (2 secrets), ECR, CloudWatch, S3 | 2–5 |
| **Total** | **about 125–140** |

NAT data processing (image pulls, git clones) and a third worker add to this.
`terraform destroy` removes everything; there is nothing left running.

## Prerequisites

- AWS CLI v2 configured for the target account, Docker with buildx, Terraform >= 1.6.
- **One-off state bootstrap** (not managed by this Terraform — it must exist before `init`):

  ```bash
  export AWS_REGION=eu-west-2
  export STATE_BUCKET=<your-unique-name>-agent-fabric-tfstate
  export LOCK_TABLE=<your-unique-name>-agent-fabric-tflock

  aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION"
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
    --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws dynamodb create-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
  ```

  Then either edit the `REPLACE_ME-...` placeholders in `envs/dev/backend.tf`
  or pass them at init time (shown below).

## Static validation

CI runs formatting and configuration validation without contacting an AWS
account:

```bash
terraform fmt -check -recursive infra/terraform
terraform -chdir=infra/terraform/envs/dev init -backend=false -input=false
terraform -chdir=infra/terraform/envs/dev validate
```

`validate` checks provider and module configuration, not credentials, remote
state availability, an execution plan, or a real AWS deployment.

## Deploy

```bash
cd infra/terraform/envs/dev
cp terraform.tfvars.example terraform.tfvars   # set api_allowed_cidrs to your /32

terraform init \
  -backend-config="bucket=$STATE_BUCKET" \
  -backend-config="dynamodb_table=$LOCK_TABLE" \
  -backend-config="region=$AWS_REGION"
```

### 1. Create the registries and push images

Instances retry `docker pull` every 30 s until the image exists, so you can
also run a full `apply` first — but pushing first avoids the wait.

```bash
terraform apply \
  -target=module.control_plane.aws_ecr_repository.control \
  -target=module.workers.aws_ecr_repository.worker

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"
CONTROL_REPO="$REGISTRY/agent-fabric-dev/control"
WORKER_REPO="$REGISTRY/agent-fabric-dev/worker"

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"

# From the repository root. Control plane runs on arm64 (t4g), workers on x86_64 (t3).
docker buildx build --platform linux/arm64 \
  -f control-plane/Dockerfile --target runtime \
  -t "$CONTROL_REPO:latest" --push .

docker buildx build --platform linux/amd64 \
  -f worker/Dockerfile \
  -t "$WORKER_REPO:latest" --push .
```

### 2. Create everything else

```bash
terraform plan -out=dev.tfplan
terraform apply dev.tfplan
terraform output
```

First boot takes roughly 5–10 minutes (RDS is the slow part, then cloud-init
on the instances).

## Verify

```bash
CP=$(terraform output -raw control_plane_instance_id)

# Shell on the control plane — no SSH, no key pair, no port 22.
aws ssm start-session --target "$CP"
```

Inside the session:

```bash
sudo tail -n 50 /var/log/agent-fabric-bootstrap.log
sudo systemctl status agent-fabric
sudo docker ps
curl -s http://localhost:8000/health
curl -s http://$(hostname -I | awk '{print $1}'):8000/health   # via the private IP, as a worker would see it
```

From your machine, if your IP is in `api_allowed_cidrs`:

```bash
API_KEY=$(aws secretsmanager get-secret-value --secret-id "$(terraform output -raw app_secret_arn)" \
  --query SecretString --output text | jq -r .api_key)
curl -s "$(terraform output -raw api_url)/health"
curl -s -H "Authorization: Bearer $API_KEY" "$(terraform output -raw api_url)/workers"
```

Workers: `aws ssm start-session --target <worker-instance-id>` (ids from
`aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names
$(terraform output -raw worker_autoscaling_group_name)`), then
`sudo docker info --format '{{json .Runtimes}}'` should list `runsc`, and
`sudo systemctl status agent-fabric-worker` should be active. Container output
for both tiers is in the CloudWatch log groups printed by `terraform output`.

### Observability profile

Only container logs are shipped (CloudWatch, via the Docker `awslogs`
driver). The repository's `docker-compose.yml` observability services
(otel-collector, Prometheus, Grafana, Tempo) are not deployed by Terraform.
To run them on the control-plane instance: copy the `observability/`
directory there (SSM `scp`-style via `aws s3 cp` to the run-logs bucket is
the simplest route, the instance role can read it), add those services to
`/etc/agent-fabric/docker-compose.yml`, set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317` in `.env`, and
`systemctl restart agent-fabric`. Reach Grafana over an SSM port forward:
`aws ssm start-session --target "$CP" --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["3000"],"localPortNumber":["3000"]}'`.

## Destroy

```bash
cd infra/terraform/envs/dev
terraform destroy
```

Everything is configured so destroy is clean without manual steps: RDS skips
the final snapshot, the S3 bucket has `force_destroy`, ECR repos have
`force_delete`, secrets use a 0-day recovery window. Destroy takes a few
minutes (NAT gateway and RDS deletion dominate). The state bucket and lock
table are not managed here and remain.

## Deliberately not done

- **No TLS, no load balancer.** The API is plain HTTP on 8000 straight to the
  instance, gated by a security-group CIDR. Put an ALB + ACM certificate in
  front before exposing it to anyone else.
- **No multi-AZ** for RDS or Redis, no read replicas, no Redis replication
  group; one NAT gateway is a single point of failure for private egress.
- **No autoscaling policies.** Worker capacity is a variable (`desired 2`,
  `max 3`); nothing scales on load.
- **Static API key** generated once and stored in Secrets Manager. No
  rotation, one project, no per-user keys.
- **Static S3 access key** for the MinIO client (see IAM boundaries). Moving
  `log_store.py` to `minio.credentials.IamAwsProvider` would remove it; the
  instance role already carries the needed bucket permissions.
- **No VPC endpoints.** ECR, S3, SSM and Secrets Manager traffic goes out
  through the NAT gateway. Interface endpoints would remove that dependency
  at extra cost.
- **Latest AMI tracking.** Both tiers select the latest AL2023 AMI from the
  public SSM parameter at plan time. The control-plane instance ignores AMI
  drift (`ignore_changes = [ami]`); the worker launch template does not, so a
  new AMI shows up as a rolling instance refresh on the next apply. Pin with
  `ami_id` if you want it frozen.
- **No drift detection, cost alarms, or apply pipeline.** CI performs static
  format and validation checks only. RDS automated backups retain one day.
- The `dev` environment is the only one. A second environment is a new
  directory under `envs/` reusing the same modules with a different state key.
