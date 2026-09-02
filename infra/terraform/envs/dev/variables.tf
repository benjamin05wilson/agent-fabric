variable "aws_region" {
  description = "AWS region for every resource."
  type        = string
  default     = "eu-west-2"
}

variable "project" {
  description = "Project tag and name prefix."
  type        = string
  default     = "agent-fabric"
}

variable "environment" {
  description = "Environment tag and name suffix."
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.42.0.0/16"
}

variable "api_allowed_cidrs" {
  description = "CIDRs (e.g. your office/VPN egress /32) allowed to reach the API on TCP 8000. The VPC CIDR is always allowed."
  type        = list(string)
  default     = []
}

variable "control_plane_public_ip" {
  description = "Attach a public IP to the control-plane instance so api_allowed_cidrs can reach it from the internet."
  type        = bool
  default     = true
}

variable "control_plane_instance_type" {
  description = "arm64 instance type for the control plane."
  type        = string
  default     = "t4g.small"
}

variable "worker_instance_type" {
  description = "x86_64 instance type for workers."
  type        = string
  default     = "t3.small"
}

variable "worker_min_size" {
  description = "Minimum workers in the ASG."
  type        = number
  default     = 2
}

variable "worker_desired_capacity" {
  description = "Desired workers in the ASG."
  type        = number
  default     = 2
}

variable "worker_max_size" {
  description = "Maximum workers in the ASG."
  type        = number
  default     = 3

  validation {
    condition     = var.worker_max_size <= 3
    error_message = "Keep the dev fleet small; raise deliberately if you need more than 3 workers."
  }
}

variable "image_tag" {
  description = "ECR image tag used for both the control and worker images."
  type        = string
  default     = "latest"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 14
}

variable "run_logs_expiration_days" {
  description = "S3 lifecycle expiry for run logs."
  type        = number
  default     = 30
}

variable "gvisor_release" {
  description = "gVisor release to install on workers (latest or a dated release such as 20240916.0)."
  type        = string
  default     = "latest"
}
