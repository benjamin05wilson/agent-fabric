# ---------------------------------------------------------------------------
# Instance role: exactly what the bootstrap script and the application need.
#   - pull the control image from its one ECR repository
#   - read the two secrets (database credentials, app secret)
#   - get/put objects in the one run-logs bucket
#   - write container logs to the one CloudWatch log group
#   - SSM Session Manager (managed policy)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name_prefix}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json

  tags = merge(var.tags, { Name = "${var.name_prefix}-control-plane" })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "instance" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPullControlImage"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [aws_ecr_repository.control.arn]
  }

  statement {
    sid       = "ReadSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.database_secret_arn, aws_secretsmanager_secret.app.arn]
  }

  statement {
    sid       = "ListLogsBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [var.logs_bucket_arn]
  }

  statement {
    sid       = "RunLogObjects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.logs_bucket_arn}/*"]
  }

  statement {
    sid       = "ContainerLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${var.log_group_arn}:*"]
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "${var.name_prefix}-control-plane"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.instance.json
}

resource "aws_iam_instance_profile" "this" {
  name = "${var.name_prefix}-control-plane"
  role = aws_iam_role.this.name

  tags = merge(var.tags, { Name = "${var.name_prefix}-control-plane" })
}

# ---------------------------------------------------------------------------
# Static S3 credentials for the MinIO client, limited to the run-logs bucket.
# The access key lives in Terraform state and in the app secret; rotate by
# tainting aws_iam_access_key.logs_writer and re-applying.
# ---------------------------------------------------------------------------

resource "aws_iam_user" "logs_writer" {
  name = "${var.name_prefix}-run-logs"
  path = "/service/"

  tags = merge(var.tags, { Name = "${var.name_prefix}-run-logs" })
}

data "aws_iam_policy_document" "logs_writer" {
  statement {
    sid       = "ListLogsBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [var.logs_bucket_arn]
  }

  statement {
    sid       = "RunLogObjects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${var.logs_bucket_arn}/*"]
  }
}

resource "aws_iam_user_policy" "logs_writer" {
  name   = "${var.name_prefix}-run-logs"
  user   = aws_iam_user.logs_writer.name
  policy = data.aws_iam_policy_document.logs_writer.json
}

resource "aws_iam_access_key" "logs_writer" {
  user = aws_iam_user.logs_writer.name
}
