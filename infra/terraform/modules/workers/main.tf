terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_ssm_parameter" "al2023_x86_64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  ami_id       = var.ami_id != "" ? var.ami_id : data.aws_ssm_parameter.al2023_x86_64.value
  image        = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
  ecr_registry = split("/", aws_ecr_repository.worker.repository_url)[0]

  instance_tags = merge(var.tags, {
    Name = "${var.name_prefix}-worker"
    Role = "worker"
  })
}

# ---------------------------------------------------------------------------
# Image registry
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "worker" {
  name                 = "${var.name_prefix}/worker"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.ecr_force_delete

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-worker" })
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name

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
# Launch template + ASG. No scaling policies: capacity is a variable.
# ---------------------------------------------------------------------------

resource "aws_launch_template" "worker" {
  name_prefix            = "${var.name_prefix}-worker-"
  image_id               = local.ami_id
  instance_type          = var.instance_type
  vpc_security_group_ids = var.security_group_ids
  update_default_version = true

  # No key pair on purpose: access is via SSM Session Manager only.

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_type           = "gp3"
      volume_size           = var.root_volume_gb
      encrypted             = true
      delete_on_termination = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/templates/user_data.sh.tftpl", {
    aws_region         = var.aws_region
    ecr_registry       = local.ecr_registry
    image              = local.image
    control_plane_grpc = var.control_plane_grpc
    log_group          = var.log_group_name
    gvisor_release     = var.gvisor_release
  }))

  tag_specifications {
    resource_type = "instance"
    tags          = local.instance_tags
  }

  tag_specifications {
    resource_type = "volume"
    tags          = local.instance_tags
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-worker" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "worker" {
  name                = "${var.name_prefix}-worker"
  min_size            = var.min_size
  desired_capacity    = var.desired_capacity
  max_size            = var.max_size
  vpc_zone_identifier = var.subnet_ids

  health_check_type         = "EC2"
  health_check_grace_period = 300
  default_cooldown          = 60

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }

  # Roll instances when the launch template (AMI, user_data, image tag) changes.
  instance_refresh {
    strategy = "Rolling"

    preferences {
      min_healthy_percentage = 50
    }
  }

  dynamic "tag" {
    for_each = local.instance_tags

    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}
