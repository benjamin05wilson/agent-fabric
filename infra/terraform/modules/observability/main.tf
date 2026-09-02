terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Deliberately minimal. Container stdout/stderr from the control plane and the
# workers is shipped by the Docker awslogs driver (configured in each module's
# user_data) into these two groups. Metrics and traces are NOT wired into
# CloudWatch: the repository's docker-compose observability services
# (otel-collector, Prometheus, Grafana, Tempo) can be run on the control-plane
# instance if needed; see infra/terraform/README.md.

resource "aws_cloudwatch_log_group" "control_plane" {
  name              = "/${var.name_prefix}/control-plane"
  retention_in_days = var.retention_days

  tags = merge(var.tags, { Name = "${var.name_prefix}-control-plane" })
}

resource "aws_cloudwatch_log_group" "workers" {
  name              = "/${var.name_prefix}/workers"
  retention_in_days = var.retention_days

  tags = merge(var.tags, { Name = "${var.name_prefix}-workers" })
}
