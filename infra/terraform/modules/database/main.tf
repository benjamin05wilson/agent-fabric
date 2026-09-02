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

# No special characters: the password is interpolated into an asyncpg URL by
# the control-plane bootstrap script and we do not want to URL-encode it there.
resource "random_password" "master" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "database" {
  name                    = "${var.name_prefix}/database"
  description             = "Agent Fabric PostgreSQL master credentials"
  recovery_window_in_days = var.secret_recovery_window_days

  tags = merge(var.tags, { Name = "${var.name_prefix}-database" })
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-postgres"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, { Name = "${var.name_prefix}-postgres" })
}

resource "aws_db_instance" "this" {
  identifier = "${var.name_prefix}-postgres"

  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage = var.allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.username
  password = random_password.master.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = var.security_group_ids
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period    = var.backup_retention_days
  auto_minor_version_upgrade = true
  apply_immediately          = true
  deletion_protection        = false
  skip_final_snapshot        = var.skip_final_snapshot
  copy_tags_to_snapshot      = true

  performance_insights_enabled = false

  tags = merge(var.tags, { Name = "${var.name_prefix}-postgres" })
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id

  secret_string = jsonencode({
    engine   = "postgres"
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    dbname   = aws_db_instance.this.db_name
    username = aws_db_instance.this.username
    password = random_password.master.result
  })
}
