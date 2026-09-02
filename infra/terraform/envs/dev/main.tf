data "aws_caller_identity" "current" {}

locals {
  name_prefix = "${var.project}-${var.environment}"

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  # Bucket names are global; the account id keeps this reproducible per account.
  run_logs_bucket = "${local.name_prefix}-run-logs-${data.aws_caller_identity.current.account_id}"
}

module "network" {
  source = "../../modules/network"

  name_prefix       = local.name_prefix
  vpc_cidr          = var.vpc_cidr
  api_allowed_cidrs = distinct(concat([var.vpc_cidr], var.api_allowed_cidrs))
  tags              = local.tags
}

module "logs" {
  source = "../../modules/logs"

  bucket_name     = local.run_logs_bucket
  expiration_days = var.run_logs_expiration_days
  force_destroy   = true
  tags            = local.tags
}

module "observability" {
  source = "../../modules/observability"

  name_prefix    = local.name_prefix
  retention_days = var.log_retention_days
  tags           = local.tags
}

module "database" {
  source = "../../modules/database"

  name_prefix        = local.name_prefix
  subnet_ids         = module.network.private_subnet_ids
  security_group_ids = [module.network.database_security_group_id]
  instance_class     = var.db_instance_class
  tags               = local.tags
}

module "redis" {
  source = "../../modules/redis"

  name_prefix        = local.name_prefix
  subnet_ids         = module.network.private_subnet_ids
  security_group_ids = [module.network.redis_security_group_id]
  node_type          = var.redis_node_type
  tags               = local.tags
}

module "control_plane" {
  source = "../../modules/control_plane"

  name_prefix         = local.name_prefix
  aws_region          = var.aws_region
  subnet_id           = module.network.public_subnet_ids[0]
  security_group_ids  = [module.network.control_plane_security_group_id]
  instance_type       = var.control_plane_instance_type
  associate_public_ip = var.control_plane_public_ip
  image_tag           = var.image_tag

  database_secret_arn = module.database.secret_arn
  redis_address       = module.redis.address
  redis_port          = module.redis.port
  logs_bucket_name    = module.logs.bucket_name
  logs_bucket_arn     = module.logs.bucket_arn
  log_group_name      = module.observability.control_plane_log_group_name
  log_group_arn       = module.observability.control_plane_log_group_arn
  api_key_project     = var.environment

  tags = local.tags
}

module "workers" {
  source = "../../modules/workers"

  name_prefix        = local.name_prefix
  aws_region         = var.aws_region
  subnet_ids         = module.network.private_subnet_ids
  security_group_ids = [module.network.worker_security_group_id]
  instance_type      = var.worker_instance_type
  min_size           = var.worker_min_size
  desired_capacity   = var.worker_desired_capacity
  max_size           = var.worker_max_size
  image_tag          = var.image_tag
  gvisor_release     = var.gvisor_release

  control_plane_grpc = module.control_plane.grpc_endpoint
  log_group_name     = module.observability.workers_log_group_name
  log_group_arn      = module.observability.workers_log_group_arn

  tags = local.tags
}
