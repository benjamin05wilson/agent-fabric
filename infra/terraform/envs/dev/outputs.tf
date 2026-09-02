output "vpc_id" {
  value = module.network.vpc_id
}

output "control_plane_instance_id" {
  description = "Use with: aws ssm start-session --target <id>"
  value       = module.control_plane.instance_id
}

output "control_plane_private_ip" {
  value = module.control_plane.private_ip
}

output "control_plane_public_ip" {
  value = module.control_plane.public_ip
}

output "api_url" {
  description = "Reachable only from api_allowed_cidrs (and inside the VPC via the private IP)."
  value       = var.control_plane_public_ip ? "http://${module.control_plane.public_ip}:8000" : "http://${module.control_plane.private_ip}:8000"
}

output "grpc_endpoint" {
  description = "Private address workers dial; not reachable from outside the VPC."
  value       = module.control_plane.grpc_endpoint
}

output "ecr_control_repository_url" {
  value = module.control_plane.ecr_repository_url
}

output "ecr_worker_repository_url" {
  value = module.workers.ecr_repository_url
}

output "worker_autoscaling_group_name" {
  value = module.workers.autoscaling_group_name
}

output "run_logs_bucket" {
  value = module.logs.bucket_name
}

output "database_address" {
  value = module.database.address
}

output "database_secret_arn" {
  value = module.database.secret_arn
}

output "app_secret_arn" {
  description = "Holds api_key (Bearer token for the API) and the S3 access key pair."
  value       = module.control_plane.app_secret_arn
}

output "redis_address" {
  value = module.redis.address
}

output "control_plane_log_group" {
  value = module.observability.control_plane_log_group_name
}

output "workers_log_group" {
  value = module.observability.workers_log_group_name
}
