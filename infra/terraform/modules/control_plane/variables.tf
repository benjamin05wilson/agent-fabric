variable "name_prefix" {
  description = "Prefix applied to resource names."
  type        = string
}

variable "aws_region" {
  description = "Region, used for the S3 endpoint, ECR login and awslogs driver."
  type        = string
}

variable "subnet_id" {
  description = "Subnet for the instance (public if associate_public_ip is true)."
  type        = string
}

variable "security_group_ids" {
  description = "Security groups attached to the instance."
  type        = list(string)
}

variable "instance_type" {
  description = "arm64 instance type (Amazon Linux 2023 arm64 AMI is used)."
  type        = string
  default     = "t4g.small"
}

variable "ami_id" {
  description = "Override AMI. Empty selects the latest AL2023 arm64 AMI via SSM."
  type        = string
  default     = ""
}

variable "root_volume_gb" {
  description = "Root volume size in GiB."
  type        = number
  default     = 20
}

variable "associate_public_ip" {
  description = "Give the instance a public IP so the API is reachable from api_allowed_cidrs."
  type        = bool
  default     = true
}

variable "image_tag" {
  description = "Tag of the control image in ECR to run."
  type        = string
  default     = "latest"
}

variable "ecr_force_delete" {
  description = "Delete the ECR repository even if it contains images."
  type        = bool
  default     = true
}

variable "database_secret_arn" {
  description = "Secrets Manager ARN with host/port/dbname/username/password JSON."
  type        = string
}

variable "redis_address" {
  description = "Redis hostname."
  type        = string
}

variable "redis_port" {
  description = "Redis port."
  type        = number
  default     = 6379
}

variable "logs_bucket_name" {
  description = "S3 bucket that receives run logs."
  type        = string
}

variable "logs_bucket_arn" {
  description = "ARN of the run-logs bucket."
  type        = string
}

variable "log_group_name" {
  description = "CloudWatch log group for container output."
  type        = string
}

variable "log_group_arn" {
  description = "ARN of the CloudWatch log group."
  type        = string
}

variable "api_key_project" {
  description = "Project slug bound to the static API key."
  type        = string
  default     = "dev"
}

variable "docker_compose_version" {
  description = "Docker Compose plugin release installed on the instance."
  type        = string
  default     = "v2.29.7"
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager recovery window; 0 deletes immediately."
  type        = number
  default     = 0
}

variable "tags" {
  description = "Tags merged into every taggable resource."
  type        = map(string)
  default     = {}
}
