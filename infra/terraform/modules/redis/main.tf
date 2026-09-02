terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-redis"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis" })
}

# Single node, no replication group: Redis is a wake-up/delivery layer here and
# the scheduler reconciles from PostgreSQL after a restart, so losing it is
# tolerable in dev.
resource "aws_elasticache_cluster" "this" {
  cluster_id = "${var.name_prefix}-redis"

  engine               = "redis"
  engine_version       = var.engine_version
  node_type            = var.node_type
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = var.security_group_ids

  apply_immediately        = true
  snapshot_retention_limit = 0

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis" })
}
