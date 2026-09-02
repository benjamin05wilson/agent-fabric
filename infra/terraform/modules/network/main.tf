terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# ---------------------------------------------------------------------------
# VPC, subnets, routing
# ---------------------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${count.index}"
    Tier = "public"
  })
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-private-${count.index}"
    Tier = "private"
  })
}

# One NAT gateway only: this is a dev environment and a second one would
# double the most expensive line item for no functional gain.
resource "aws_eip" "nat" {
  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags = merge(var.tags, { Name = "${var.name_prefix}-nat" })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-private" })
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# Security groups. The aws_security_group resource strips the AWS default
# allow-all egress rule, so every allowed flow below is explicit.
# ---------------------------------------------------------------------------

resource "aws_security_group" "control_plane" {
  name        = "${var.name_prefix}-control-plane"
  description = "Agent Fabric control plane (API, gRPC gateway, scheduler)"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-control-plane" })
}

resource "aws_security_group" "worker" {
  name        = "${var.name_prefix}-worker"
  description = "Agent Fabric gVisor workers"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-worker" })
}

resource "aws_security_group" "database" {
  name        = "${var.name_prefix}-database"
  description = "Agent Fabric PostgreSQL"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-database" })
}

resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis"
  description = "Agent Fabric Redis"
  vpc_id      = aws_vpc.this.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis" })
}

# Control plane: API from allowed CIDRs, gRPC from workers only, egress anywhere
# (ECR, Secrets Manager, S3, SSM, RDS, Redis all go out through this rule).

resource "aws_vpc_security_group_ingress_rule" "control_plane_api" {
  for_each = toset(var.api_allowed_cidrs)

  security_group_id = aws_security_group.control_plane.id
  description       = "HTTP API"
  cidr_ipv4         = each.value
  from_port         = 8000
  to_port           = 8000
  ip_protocol       = "tcp"

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_grpc_from_workers" {
  security_group_id            = aws_security_group.control_plane.id
  description                  = "gRPC from workers"
  referenced_security_group_id = aws_security_group.worker.id
  from_port                    = 50051
  to_port                      = 50051
  ip_protocol                  = "tcp"

  tags = var.tags
}

resource "aws_vpc_security_group_egress_rule" "control_plane_all" {
  security_group_id = aws_security_group.control_plane.id
  description       = "All outbound"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"

  tags = var.tags
}

# Workers: no inbound at all (SSM Session Manager is outbound-initiated).
# Outbound is open because untrusted jobs may clone public repositories and
# workers pull profile images; the sandbox network policy is enforced per run.

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker.id
  description       = "All outbound (git clone, image pulls, ECR, SSM, gRPC)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"

  tags = var.tags
}

# Database and Redis: only the control-plane security group may connect.

resource "aws_vpc_security_group_ingress_rule" "database_from_control_plane" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from control plane"
  referenced_security_group_id = aws_security_group.control_plane.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"

  tags = var.tags
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_control_plane" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from control plane"
  referenced_security_group_id = aws_security_group.control_plane.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"

  tags = var.tags
}
