terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  ami_id       = var.ami_id != "" ? var.ami_id : data.aws_ssm_parameter.al2023_arm64.value
  image        = "${aws_ecr_repository.control.repository_url}:${var.image_tag}"
  ecr_registry = split("/", aws_ecr_repository.control.repository_url)[0]
  s3_endpoint  = "s3.${var.aws_region}.amazonaws.com"
}

# ---------------------------------------------------------------------------
# Image registry
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "control" {
  name                 = "${var.name_prefix}/control"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.ecr_force_delete

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-control" })
}

resource "aws_ecr_lifecycle_policy" "control" {
  repository = aws_ecr_repository.control.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# Application secret: static API key plus the S3 access key used by the
# MinIO client in the control plane (it takes explicit credentials, so an
# instance-profile-only setup would need a code change).
# ---------------------------------------------------------------------------

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name_prefix}/control-plane"
  description             = "Agent Fabric control-plane API key and run-log S3 credentials"
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { Name = "${var.name_prefix}-control-plane" })
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id

  secret_string = jsonencode({
    api_key              = "af_${random_password.api_key.result}"
    s3_access_key_id     = aws_iam_access_key.logs_writer.id
    s3_secret_access_key = aws_iam_access_key.logs_writer.secret
  })
}

# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "this" {
  ami                         = local.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = var.security_group_ids
  associate_public_ip_address = var.associate_public_ip
  iam_instance_profile        = aws_iam_instance_profile.this.name

  # No key pair on purpose: access is via SSM Session Manager only.
  key_name = null

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    aws_region          = var.aws_region
    ecr_registry        = local.ecr_registry
    image               = local.image
    app_secret_arn      = aws_secretsmanager_secret.app.arn
    database_secret_arn = var.database_secret_arn
    redis_url           = "redis://${var.redis_address}:${var.redis_port}/0"
    s3_endpoint         = local.s3_endpoint
    logs_bucket         = var.logs_bucket_name
    api_key_project     = var.api_key_project
    log_group           = var.log_group_name
    compose_version     = var.docker_compose_version
  })
  user_data_replace_on_change = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-control-plane"
    Role = "control-plane"
  })

  volume_tags = merge(var.tags, { Name = "${var.name_prefix}-control-plane" })

  lifecycle {
    # The SSM "latest AMI" parameter moves frequently; do not replace the
    # instance on every plan because of it. Change ami_id explicitly instead.
    ignore_changes = [ami]
  }

  depends_on = [aws_secretsmanager_secret_version.app]
}
